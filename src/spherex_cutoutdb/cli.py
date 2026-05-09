"""Argparse CLI for ``spxcutdb``."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
import yaml

from .catalog import ingest_catalog, normalize_sources, read_source_catalog, validate_sources
from .config import (
    ALLOWED_CUTOUT_COLLECTIONS,
    EXCLUDED_CUTOUT_COLLECTIONS,
    Config,
    ensure_project_directories,
    load_config,
    config_hash,
    validate_effective_config,
    write_default_config,
)
from .database import (
    connect,
    finish_run,
    initialize_schema,
    deactivate_source_product_matches,
    record_failure,
    record_validation,
    start_run,
    upsert_discovery_products,
    upsert_source_product_matches,
)
from .downloader import count_planned_downloads, download_plan as run_download_plan
from .downloader import resolve_project_path
from .exceptions import CatalogError, ConfigError, SpxCutoutDBError
from .irsa_sia import build_match_rows, discover_for_source
from .integrated_workflow import (
    run_catalog_workflow,
    summarize_workflow_project,
    validate_project_catalog,
)
from .logging_utils import configure_logging, make_console
from .manifest import export_manifests
from .calibration import sync_calibrations, validate_cached_calibrations
from .photometry.workflow import (
    plan_photometry,
    run_photometry,
    run_source_photometry,
    summarize_photometry,
)
from .photometry.outputs import source_output_paths, validate_source_outputs
from .photometry.result_store import finish_photometry_run, start_photometry_run
from .planner import plan_downloads
from .summary import coverage_dataframe, print_summary, write_summary_json
from .validator import validate_cutout


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except CatalogError as exc:
        print(f"catalog error: {exc}", file=sys.stderr)
        return 3
    except SpxCutoutDBError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spxcutdb",
        description=(
            "Manage SPHEREx Level-2 cutout provenance and run conservative "
            "PSF forced photometry with calibration provenance."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    init = sub.add_parser("init", help="Create project structure and database")
    init.add_argument("project_path", nargs="?", default=".")
    init.add_argument("--project", dest="project_option", type=Path, help="Project directory; equivalent to positional project_path")
    init.add_argument("--catalog", type=Path)
    init.add_argument("--target-id-column", help="Catalog column to use as durable source identity")
    init.add_argument("--ra-column", help="Catalog RA column")
    init.add_argument("--dec-column", help="Catalog Dec column")
    init.add_argument("--force", action="store_true")
    init.add_argument("--default-cutout-size-arcsec", type=float, default=180.0)
    init.add_argument("--include-deep", dest="include_deep", action="store_true", default=True)
    init.add_argument("--no-include-deep", dest="include_deep", action="store_false")
    init.set_defaults(func=cmd_init)

    config_cmd = sub.add_parser("config", help="Inspect and validate effective configuration")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show", help="Print the effective project configuration")
    add_common(config_show)
    config_show.add_argument("--batch-config", type=Path, help="Apply the same run override file used by spxcutdb run")
    config_show.add_argument("--effective", action="store_true", help="Accepted for clarity; project configs are printed after overrides")
    config_show.add_argument("--hash", action="store_true", help="Print the effective configuration hash")
    config_show.add_argument("--format", choices=["yaml", "json"], default="yaml")
    config_show.set_defaults(func=cmd_config_show)
    config_validate = config_sub.add_parser("validate", help="Validate effective config and catalog mapping")
    add_common(config_validate)
    config_validate.add_argument("--batch-config", type=Path, help="Apply the same run override file used by spxcutdb run")
    config_validate.set_defaults(func=cmd_config_validate)
    config_defaults = config_sub.add_parser("defaults", help="Print built-in default configuration")
    config_defaults.add_argument("--format", choices=["yaml", "json"], default="yaml")
    config_defaults.set_defaults(func=cmd_config_defaults)
    config_diff = config_sub.add_parser("diff", help="Diff effective config against built-in defaults")
    add_common(config_diff)
    config_diff.add_argument("--batch-config", type=Path, help="Apply the same run override file used by spxcutdb run")
    config_diff.add_argument("--against-defaults", action="store_true", default=True)
    config_diff.add_argument("--format", choices=["text", "json"], default="text")
    config_diff.set_defaults(func=cmd_config_diff)

    catalog = sub.add_parser("catalog", help="Catalog commands")
    cat_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    cat_val = cat_sub.add_parser("validate")
    add_common(cat_val)
    cat_val.set_defaults(func=cmd_catalog_validate)
    cat_ing = cat_sub.add_parser("ingest")
    add_common(cat_ing)
    cat_ing.set_defaults(func=cmd_catalog_ingest)

    discover = sub.add_parser("discover", help="Discover overlapping SPHEREx parent products")
    add_common(discover)
    discover.add_argument("--collections")
    discover.add_argument("--force-discovery", action="store_true")
    discover.add_argument("--search-radius-arcsec", type=float)
    discover.add_argument("--maxrec", type=int)
    discover.add_argument("--concurrency", type=int)
    discover.add_argument("--resume", action="store_true")
    discover.add_argument("--update", action="store_true")
    discover.add_argument("--source-name", help="Restrict discovery to one catalog source_name")
    discover.add_argument("--tap-fallback", action="store_true")
    discover.add_argument("--mock-sia", type=Path)
    discover.set_defaults(func=cmd_discover)

    plan = sub.add_parser("plan", help="Plan downloader cutout work from discovered matches")
    add_common(plan)
    plan.add_argument("--source-name", help="Restrict the plan to one catalog source_name")
    plan.add_argument("--force-plan", action="store_true")
    plan.add_argument("--redownload-invalid", dest="redownload_invalid", action="store_true", default=None)
    plan.add_argument("--no-redownload-invalid", dest="redownload_invalid", action="store_false")
    plan.add_argument("--active-only", action="store_true")
    plan.add_argument("--export-plan", action="store_true")
    plan.set_defaults(func=cmd_plan)

    download = sub.add_parser("download", help="Download and validate planned cutouts")
    add_common(download)
    download.add_argument("--max-downloads", type=int)
    download.add_argument("--concurrency", type=int)
    add_download_runtime_options(download)
    download.add_argument("--retry-failed-only", action="store_true")
    download.add_argument("--no-progress", action="store_true")
    download.set_defaults(func=cmd_download)

    validate = sub.add_parser("validate", help="Validate local cutout FITS files")
    add_common(validate)
    validate.add_argument("--catalog", type=Path, help="Run project/catalog preflight instead of cutout validation")
    validate.add_argument("--path", type=Path)
    validate.add_argument("--failed-only", action="store_true")
    validate.add_argument("--missing-only", action="store_true")
    validate.add_argument("--update-db", dest="update_db", action="store_true", default=True)
    validate.add_argument("--no-update-db", dest="update_db", action="store_false")
    validate.set_defaults(func=cmd_validate)

    validate_cutouts = sub.add_parser("validate-cutouts", help="Compatibility alias for local cutout FITS validation")
    add_common(validate_cutouts)
    validate_cutouts.add_argument("--path", type=Path)
    validate_cutouts.add_argument("--failed-only", action="store_true")
    validate_cutouts.add_argument("--missing-only", action="store_true")
    validate_cutouts.add_argument("--update-db", dest="update_db", action="store_true", default=True)
    validate_cutouts.add_argument("--no-update-db", dest="update_db", action="store_false")
    validate_cutouts.set_defaults(func=cmd_validate_cutouts)

    update_db = sub.add_parser("update-db", help="Refresh local cutout file-existence metadata")
    add_common(update_db)
    update_db.set_defaults(func=cmd_update_db)

    coverage = sub.add_parser("coverage", help="Summarize discovery/download coverage by source")
    add_common(coverage)
    coverage.add_argument("--format", choices=["table", "csv", "json"], default="table")
    coverage.add_argument("--output", type=Path)
    coverage.add_argument("--active-only", action="store_true", default=True)
    coverage.add_argument("--failed-only", action="store_true")
    coverage.add_argument("--no-coverage-only", action="store_true")
    coverage.set_defaults(func=cmd_coverage)

    retry = sub.add_parser("retry-failed", help="Retry failed downloader work")
    add_common(retry)
    retry.add_argument("--phase", choices=["download", "validation", "all"], default="download")
    retry.add_argument("--max-retries", type=int)
    retry.add_argument("--max-downloads", type=int)
    retry.add_argument("--concurrency", type=int)
    add_download_runtime_options(retry)
    retry.add_argument("--no-progress", action="store_true")
    retry.set_defaults(func=cmd_retry_failed)

    clean = sub.add_parser("clean-partials", help="Remove old partial downloader files")
    add_common(clean)
    clean.add_argument("--older-than-hours", type=float, default=24.0)
    clean.add_argument("--quarantine-invalid", action="store_true")
    clean.add_argument("--delete-invalid", action="store_true")
    clean.set_defaults(func=cmd_clean_partials)

    export = sub.add_parser("export-manifest", help="Export database manifest tables")
    add_common(export)
    export.add_argument("--table", action="append")
    export.add_argument("--format", action="append", default=[])
    export.add_argument("--output-dir", type=Path)
    export.add_argument("--active-only", action="store_true")
    export.set_defaults(func=cmd_export_manifest)

    calibration = sub.add_parser(
        "calibration",
        aliases=["calib"],
        help="Sync, validate, and inspect photometry calibration products",
        description=(
            "Manage the calibration cache used by photometry. Required science "
            "products are Spectral WCS (CWAVE/CBAND) and solid_angle_pixel_map."
        ),
    )
    cal_sub = calibration.add_subparsers(dest="calibration_command", required=True)
    cal_sync = cal_sub.add_parser(
        "sync",
        help="Import/download required calibration FITS products",
        description=(
            "Import or download calibration files, validate them, and register "
            "them in the project database. By default this uses the public "
            "SPHEREx QR2 cloud mirror with concurrent downloads. Use "
            "--download-source ibe for the official IRSA IBE directories, "
            "--download-source auto to probe both, --input-dir for an existing "
            "cache, or --url PRODUCT=URL_TEMPLATE for a mirror."
        ),
    )
    add_common(cal_sync)
    add_calibration_options(cal_sync)
    cal_sync.set_defaults(func=cmd_calibration_sync)
    cal_status = cal_sub.add_parser("status", help="Show registered calibration products")
    add_common(cal_status)
    cal_status.set_defaults(func=cmd_calibration_status)
    cal_validate = cal_sub.add_parser("validate", help="Revalidate calibration files already in the cache")
    add_common(cal_validate)
    cal_validate.add_argument("--product", action="append")
    cal_validate.set_defaults(func=cmd_calibration_validate)

    photometry = sub.add_parser(
        "photometry",
        help="Plan and run SPHEREx PSF forced photometry",
        description=(
            "Run conservative downstream photometry from validated downloader "
            "cutouts and registered calibration products. Measurements preserve "
            "negative/non-detected forced fluxes and keep science recommendation "
            "separate from detection status."
        ),
    )
    phot_sub = photometry.add_subparsers(dest="photometry_command", required=True)
    phot_plan = phot_sub.add_parser(
        "plan",
        help="Classify photometry work before downloading",
        description=(
            "Classify each source/product candidate as already measured, valid "
            "cutout needing measurement, missing/invalid cutout, or blocked by "
            "missing calibration."
        ),
    )
    add_common(phot_plan)
    phot_plan.add_argument("--source-name", help="Restrict the plan to one catalog source_name")
    phot_plan.add_argument("--force-rerun", action="store_true", help="Preview work as if matching current-identity photometry rows must be remeasured")
    phot_plan.set_defaults(func=cmd_photometry_plan)
    phot_source = phot_sub.add_parser(
        "source",
        help="Run photometry for one source",
        description="Run the low-storage workflow for one source selected by --source-id or --source-name.",
    )
    add_common(phot_source)
    phot_source.add_argument("--source-name", help="Select one source by catalog source_name")
    add_photometry_runtime_options(phot_source)
    phot_source.set_defaults(func=cmd_photometry_source)
    phot_run = phot_sub.add_parser(
        "run",
        help="Run photometry for many sources",
        description="Run the resumable low-storage workflow for selected or active catalog sources.",
    )
    add_common(phot_run)
    phot_run.add_argument("--source-name", help="Restrict the run to one catalog source_name")
    add_photometry_runtime_options(phot_run, include_worker_backend=True)
    phot_run.set_defaults(func=cmd_photometry_run)
    phot_rerun = phot_sub.add_parser(
        "rerun",
        help="Force remeasure current-identity photometry rows",
        description=(
            "Shortcut for photometry run --force-rerun. Existing matching "
            "photometry rows are remeasured with the current config/code/schema; "
            "missing temporary cutouts may be downloaded again."
        ),
    )
    add_common(phot_rerun)
    phot_rerun.add_argument("--source-name", help="Restrict the rerun to one catalog source_name")
    add_photometry_runtime_options(phot_rerun, include_worker_backend=True)
    phot_rerun.set_defaults(func=cmd_photometry_rerun)
    phot_summarize = phot_sub.add_parser("summarize", help="Write a catalog-level photometry summary CSV")
    add_common(phot_summarize)
    phot_summarize.set_defaults(func=cmd_photometry_summarize)
    phot_clean = phot_sub.add_parser("clean", help="Remove old photometry temporary files")
    add_common(phot_clean)
    phot_clean.add_argument("--older-than-hours", type=float, default=24.0)
    phot_clean.set_defaults(func=cmd_photometry_clean)
    phot_clean_results = phot_sub.add_parser(
        "clean-results",
        help="Remove photometry result rows and generated products so photometry can be rerun",
        description=(
            "Delete photometry measurements, work items, failures, summaries, "
            "output-product registry rows, and generated result files for selected "
            "sources. This does not delete downloaded cutout records or calibration."
        ),
    )
    add_common(phot_clean_results)
    phot_clean_results.add_argument("--source-name", help="Restrict cleanup to one catalog source_name")
    phot_clean_results.add_argument("--all", action="store_true", help="Clean photometry results for all sources")
    phot_clean_results.add_argument("--keep-files", action="store_true", help="Only clean database rows; leave files under results/")
    phot_clean_results.add_argument("--yes", action="store_true", help="Actually delete rows/files; without this use --dry-run")
    phot_clean_results.set_defaults(func=cmd_photometry_clean_results)
    phot_validate = phot_sub.add_parser("validate-results", help="Validate durable photometry output products")
    add_common(phot_validate)
    phot_validate.set_defaults(func=cmd_photometry_validate_results)

    run = sub.add_parser("run", help="Run the integrated catalog-to-spectrum workflow")
    add_common(run)
    run.add_argument(
        "--batch-config",
        type=Path,
        help="YAML override file for workflow/runtime/cleanup/cutouts/download/photometry batch settings",
    )
    run.add_argument("--catalog", type=Path, help="Catalog path override for this run")
    run.add_argument("--source-name", help="Restrict the run to one catalog source_name")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--discover", action="store_true", help="Run discovery before planning")
    run.add_argument("--update", action="store_true")
    run.add_argument("--collections")
    run.add_argument("--force-discovery", action="store_true")
    run.add_argument("--search-radius-arcsec", type=float)
    run.add_argument("--maxrec", type=int)
    run.add_argument("--concurrency", type=int)
    run.add_argument("--tap-fallback", action="store_true")
    run.add_argument("--mock-sia", type=Path)
    run.add_argument("--download-missing", dest="download_missing", action="store_true", default=None)
    run.add_argument("--no-download", dest="download_missing", action="store_false")
    run.add_argument("--sync-calibration", action="store_true")
    run.add_argument("--no-sync-calibration", dest="sync_calibration", action="store_false")
    run.add_argument("--skip-valid-measurements", dest="skip_valid_measurements", action="store_true", default=None)
    run.add_argument("--no-skip-valid-measurements", dest="skip_valid_measurements", action="store_false")
    run.add_argument("--regenerate-missing-outputs", dest="regenerate_missing_outputs", action="store_true", default=None)
    run.add_argument("--no-regenerate-missing-outputs", dest="regenerate_missing_outputs", action="store_false")
    run.add_argument("--force-photometry-rerun", action="store_true")
    run.add_argument(
        "--cleanup-cutouts",
        choices=["never", "success-after-measurement", "success-after-source", "success-after-run", "successful", "none"],
    )
    run.add_argument("--keep-failed-cutouts", dest="keep_failed_cutouts", action="store_true", default=None)
    run.add_argument("--delete-failed-cutouts", dest="keep_failed_cutouts", action="store_false")
    run.add_argument("--max-download-workers", type=int)
    run.add_argument("--max-fit-workers", type=int)
    run.add_argument("--max-source-workers", type=int)
    run.add_argument("--max-inflight-cutouts", type=int)
    run.add_argument("--max-live-cutout-gb", type=float)
    run.add_argument("--max-open-fits-files", type=int)
    run.add_argument("--qa-level", choices=["minimal", "standard", "full"])
    run.add_argument("--qa-workers", type=int, help="Parallel worker processes for full per-measurement QA PNG writing")
    run.add_argument("--qa-dpi", type=int, help="DPI for full per-measurement QA PNG writing")
    run.add_argument("--qa-colorbars", action="store_true", help="Draw per-panel colorbars in full QA PNGs; slower than the default scale labels")
    run.add_argument("--no-progress", action="store_true")
    run.set_defaults(func=cmd_run)

    summary_cmd = sub.add_parser("summary", help="Summarize integrated workflow completeness and outputs")
    add_common(summary_cmd)
    summary_cmd.add_argument("--rebuild-missing-outputs", action="store_true")
    summary_cmd.add_argument("--failed-only", action="store_true")
    summary_cmd.add_argument("--format", choices=["table", "csv", "json"], default="table")
    summary_cmd.add_argument("--output", type=Path)
    summary_cmd.set_defaults(func=cmd_summary)

    sync = sub.add_parser("sync", help="Run catalog ingest, discovery, planning, download, validation, and manifests")
    add_common(sync)
    sync.add_argument("--skip-discovery", action="store_true")
    sync.add_argument("--skip-download", action="store_true")
    sync.add_argument("--skip-validation", action="store_true")
    sync.add_argument("--force-discovery", action="store_true")
    sync.add_argument("--force-plan", action="store_true")
    sync.add_argument("--max-downloads", type=int)
    sync.add_argument("--concurrency", type=int)
    add_download_runtime_options(sync)
    sync.add_argument("--no-progress", action="store_true")
    sync.add_argument("--tap-fallback", action="store_true")
    sync.add_argument("--mock-sia", type=Path)
    sync.add_argument("--format", action="append", default=[])
    sync.set_defaults(func=cmd_sync)

    return parser


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path("."), help="Project directory containing spherex_cutoutdb.yaml")
    parser.add_argument("--config", type=Path, help="Optional config YAML path; defaults to PROJECT/spherex_cutoutdb.yaml")
    parser.add_argument("--verbose", action="store_true", help="Print per-source/per-file progress details")
    parser.add_argument("--quiet", action="store_true", help="Suppress nonessential terminal output")
    parser.add_argument("--log-level", default=None, help="Python logging level override")
    parser.add_argument("--dry-run", action="store_true", help="Plan/report without executing the command's main side effects")
    parser.add_argument("--limit-sources", type=int, help="Limit active sources processed")
    parser.add_argument("--source-id", action="append", help="Restrict to one source_id; may be repeated")
    parser.add_argument("--run-id", help="Explicit run identifier for provenance")


def add_download_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-workers", type=int, help="File-level downloader workers")
    parser.add_argument("--per-host-rate-limit", type=float, help="Maximum request starts per host per second; 0 disables")
    parser.add_argument("--per-host-max-concurrency", type=int, help="Maximum simultaneous downloads per host")
    parser.add_argument("--min-rate-mib-per-sec", type=float, help="Abort/requeue transfers below this sustained MiB/s")
    parser.add_argument("--low-speed-time", type=float, help="Seconds a transfer may stay below --min-rate-mib-per-sec")
    parser.add_argument("--retry-count", type=int, help="Maximum HTTP/network attempts per file")
    parser.add_argument("--timeout", type=float, help="Read timeout in seconds")
    parser.add_argument("--overwrite", action="store_true", help="Redownload even when a local file exists")
    parser.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=None, help="Validate and reuse existing local files")
    parser.add_argument("--no-skip-existing", dest="skip_existing", action="store_false", help="Do not skip existing local files")


def add_calibration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--product", action="append", default=["required"], help="Calibration product to sync: required, spectral_wcs, or solid_angle_pixel_map")
    parser.add_argument("--products", help="Comma-separated calibration products; alternative to repeated --product")
    parser.add_argument("--detectors", help="Comma-separated detector IDs, e.g. 1,2,3 or D1,D2")
    parser.add_argument("--input-dir", type=Path, help="Import calibration FITS files from an existing directory tree")
    parser.add_argument("--max-workers", type=int, help="Parallel calibration download workers")
    parser.add_argument("--download-source", choices=["auto", "cloud", "ibe"], help="Calibration download source; auto probes cloud and IRSA IBE and uses the faster route")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Product URL template, e.g. spectral_wcs=https://.../D{detector}/file.fits; may be repeated",
    )


def add_photometry_runtime_options(parser: argparse.ArgumentParser, *, include_worker_backend: bool = False) -> None:
    parser.add_argument("--qa-level", choices=["minimal", "standard", "full"], help="Amount of QA plotting to write")
    parser.add_argument("--qa-workers", type=int, help="Parallel worker processes for full per-measurement QA PNG writing")
    parser.add_argument("--qa-dpi", type=int, help="DPI for full per-measurement QA PNG writing")
    parser.add_argument("--qa-colorbars", action="store_true", help="Draw per-panel colorbars in full QA PNGs; slower than the default scale labels")
    parser.add_argument("--force-rerun", action="store_true", help="Remeasure matching current-identity photometry rows instead of skipping them")
    parser.add_argument("--max-source-workers", type=int, help="Parallel photometry worker count for multi-source runs")
    if include_worker_backend:
        parser.add_argument(
            "--worker-backend",
            choices=["process", "thread"],
            default="process",
            help="Execution backend for multi-worker photometry run; process uses OS processes, thread keeps the legacy source-thread scheduler",
        )
    parser.add_argument("--cleanup-cutouts", choices=["successful", "none"], help="Delete successful temporary cutouts after durable outputs validate, or keep all")
    parser.add_argument("--retain-cutouts", choices=["failed", "all"], help="Retain failed-only cutouts or all cutouts for debugging")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bars")


def _load(args) -> tuple[Any, Any, Any]:
    overrides: dict[str, Any] = _load_batch_config_overrides(getattr(args, "batch_config", None))
    if getattr(args, "catalog", None):
        overrides.setdefault("catalog", {})["path"] = str(args.catalog)
    if getattr(args, "search_radius_arcsec", None):
        overrides.setdefault("discovery", {})["search_radius_arcsec"] = args.search_radius_arcsec
    if getattr(args, "maxrec", None):
        overrides.setdefault("discovery", {})["maxrec_per_source_collection"] = args.maxrec
    if getattr(args, "redownload_invalid", None) is not None:
        overrides.setdefault("planning", {})["redownload_invalid"] = args.redownload_invalid
    if getattr(args, "concurrency", None):
        if getattr(args, "command", None) == "discover":
            overrides.setdefault("discovery", {})["concurrency"] = args.concurrency
        elif getattr(args, "command", None) == "sync":
            overrides.setdefault("discovery", {})["concurrency"] = args.concurrency
            overrides.setdefault("download", {})["concurrency"] = args.concurrency
            overrides.setdefault("download", {})["max_workers"] = args.concurrency
        else:
            overrides.setdefault("download", {})["concurrency"] = args.concurrency
            overrides.setdefault("download", {})["max_workers"] = args.concurrency
    if getattr(args, "max_workers", None):
        if getattr(args, "command", None) in {"calibration", "calib"}:
            overrides.setdefault("calibration", {})["download_max_workers"] = args.max_workers
        else:
            overrides.setdefault("download", {})["max_workers"] = args.max_workers
    if getattr(args, "download_source", None):
        overrides.setdefault("calibration", {})["download_source"] = args.download_source
    if getattr(args, "per_host_rate_limit", None) is not None:
        overrides.setdefault("download", {})["per_host_rate_limit_per_second"] = args.per_host_rate_limit
    if getattr(args, "per_host_max_concurrency", None) is not None:
        overrides.setdefault("download", {})["per_host_max_concurrency"] = args.per_host_max_concurrency
    if getattr(args, "min_rate_mib_per_sec", None) is not None:
        overrides.setdefault("download", {})["min_download_rate_bytes_per_second"] = (
            args.min_rate_mib_per_sec * 1024 * 1024
        )
    if getattr(args, "low_speed_time", None) is not None:
        overrides.setdefault("download", {})["low_speed_time_sec"] = args.low_speed_time
    if getattr(args, "retry_count", None):
        overrides.setdefault("download", {}).setdefault("retry", {})["attempts"] = args.retry_count
    if getattr(args, "timeout", None):
        overrides.setdefault("download", {})["read_timeout_sec"] = args.timeout
    if getattr(args, "overwrite", False):
        overrides.setdefault("download", {})["overwrite_existing"] = True
    if getattr(args, "skip_existing", None) is not None:
        overrides.setdefault("download", {})["skip_existing"] = args.skip_existing
    if getattr(args, "qa_level", None):
        overrides.setdefault("photometry", {})["qa_level"] = args.qa_level
    if getattr(args, "qa_workers", None):
        overrides.setdefault("photometry", {}).setdefault("qa", {})["full_plot_workers"] = args.qa_workers
    if getattr(args, "qa_dpi", None):
        overrides.setdefault("photometry", {}).setdefault("qa", {})["measurement_plot_dpi"] = args.qa_dpi
    if getattr(args, "qa_colorbars", False):
        overrides.setdefault("photometry", {}).setdefault("qa", {})["measurement_plot_colorbars"] = True
    if getattr(args, "max_source_workers", None):
        overrides.setdefault("runtime", {})["max_source_workers"] = args.max_source_workers
    if getattr(args, "max_download_workers", None):
        overrides.setdefault("runtime", {})["max_download_workers"] = args.max_download_workers
        overrides.setdefault("download", {})["max_workers"] = args.max_download_workers
        overrides.setdefault("download", {})["concurrency"] = args.max_download_workers
    if getattr(args, "max_fit_workers", None):
        overrides.setdefault("runtime", {})["max_fit_workers"] = args.max_fit_workers
    if getattr(args, "max_inflight_cutouts", None):
        overrides.setdefault("runtime", {})["max_inflight_cutouts"] = args.max_inflight_cutouts
    if getattr(args, "max_live_cutout_gb", None) is not None:
        overrides.setdefault("runtime", {})["max_live_cutout_gb"] = args.max_live_cutout_gb
    if getattr(args, "max_open_fits_files", None):
        overrides.setdefault("runtime", {})["max_open_fits_files"] = args.max_open_fits_files
    if getattr(args, "download_missing", None) is not None:
        overrides.setdefault("workflow", {})["download_missing"] = args.download_missing
    if getattr(args, "skip_valid_measurements", None) is not None:
        overrides.setdefault("workflow", {})["skip_valid_measurements"] = args.skip_valid_measurements
    if getattr(args, "regenerate_missing_outputs", None) is not None:
        overrides.setdefault("workflow", {})["regenerate_missing_outputs"] = args.regenerate_missing_outputs
    if getattr(args, "cleanup_cutouts", None) in {"never", "success-after-measurement", "success-after-source", "success-after-run"}:
        overrides.setdefault("cleanup", {})["cutouts"] = args.cleanup_cutouts
    if getattr(args, "keep_failed_cutouts", None) is not None:
        overrides.setdefault("cleanup", {})["keep_failed_cutouts"] = args.keep_failed_cutouts
    if getattr(args, "cleanup_cutouts", None) == "none":
        overrides.setdefault("photometry", {}).setdefault("cleanup", {})["delete_successful_cutouts"] = False
    if getattr(args, "retain_cutouts", None) == "all":
        overrides.setdefault("photometry", {}).setdefault("cleanup", {})["delete_successful_cutouts"] = False
        overrides.setdefault("photometry", {}).setdefault("cleanup", {})["keep_failed_cutouts"] = True
    setattr(args, "_effective_cli_overrides", overrides)
    config = load_config(args.project, args.config, overrides or None)
    ensure_project_directories(config)
    configure_logging(config, args.log_level)
    console = make_console(config, quiet=getattr(args, "quiet", False))
    conn = connect(config.project.database_path)
    initialize_schema(conn)
    return config, conn, console


def _load_batch_config_overrides(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"batch config does not exist: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("batch config must be a YAML mapping")
    allowed = {
        "workflow",
        "runtime",
        "cleanup",
        "cutouts",
        "download",
        "discovery",
        "calibration",
        "photometry",
        "planning",
        "logging",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"unsupported batch config section(s): {', '.join(unknown)}")
    return data


def cmd_init(args) -> int:
    project = Path(args.project_option or args.project_path).expanduser().resolve()
    config_path = write_default_config(
        project,
        args.catalog,
        force=args.force,
        default_cutout_size_arcsec=args.default_cutout_size_arcsec,
        include_deep=args.include_deep,
        target_id_column=args.target_id_column,
        ra_column=args.ra_column,
        dec_column=args.dec_column,
    )
    config = load_config(project, config_path)
    ensure_project_directories(config)
    conn = connect(config.project.database_path)
    initialize_schema(conn)
    conn.close()
    print(f"Created project: {project}")
    print(f"Wrote config: {config_path}")
    print(f"Initialized database: {config.project.database_path}")
    print(f"Next: spxcutdb validate --project {project} --catalog {args.catalog or config.catalog.path}")
    return 0


def cmd_config_show(args) -> int:
    config, conn, console = _load(args)
    payload = config.model_dump(mode="json")
    if args.format == "json":
        console.print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        console.print(yaml.safe_dump(payload, sort_keys=False))
    if args.hash:
        console.print(f"config_hash: {config_hash(config)}")
    conn.close()
    return 0


def cmd_config_validate(args) -> int:
    config, conn, console = _load(args)
    errors = validate_effective_config(config)
    if errors:
        for error in errors:
            console.print(f"ERROR: {error}")
        conn.close()
        return 2
    console.print("Configuration valid")
    console.print(f"config_hash: {config_hash(config)}")
    conn.close()
    return 0


def cmd_config_defaults(args) -> int:
    payload = Config().model_dump(mode="json")
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(yaml.safe_dump(payload, sort_keys=False))
    return 0


def cmd_config_diff(args) -> int:
    config, conn, console = _load(args)
    default_payload = Config().model_dump(mode="json")
    current_payload = config.model_dump(mode="json")
    diffs = _config_diffs(default_payload, current_payload)
    if args.format == "json":
        console.print(json.dumps(diffs, indent=2, sort_keys=True))
    elif not diffs:
        console.print("No differences from defaults")
    else:
        for item in diffs:
            console.print(f"{item['path']}: {item['default']!r} -> {item['current']!r}")
    conn.close()
    return 0


def _config_diffs(default: Any, current: Any, prefix: str = "") -> list[dict[str, Any]]:
    if isinstance(default, dict) and isinstance(current, dict):
        out: list[dict[str, Any]] = []
        for key in sorted(set(default) | set(current)):
            path = f"{prefix}.{key}" if prefix else str(key)
            out.extend(_config_diffs(default.get(key), current.get(key), path))
        return out
    if default != current:
        return [{"path": prefix, "default": default, "current": current}]
    return []


def cmd_catalog_validate(args) -> int:
    config, conn, console = _load(args)
    raw = read_source_catalog(config)
    normalized = normalize_sources(raw, config)
    report = validate_sources(normalized, config)
    table = Table(title="Catalog validation")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("input rows", str(report.n_rows_input))
    table.add_row("valid rows", str(report.n_rows_valid))
    table.add_row("invalid rows", str(report.n_rows_invalid))
    table.add_row("warnings", str(len(report.warnings)))
    table.add_row("errors", str(len(report.errors)))
    console.print(table)
    for warning in report.warnings:
        console.print(f"warning: {warning}")
    for error in report.errors:
        console.print(f"error: {error}")
    conn.close()
    return 0 if report.valid else 3


def cmd_catalog_ingest(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "catalog ingest", vars(args), config)
    catalog_version_id, report, stats = ingest_catalog(conn, config, run_id)
    finish_run(conn, run_id, "success", {"catalog_version_id": catalog_version_id, **stats})
    console.print(f"Ingested catalog {catalog_version_id}: {stats}")
    conn.close()
    return 0 if report.valid else 3


def cmd_discover(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "discover", vars(args), config)
    if not _active_sources(conn, None, 1):
        catalog_version_id, report, stats = ingest_catalog(conn, config, run_id)
        if args.verbose:
            console.print(f"Ingested catalog before discovery: {catalog_version_id} {stats}")
        if not report.valid:
            finish_run(conn, run_id, "failed", {"catalog_errors": report.errors})
            conn.close()
            return 3
    if getattr(args, "source_name", None):
        row = conn.execute("SELECT source_id FROM sources WHERE source_name = ?", (args.source_name,)).fetchone()
        if row is None:
            conn.close()
            raise SpxCutoutDBError(f"source not found: {args.source_name}")
        args.source_id = list(args.source_id or []) + [row["source_id"]]
    counts = _run_discovery(conn, config, run_id, args, console)
    status = "partial_success" if counts["failures"] else "success"
    finish_run(conn, run_id, status, counts)
    console.print(f"Discovery complete: {counts}")
    conn.close()
    return 1 if counts["failures"] else 0


def _run_discovery(conn, config, run_id: str | None, args, console) -> dict[str, int]:
    sources = _active_sources(conn, args.source_id, args.limit_sources)
    collections = _parse_collections(args.collections) if getattr(args, "collections", None) else None
    counts = {"sources": len(sources), "products": 0, "matches": 0, "failures": 0}
    selected_collections = collections or config.discovery.collections
    for source in sources:
        deactivate_source_product_matches(conn, source["source_id"], selected_collections)

    workers = max(1, int(config.discovery.concurrency))
    if args.verbose:
        console.print(
            f"Discovering {len(sources)} source(s) with concurrency={workers}; "
            f"collections={','.join(selected_collections)}"
        )
    completed = _discover_iter(
        sources,
        config,
        run_id,
        collections,
        workers,
        getattr(args, "mock_sia", None),
    )
    show_progress = (
        bool(config.logging.progress_bars)
        and not getattr(args, "quiet", False)
        and not getattr(args, "verbose", False)
    )
    if show_progress:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("products={task.fields[products]} failures={task.fields[failures]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as bar:
            task_id = bar.add_task(
                "Discovering sources",
                total=len(sources),
                products=0,
                failures=0,
            )
            for source, result in completed:
                _record_discovery_result(conn, config, run_id, source, result, counts, args, console)
                bar.update(
                    task_id,
                    advance=1,
                    products=counts["products"],
                    failures=counts["failures"],
                )
    else:
        for source, result in completed:
            _record_discovery_result(conn, config, run_id, source, result, counts, args, console)
    return counts


def _discover_iter(sources, config, run_id, collections, workers, mock_sia):
    if workers <= 1 or len(sources) <= 1:
        for source in sources:
            result = discover_for_source(
                source,
                config,
                run_id,
                None,
                collections=collections,
                mock_sia=mock_sia,
            )
            yield source, result
        return

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                discover_for_source,
                source,
                config,
                run_id,
                None,
                collections=collections,
                mock_sia=mock_sia,
            ): source
            for source in sources
        }
        for future in as_completed(futures):
            yield futures[future], future.result()


def _record_discovery_result(conn, config, run_id, source, result, counts, args, console) -> None:
    if args.verbose:
        console.print(
            f"Source {source['source_id']} RA={source['ra_deg']} Dec={source['dec_deg']}"
        )
        if result.rows.empty:
            console.print(f"  no candidate parent MEFs")
        else:
            grouped = result.rows.groupby("collection").size().to_dict()
            for collection, n_rows in grouped.items():
                console.print(f"  {collection}: {n_rows} candidate parent MEFs")
    if not result.rows.empty:
        product_ids = upsert_discovery_products(conn, run_id, result.rows)
        matches = build_match_rows(source, result.rows, product_ids, config)
        upsert_source_product_matches(conn, run_id, matches)
        counts["products"] += len(result.rows)
        counts["matches"] += len(matches)
    for failure in result.failures:
        failure["run_id"] = run_id
        record_failure(conn, failure)
        counts["failures"] += 1


def _parse_collections(value: str) -> list[str]:
    collections = [item.strip() for item in value.split(",") if item.strip()]
    excluded = sorted(set(collections) & EXCLUDED_CUTOUT_COLLECTIONS)
    unsupported = sorted(set(collections) - ALLOWED_CUTOUT_COLLECTIONS)
    if excluded:
        raise ConfigError(f"excluded collection is not valid for cutout discovery: {', '.join(excluded)}")
    if unsupported:
        raise ConfigError(f"unsupported discovery collection(s): {', '.join(unsupported)}")
    if not collections:
        raise ConfigError("at least one discovery collection is required")
    return collections


def cmd_plan(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "plan", vars(args), config)
    source_ids = args.source_id
    if args.source_name:
        row = conn.execute("SELECT source_id FROM sources WHERE source_name = ?", (args.source_name,)).fetchone()
        if row is None:
            raise SpxCutoutDBError(f"source not found: {args.source_name}")
        source_ids = [row["source_id"]]
    plan_df = plan_downloads(conn, run_id, config, source_ids)
    counts = plan_df["action"].value_counts().to_dict() if not plan_df.empty else {}
    finish_run(conn, run_id, "success", counts)
    console.print(f"Download plan: {counts}")
    if args.export_plan:
        export_manifests(conn, run_id, config, formats=["csv"], tables=["plan"])
    conn.close()
    return 0


def cmd_download(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "download", vars(args), config)
    if args.dry_run:
        n = count_planned_downloads(
            conn,
            run_id,
            args.max_downloads,
            overwrite=bool(config.download.overwrite_existing),
        )
        console.print(f"Dry run: {n} planned downloads would be attempted")
        finish_run(conn, run_id, "success", {"dry_run_downloads": n})
        conn.close()
        return 0
    summary = run_download_plan(
        conn,
        run_id,
        config,
        args.max_downloads,
        console=console,
        progress=bool(config.logging.progress_bars) and not getattr(args, "no_progress", False) and not args.quiet,
        verbose=args.verbose,
    )
    finish_run(conn, run_id, "partial_success" if summary.failed else "success", asdict(summary))
    _print_download_summary(console, summary)
    print_summary(conn, run_id, config, console)
    conn.close()
    return 1 if summary.failed else 0


def cmd_validate(args) -> int:
    config, conn, console = _load(args)
    if getattr(args, "catalog", None):
        checks = validate_project_catalog(config)
        table = Table(title="Project/catalog preflight")
        table.add_column("Check")
        table.add_column("Value")
        for key in [
            "catalog_path",
            "target_id_column",
            "source_name_column",
            "ra_column",
            "dec_column",
            "n_rows_input",
            "n_rows_valid",
            "n_rows_invalid",
            "database_path",
            "cutout_dir",
            "calibration_cache",
            "download_workers",
            "fit_workers",
            "max_inflight_cutouts",
            "max_live_cutout_gb",
        ]:
            table.add_row(key, str(checks.get(key, "")))
        table.add_row("valid", str(checks["valid"]))
        console.print(table)
        for warning in checks.get("warnings", []):
            console.print(f"warning: {warning}")
        for error in checks.get("errors", []):
            console.print(f"error: {error}")
        duplicates = checks.get("duplicate_target_ids") or []
        if duplicates:
            console.print("duplicate target ids: " + ", ".join(duplicates[:12]))
        conn.close()
        return 0 if checks["valid"] else 3
    return cmd_validate_cutouts_loaded(args, config, conn, console)


def cmd_validate_cutouts(args) -> int:
    config, conn, console = _load(args)
    return cmd_validate_cutouts_loaded(args, config, conn, console)


def cmd_validate_cutouts_loaded(args, config, conn, console) -> int:
    run_id = start_run(conn, "validate", vars(args), config)
    rows = _validation_targets(conn, config, args)
    counts = {"passed": 0, "passed_with_warnings": 0, "failed": 0}
    for cutout_id, local_path in rows:
        path = resolve_project_path(config, local_path)
        result = validate_cutout(path, config)
        status_key = result.status if result.status in counts else "failed"
        counts[status_key] += 1
        if args.update_db:
            record_validation(
                conn,
                {
                    "run_id": run_id,
                    "cutout_id": cutout_id,
                    "local_path": _rel_or_str(path, config),
                    "status": result.status,
                    "reason": result.reason,
                    "warnings": result.warnings,
                    "errors": result.errors,
                    "file_size_bytes": result.file_size_bytes,
                    "sha256": result.sha256,
                    "required_hdus_present": result.required_hdus_present,
                    "image_shape": result.image_shape,
                    "flags_shape": result.flags_shape,
                    "variance_shape": result.variance_shape,
                    "zodi_shape": result.zodi_shape,
                    "psf_shape": result.psf_shape,
                    "wcwave_summary": result.wcwave_summary,
                    "spatial_wcs_valid": result.wcs_summary.get("spatial_wcs_valid", False),
                    "spectral_wcs_valid": result.wcs_summary.get("spectral_wcs_valid", False),
                    "hdu_summary": result.hdu_summary,
                    "wcs_summary": result.wcs_summary,
                    "psf_metadata": result.psf_metadata,
                    "header_metadata": result.header_metadata,
                },
            )
            if not result.status.startswith("passed"):
                record_failure(
                    conn,
                    {
                        "run_id": run_id,
                        "cutout_id": cutout_id,
                        "phase": "validation",
                        "status": "open",
                        "reason": result.reason,
                        "local_path": _rel_or_str(path, config),
                    },
                )
    finish_run(conn, run_id, "partial_success" if counts["failed"] else "success", counts)
    console.print(f"Validated {sum(counts.values())} files: {counts}")
    conn.close()
    return 1 if counts["failed"] else 0


def cmd_update_db(args) -> int:
    config, conn, console = _load(args)
    rows = conn.execute("SELECT cutout_id, local_path FROM cutouts").fetchall()
    updated = 0
    for row in rows:
        path = resolve_project_path(config, row["local_path"])
        exists = path.exists()
        size = path.stat().st_size if exists else None
        conn.execute(
            "UPDATE cutouts SET file_exists = ?, file_size_bytes = ? WHERE cutout_id = ?",
            (1 if exists else 0, size, row["cutout_id"]),
        )
        updated += 1
    conn.commit()
    console.print(f"Refreshed file metadata for {updated} cutouts")
    conn.close()
    return 0


def cmd_coverage(args) -> int:
    config, conn, console = _load(args)
    df = coverage_dataframe(conn, active_only=args.active_only)
    if args.failed_only:
        df = df[df["n_failed_cutouts"] > 0]
    if args.no_coverage_only:
        df = df[df["coverage_status"] == "not_covered"]
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "json":
            df.to_json(args.output, orient="records", indent=2)
        else:
            df.to_csv(args.output, index=False)
    elif args.format == "json":
        console.print(df.to_json(orient="records", indent=2))
    elif args.format == "csv":
        console.print(df.to_csv(index=False))
    else:
        table = Table(title="SPHEREx coverage")
        for column in [
            "source_id",
            "source_name",
            "n_discovered_parent_mefs",
            "n_valid_cutouts",
            "n_failed_cutouts",
            "n_detectors",
            "detectors",
            "coverage_status",
        ]:
            table.add_column(column)
        for _, row in df.iterrows():
            table.add_row(*(str(row.get(column, "")) for column in [
                "source_id",
                "source_name",
                "n_discovered_parent_mefs",
                "n_valid_cutouts",
                "n_failed_cutouts",
                "n_detectors",
                "detectors",
                "coverage_status",
            ]))
        console.print(table)
    conn.close()
    return 0


def cmd_retry_failed(args) -> int:
    args.dry_run = getattr(args, "dry_run", False)
    return cmd_download(args)


def cmd_clean_partials(args) -> int:
    config, conn, console = _load(args)
    cutoff = time.time() - args.older_than_hours * 3600.0
    partial_dir = config.project.data_root / "partial"
    removed = 0
    for path in partial_dir.glob(f"*{config.download.partial_suffix}"):
        if path.stat().st_mtime <= cutoff:
            if args.dry_run:
                console.print(f"Would remove {path}")
            else:
                path.unlink()
            removed += 1
    console.print(f"{'Would remove' if args.dry_run else 'Removed'} {removed} partial files")
    conn.close()
    return 0


def cmd_export_manifest(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "export-manifest", vars(args), config)
    formats = args.format or config.exports.formats
    paths = export_manifests(conn, run_id, config, formats=formats, tables=args.table, output_dir=args.output_dir)
    finish_run(conn, run_id, "success", {"exports": len(paths)})
    console.print(f"Exported {len(paths)} manifest files")
    conn.close()
    return 0


def cmd_calibration_sync(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "calibration sync", vars(args), config)
    products = _parse_products(args)
    summary = sync_calibrations(
        conn,
        config,
        products=products,
        detectors=_parse_detectors(args.detectors),
        input_dir=args.input_dir,
        urls=_parse_url_templates(args.url),
    )
    counts = {
        "imported": summary.imported,
        "downloaded": summary.downloaded,
        "skipped_existing": summary.skipped_existing,
        "validated": summary.validated,
        "valid": summary.valid,
        "failed": summary.failed,
        "missing": len(summary.missing),
    }
    finish_run(conn, run_id, "partial_success" if summary.failed or summary.missing else "success", counts)
    console.print(f"Calibration sync: {counts}")
    if summary.missing:
        console.print("Missing required calibration: " + ", ".join(summary.missing[:12]))
    conn.close()
    return 1 if summary.failed or summary.missing else 0


def cmd_calibration_status(args) -> int:
    config, conn, console = _load(args)
    rows = conn.execute(
        """
        SELECT release, product_type, detector_id, validation_status, COUNT(*) AS n
        FROM calibration_products
        GROUP BY release, product_type, detector_id, validation_status
        ORDER BY release, product_type, detector_id, validation_status
        """
    ).fetchall()
    table = Table(title="Calibration cache")
    for column in ["release", "product", "detector", "status", "count"]:
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["release"]),
            str(row["product_type"]),
            str(row["detector_id"]),
            str(row["validation_status"]),
            str(row["n"]),
        )
    console.print(table)
    conn.close()
    return 0


def cmd_calibration_validate(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "calibration validate", vars(args), config)
    summary = validate_cached_calibrations(conn, config, products=_parse_products(args))
    counts = {"validated": summary.validated, "valid": summary.valid, "failed": summary.failed}
    finish_run(conn, run_id, "partial_success" if summary.failed else "success", counts)
    console.print(f"Calibration validation: {counts}")
    conn.close()
    return 1 if summary.failed else 0


def cmd_photometry_plan(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "photometry plan", vars(args), config)
    photometry_run_id = start_photometry_run(conn, config, run_id)
    source_ids = args.source_id
    if args.source_name:
        row = conn.execute("SELECT source_id FROM sources WHERE source_name = ?", (args.source_name,)).fetchone()
        if row is None:
            raise SpxCutoutDBError(f"source not found: {args.source_name}")
        source_ids = [row["source_id"]]
    items, counts = plan_photometry(
        conn,
        config,
        source_ids=source_ids,
        photometry_run_id=photometry_run_id,
        force_remeasure=args.force_rerun,
    )
    finish_photometry_run(conn, photometry_run_id, "success", counts)
    finish_run(conn, run_id, "success", {"planned": len(items), **counts})
    console.print(f"Photometry plan: {counts}")
    conn.close()
    return 0


def cmd_photometry_source(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "photometry source", vars(args), config)
    source_id = args.source_id[-1] if args.source_id else None
    try:
        summary = run_source_photometry(
            conn,
            config,
            source_id=source_id,
            source_name=args.source_name,
            run_id=run_id,
            qa_level=args.qa_level,
            progress=bool(config.logging.progress_bars) and not getattr(args, "no_progress", False) and not args.quiet,
            verbose=args.verbose,
            console=console,
            force_remeasure=args.force_rerun,
        )
        finish_run(conn, run_id, "partial_success" if summary.failed else "success", asdict(summary))
    except Exception as exc:  # noqa: BLE001 - command-level failure
        finish_run(conn, run_id, "failed", {"reason": str(exc)})
        raise
    _print_photometry_summary(console, "Photometry source", summary)
    conn.close()
    return 1 if summary.failed else 0


def cmd_photometry_run(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, f"photometry {args.photometry_command}", vars(args), config)
    source_ids = args.source_id
    if args.source_name:
        row = conn.execute("SELECT source_id FROM sources WHERE source_name = ?", (args.source_name,)).fetchone()
        if row is None:
            raise SpxCutoutDBError(f"source not found: {args.source_name}")
        source_ids = [row["source_id"]]
    try:
        summary = run_photometry(
            conn,
            config,
            source_ids=source_ids,
            limit_sources=args.limit_sources,
            run_id=run_id,
            qa_level=args.qa_level,
            progress=bool(config.logging.progress_bars) and not getattr(args, "no_progress", False) and not args.quiet,
            verbose=args.verbose,
            console=console,
            max_source_workers=args.max_source_workers,
            force_remeasure=args.force_rerun,
            worker_backend=getattr(args, "worker_backend", "process"),
        )
        finish_run(conn, run_id, "partial_success" if summary.failed else "success", asdict(summary))
    except Exception as exc:  # noqa: BLE001 - command-level failure
        finish_run(conn, run_id, "failed", {"reason": str(exc)})
        raise
    _print_photometry_summary(console, "Photometry run", summary)
    conn.close()
    return 1 if summary.failed else 0


def cmd_photometry_rerun(args) -> int:
    args.force_rerun = True
    return cmd_photometry_run(args)


def cmd_photometry_summarize(args) -> int:
    config, conn, console = _load(args)
    path = summarize_photometry(conn, config)
    console.print(f"Wrote photometry catalog summary: {_rel_or_str(path, config)}")
    conn.close()
    return 0


def cmd_photometry_clean(args) -> int:
    config, conn, console = _load(args)
    cutoff = time.time() - args.older_than_hours * 3600.0
    roots = [config.project.cache_root / "photometry_scratch", config.photometry.output_root]
    removed = 0
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.tmp"):
            if path.stat().st_mtime <= cutoff:
                if args.dry_run:
                    console.print(f"Would remove {path}")
                else:
                    path.unlink()
                removed += 1
    console.print(f"{'Would remove' if args.dry_run else 'Removed'} {removed} photometry temporary files")
    conn.close()
    return 0


def cmd_photometry_clean_results(args) -> int:
    config, conn, console = _load(args)
    source_ids = _resolve_clean_result_source_ids(conn, args)
    if not source_ids:
        conn.close()
        raise SpxCutoutDBError("no sources selected; use --source-id, --source-name, --limit-sources, or --all")
    if not args.dry_run and not args.yes:
        conn.close()
        raise SpxCutoutDBError("clean-results is destructive; rerun with --dry-run or add --yes")

    stats = _clean_photometry_results(
        conn,
        config,
        source_ids=source_ids,
        delete_files=not args.keep_files,
        dry_run=args.dry_run,
        console=console,
    )
    action = "Would clean" if args.dry_run else "Cleaned"
    console.print(
        f"{action} photometry results: sources={stats['sources']} "
        f"measurements={stats['measurements']} work_items={stats['work_items']} "
        f"failures={stats['failures']} outputs={stats['output_products']} "
        f"summaries={stats['summaries']} files={stats['files']}"
    )
    conn.close()
    return 0


def cmd_photometry_validate_results(args) -> int:
    config, conn, console = _load(args)
    rows = conn.execute("SELECT * FROM photometry_source_summaries ORDER BY source_id").fetchall()
    checked = 0
    failed = 0
    for row in rows:
        source = dict(conn.execute("SELECT * FROM sources WHERE source_id = ?", (row["source_id"],)).fetchone())
        manifest_paths = source_output_paths(config, source)
        paths = {
            "csv": _project_path(config, row["spectrum_path"]),
            "sed": _project_path(config, row["sed_plot_path"]),
            "qa": _project_path(config, row["qa_summary_path"]),
            "provenance": _project_path(config, row["provenance_path"]),
            "index": _project_path(config, row["measurement_index_path"]),
            "manifest": manifest_paths["manifest"],
        }
        measurements = _measurements_for_output_validation(conn, row["source_id"])
        checked += 1
        if not validate_source_outputs(paths, config=config, source=source, measurements=measurements):
            failed += 1
            console.print(f"invalid outputs for source {row['source_id']}")
    console.print(f"Validated photometry outputs: checked={checked} failed={failed}")
    conn.close()
    return 1 if failed else 0


def _measurements_for_output_validation(conn, source_id: str) -> list[Any]:
    rows = conn.execute(
        "SELECT row_json, provenance_json FROM photometry_measurements WHERE source_id = ? ORDER BY wavelength_um, measurement_id",
        (source_id,),
    ).fetchall()
    return [
        SimpleNamespace(
            row=json.loads(row["row_json"]) if row["row_json"] else {},
            provenance=json.loads(row["provenance_json"]) if row["provenance_json"] else {},
            qa_arrays={},
        )
        for row in rows
    ]


def cmd_run(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "run", vars(args), config)
    status = "success"
    try:
        should_discover = bool(args.discover or args.update or args.force_discovery)
        if should_discover:
            if not args.dry_run:
                catalog_version_id, report, stats = ingest_catalog(conn, config, run_id)
                if not report.valid:
                    finish_run(conn, run_id, "failed", {"catalog_errors": report.errors})
                    conn.close()
                    return 3
                if args.verbose:
                    console.print(f"Updated catalog before workflow run: {catalog_version_id} {stats}")
            if args.source_name:
                row = conn.execute("SELECT source_id FROM sources WHERE source_name = ?", (args.source_name,)).fetchone()
                if row is None:
                    raise SpxCutoutDBError(f"source not found: {args.source_name}")
                args.source_id = list(args.source_id or []) + [row["source_id"]]
            discovery_counts = _run_discovery(conn, config, run_id, args, console) if not args.dry_run else {"dry_run_discovery": 1}
            if args.verbose:
                console.print(f"Update discovery before workflow run: {discovery_counts}")
        elif not _has_selected_discovery_matches(conn, source_ids=args.source_id, source_name=args.source_name):
            recommendation = (
                f"spxcutdb discover --project {config.project.root} --catalog {config.catalog.path} --resume"
            )
            raise SpxCutoutDBError(
                "no active SPHEREx discovery matches are available for this run. "
                f"Run discovery first or add --discover. Recommended command: {recommendation}"
            )
        summary = run_catalog_workflow(
            conn,
            config,
            run_id=run_id,
            source_ids=args.source_id,
            source_name=args.source_name,
            limit_sources=args.limit_sources,
            resume=args.resume,
            update=args.update,
            download_missing=args.download_missing,
            sync_calibration=args.sync_calibration,
            cleanup_cutouts=args.cleanup_cutouts,
            keep_failed_cutouts=args.keep_failed_cutouts,
            qa_level=args.qa_level,
            force_photometry_rerun=args.force_photometry_rerun or (
                args.skip_valid_measurements is False
            ),
            max_download_workers=args.max_download_workers,
            max_fit_workers=args.max_fit_workers,
            max_inflight_cutouts=args.max_inflight_cutouts,
            max_live_cutout_gb=args.max_live_cutout_gb,
            max_open_fits_files=args.max_open_fits_files,
            dry_run=args.dry_run,
            progress=bool(config.logging.progress_bars) and not getattr(args, "no_progress", False) and not args.quiet,
            verbose=args.verbose,
            console=console,
        )
        status = "partial_success" if (
            summary.download_failed or summary.fit_failed or summary.outputs_failed or summary.blocked
        ) else "success"
        finish_run(conn, run_id, status, asdict(summary), summary.summary_path)
    except Exception as exc:  # noqa: BLE001 - command-level failure is recorded
        finish_run(conn, run_id, "failed", {"reason": str(exc)})
        raise
    _print_workflow_summary(console, "Integrated workflow", summary)
    conn.close()
    return 0 if status == "success" else 1


def _has_selected_discovery_matches(conn, *, source_ids: list[str] | None, source_name: str | None) -> bool:
    params: list[Any] = []
    sql = """
        SELECT 1
        FROM source_product_matches spm
        JOIN sources s ON s.source_id = spm.source_id
        WHERE spm.active = 1 AND s.active = 1
    """
    selected = list(source_ids or [])
    if source_name:
        row = conn.execute("SELECT source_id FROM sources WHERE source_name = ? AND active = 1", (source_name,)).fetchone()
        if row is not None:
            selected.append(row["source_id"])
    if selected:
        placeholders = ",".join("?" for _ in selected)
        sql += f" AND spm.source_id IN ({placeholders})"
        params.extend(selected)
    sql += " LIMIT 1"
    return conn.execute(sql, tuple(params)).fetchone() is not None


def cmd_summary(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "summary", vars(args), config)
    summary = summarize_workflow_project(
        conn,
        config,
        rebuild_missing_outputs=args.rebuild_missing_outputs,
        run_id=run_id,
        console=console,
        verbose=args.verbose,
    )
    finish_run(conn, run_id, "success", asdict(summary), summary.summary_path)
    payload = asdict(summary)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "json":
            args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        else:
            pd = __import__("pandas")
            pd.DataFrame([payload]).to_csv(args.output, index=False)
    if args.format == "json" and not args.output:
        console.print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_workflow_summary(console, "Workflow summary", summary)
    conn.close()
    return 0


def cmd_sync(args) -> int:
    config, conn, console = _load(args)
    run_id = start_run(conn, "sync", vars(args), config)
    status = "success"
    counts: dict[str, Any] = {}
    try:
        catalog_version_id, report, stats = ingest_catalog(conn, config, run_id)
        counts.update({"catalog_version_id": catalog_version_id, **stats})
        if not args.skip_discovery:
            counts.update(_run_discovery(conn, config, run_id, args, console))
        plan_df = plan_downloads(conn, run_id, config, args.source_id)
        counts["planned"] = len(plan_df)
        if not args.skip_download and not args.dry_run:
            dl = run_download_plan(
                conn,
                run_id,
                config,
                args.max_downloads,
                console=console,
                progress=bool(config.logging.progress_bars)
                and not getattr(args, "no_progress", False)
                and not args.quiet,
                verbose=args.verbose,
            )
            counts.update({f"download_{k}": v for k, v in asdict(dl).items()})
            _print_download_summary(console, dl)
            if dl.failed:
                status = "partial_success"
        if not args.skip_validation:
            _validate_current_cutouts(conn, config, run_id)
        formats = args.format or config.exports.formats
        export_manifests(conn, run_id, config, formats=formats)
        summary_path = write_summary_json(conn, run_id, config)
        print_summary(conn, run_id, config, console)
        finish_run(conn, run_id, status, counts, _rel_or_str(summary_path, config))
    except Exception as exc:  # noqa: BLE001 - command-level failure is recorded
        record_failure(
            conn,
            {
                "run_id": run_id,
                "phase": "summary",
                "status": "open",
                "reason": str(exc),
                "exception_class": exc.__class__.__name__,
                "exception_message": str(exc),
            },
        )
        finish_run(conn, run_id, "failed", counts)
        raise
    finally:
        conn.close()
    return 0 if status == "success" else 1


def _active_sources(conn, source_ids: list[str] | None, limit: int | None) -> list[dict[str, Any]]:
    params: list[Any] = []
    clause = "WHERE active = 1"
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        clause += f" AND source_id IN ({placeholders})"
        params.extend(source_ids)
    sql = f"SELECT * FROM sources {clause} ORDER BY COALESCE(priority, 999999), source_id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _validation_targets(conn, config, args) -> list[tuple[int | None, str]]:
    if args.path:
        path = Path(args.path)
        if path.is_dir():
            files = sorted(path.rglob("*.fits"))
        else:
            files = [path]
        targets = []
        for file_path in files:
            rel = _rel_or_str(file_path.resolve(), config)
            row = conn.execute(
                "SELECT cutout_id FROM cutouts WHERE local_path = ? OR local_path = ?",
                (rel, str(file_path)),
            ).fetchone()
            targets.append((row["cutout_id"] if row else None, str(file_path)))
        return targets
    clause = ""
    if args.failed_only:
        clause = "WHERE validation_status LIKE 'failed%'"
    elif args.missing_only:
        clause = "WHERE file_exists = 0"
    return [(row["cutout_id"], row["local_path"]) for row in conn.execute(f"SELECT cutout_id, local_path FROM cutouts {clause}").fetchall()]


def _validate_current_cutouts(conn, config, run_id: str | None) -> None:
    args = argparse.Namespace(path=None, failed_only=False, missing_only=False, update_db=True)
    for cutout_id, local_path in _validation_targets(conn, config, args):
        path = resolve_project_path(config, local_path)
        result = validate_cutout(path, config)
        record_validation(
            conn,
            {
                "run_id": run_id,
                "cutout_id": cutout_id,
                "local_path": _rel_or_str(path, config),
                "status": result.status,
                "reason": result.reason,
                "warnings": result.warnings,
                "errors": result.errors,
                "file_size_bytes": result.file_size_bytes,
                "sha256": result.sha256,
                "required_hdus_present": result.required_hdus_present,
                "image_shape": result.image_shape,
                "flags_shape": result.flags_shape,
                "variance_shape": result.variance_shape,
                "zodi_shape": result.zodi_shape,
                "psf_shape": result.psf_shape,
                "wcwave_summary": result.wcwave_summary,
                "spatial_wcs_valid": result.wcs_summary.get("spatial_wcs_valid", False),
                "spectral_wcs_valid": result.wcs_summary.get("spectral_wcs_valid", False),
                "hdu_summary": result.hdu_summary,
                "wcs_summary": result.wcs_summary,
                "psf_metadata": result.psf_metadata,
                "header_metadata": result.header_metadata,
            },
        )


def _print_download_summary(console, summary) -> None:
    table = Table(title="Download target summary")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for label, value in [
        ("total targets", summary.total_targets),
        ("successful targets", summary.successful_targets),
        ("partially failed targets", summary.partially_failed_targets),
        ("failed targets", summary.failed_targets),
        ("planned files attempted", summary.attempted),
        ("skipped files", summary.skipped),
        ("downloaded files", summary.downloaded),
        ("failed files", summary.failed),
        ("total downloaded bytes", summary.bytes_downloaded),
    ]:
        table.add_row(label, str(value))
    console.print(table)


def _print_photometry_summary(console, title: str, summary) -> None:
    table = Table(title=f"{title} summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for label, value in [
        ("planned", summary.planned),
        ("skipped", summary.skipped),
        ("measured", summary.measured),
        ("failed", summary.failed),
        ("science recommended", summary.science_recommended),
        ("downloaded", summary.downloaded),
        ("deleted cutouts", summary.deleted_cutouts),
        ("qa plots planned", getattr(summary, "qa_plots_planned", 0)),
        ("qa plots written", getattr(summary, "qa_plots_written", 0)),
        ("qa plots failed", getattr(summary, "qa_plots_failed", 0)),
    ]:
        table.add_row(label, str(value))
    if summary.states:
        table.add_row("states", ", ".join(f"{key}={value}" for key, value in sorted(summary.states.items())))
    console.print(table)
    if summary.output_paths:
        paths = Table(title=f"{title} outputs")
        paths.add_column("Product")
        paths.add_column("Path")
        for key, path in sorted(summary.output_paths.items()):
            paths.add_row(key, str(path))
        console.print(paths)


def _print_workflow_summary(console, title: str, summary) -> None:
    table = Table(title=f"{title} summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for label, value in [
        ("sources total", summary.sources_total),
        ("sources complete", summary.sources_complete),
        ("sources partial", summary.sources_partial),
        ("sources failed", summary.sources_failed),
        ("planned", summary.planned),
        ("already valid", summary.already_valid),
        ("queued download", summary.queued_download),
        ("downloaded", summary.downloaded),
        ("download failed", summary.download_failed),
        ("queued fit", summary.queued_fit),
        ("measured", summary.measured),
        ("fit failed", summary.fit_failed),
        ("blocked", summary.blocked),
        ("science recommended", summary.science_recommended),
        ("outputs valid", summary.outputs_valid),
        ("outputs written/rebuilt", summary.outputs_rebuilt),
        ("outputs failed", summary.outputs_failed),
        ("qa plots planned", getattr(summary, "qa_plots_planned", 0)),
        ("qa plots written", getattr(summary, "qa_plots_written", 0)),
        ("qa plots failed", getattr(summary, "qa_plots_failed", 0)),
        ("cleanup deleted", summary.cleanup_deleted),
        ("cleanup deleted bytes", summary.cleanup_deleted_bytes),
        ("live cutout count", summary.live_cutout_count),
        ("live cutout bytes", summary.live_cutout_bytes),
        ("backpressure events", summary.backpressure_events),
    ]:
        table.add_row(label, str(value))
    if summary.states:
        table.add_row("states", ", ".join(f"{key}={value}" for key, value in sorted(summary.states.items())))
    if summary.summary_path:
        table.add_row("summary path", summary.summary_path)
    if summary.event_log_path:
        table.add_row("event log", summary.event_log_path)
    for hint in getattr(summary, "operator_hints", []) or []:
        table.add_row("next action", hint)
    console.print(table)


def _resolve_clean_result_source_ids(conn, args) -> list[str]:
    if getattr(args, "all", False):
        rows = conn.execute(
            """
            SELECT DISTINCT source_id FROM (
              SELECT source_id FROM photometry_measurements
              UNION SELECT source_id FROM photometry_work_items
              UNION SELECT source_id FROM photometry_failures
              UNION SELECT source_id FROM photometry_output_products
              UNION SELECT source_id FROM photometry_source_summaries
            )
            ORDER BY source_id
            """
        ).fetchall()
        source_ids = [row["source_id"] for row in rows if row["source_id"] is not None]
    else:
        source_ids = list(getattr(args, "source_id", None) or [])
        if getattr(args, "source_name", None):
            row = conn.execute("SELECT source_id FROM sources WHERE source_name = ?", (args.source_name,)).fetchone()
            if row is None:
                raise SpxCutoutDBError(f"source not found: {args.source_name}")
            source_ids.append(row["source_id"])
        if getattr(args, "limit_sources", None):
            rows = conn.execute(
                "SELECT source_id FROM sources WHERE active = 1 ORDER BY COALESCE(priority, 999999), source_id LIMIT ?",
                (args.limit_sources,),
            ).fetchall()
            source_ids.extend(row["source_id"] for row in rows)
    return list(dict.fromkeys(source_ids))


def _clean_photometry_results(conn, config, *, source_ids: list[str], delete_files: bool, dry_run: bool, console) -> dict[str, int]:
    placeholders = ",".join("?" for _ in source_ids)
    params = tuple(source_ids)
    stats = {
        "sources": len(source_ids),
        "measurements": _count_where(conn, "photometry_measurements", placeholders, params),
        "work_items": _count_where(conn, "photometry_work_items", placeholders, params),
        "failures": _count_where(conn, "photometry_failures", placeholders, params),
        "output_products": _count_where(conn, "photometry_output_products", placeholders, params),
        "summaries": _count_where(conn, "photometry_source_summaries", placeholders, params),
        "files": 0,
    }
    paths = _photometry_result_paths_for_sources(conn, config, source_ids) if delete_files else []
    if dry_run:
        for path in paths:
            console.print(f"Would remove {path}")
        stats["files"] = sum(1 for path in paths if path.exists())
        return stats

    if delete_files:
        for path in paths:
            if _remove_result_path(path):
                stats["files"] += 1

    conn.execute(f"DELETE FROM photometry_output_products WHERE source_id IN ({placeholders})", params)
    conn.execute(f"DELETE FROM photometry_source_summaries WHERE source_id IN ({placeholders})", params)
    conn.execute(f"DELETE FROM photometry_failures WHERE source_id IN ({placeholders})", params)
    conn.execute(f"DELETE FROM photometry_measurements WHERE source_id IN ({placeholders})", params)
    conn.execute(f"DELETE FROM photometry_work_items WHERE source_id IN ({placeholders})", params)
    conn.commit()
    return stats


def _count_where(conn, table_name: str, placeholders: str, params: tuple[str, ...]) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name} WHERE source_id IN ({placeholders})", params).fetchone()[0])


def _photometry_result_paths_for_sources(conn, config, source_ids: list[str]) -> list[Path]:
    paths: list[Path] = []
    placeholders = ",".join("?" for _ in source_ids)
    params = tuple(source_ids)
    for row in conn.execute(f"SELECT path FROM photometry_output_products WHERE source_id IN ({placeholders})", params):
        paths.append(_project_path(config, row["path"]))
    for row in conn.execute(f"SELECT * FROM photometry_source_summaries WHERE source_id IN ({placeholders})", params):
        for key in ["spectrum_path", "sed_plot_path", "qa_summary_path", "provenance_path", "measurement_index_path"]:
            if row[key]:
                paths.append(_project_path(config, row[key]))
    for row in conn.execute(f"SELECT * FROM sources WHERE source_id IN ({placeholders})", params):
        source = {key: row[key] for key in row.keys()}
        output_paths = source_output_paths(config, source)
        paths.extend(output_paths[key] for key in ["csv", "sed", "qa", "provenance", "index", "manifest", "qa_dir"])
    return _dedupe_paths(paths)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    out: dict[str, Path] = {}
    for path in paths:
        out[str(path.resolve())] = path
    return sorted(out.values(), key=lambda value: len(str(value)), reverse=True)


def _remove_result_path(path: Path) -> bool:
    if path.is_dir():
        shutil.rmtree(path)
        return True
    if path.exists():
        path.unlink()
        return True
    return False


def _rel_or_str(path: Path, config) -> str:
    try:
        return str(Path(path).resolve().relative_to(config.project.root))
    except ValueError:
        return str(path)


def _project_path(config, value: str | None) -> Path:
    path = Path(value or "")
    return path if path.is_absolute() else config.project.root / path


def _parse_products(args) -> list[str]:
    products: list[str] = []
    for value in getattr(args, "product", None) or []:
        products.extend(item.strip() for item in str(value).split(",") if item.strip())
    if getattr(args, "products", None):
        products.extend(item.strip() for item in args.products.split(",") if item.strip())
    return products or ["required"]


def _parse_detectors(value: str | None) -> list[int] | None:
    if not value:
        return None
    return [int(item.strip().lstrip("D")) for item in value.split(",") if item.strip()]


def _parse_url_templates(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ConfigError("--url must be PRODUCT=URL_TEMPLATE")
        product, url = value.split("=", 1)
        parsed[product.strip()] = url.strip()
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
