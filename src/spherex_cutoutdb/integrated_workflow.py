"""Integrated catalog-to-spectrum workflow orchestration.

This module deliberately sits above the existing discovery, planner,
downloader, calibration, and V5 photometry modules.  The downloader remains
the only network/cutout validation implementation, and the photometry kernel
remains the only science measurement implementation.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from threading import BoundedSemaphore
import time
from types import SimpleNamespace
from typing import Any

import pandas as pd

from .calibration import resolve_required_calibrations, sync_calibrations
from .catalog import ingest_catalog, normalize_sources, read_source_catalog, validate_sources
from .config import Config, config_hash
from .database import canonical_json, stable_hash, utcnow
from .downloader import CompletedDownload, iter_download_plan_results, resolve_project_path
from .exceptions import CatalogError, SpxCutoutDBError
from .photometry.measure import MeasurementResult, measure_cutout
from .photometry.measurement_plan import build_photometry_plan
from .photometry.outputs import (
    full_qa_files_exist,
    full_qa_measurement_path,
    source_output_paths,
    validate_full_qa_measurement,
    validate_full_qa_outputs,
    validate_source_outputs,
    write_full_measurement_qa_batch,
    write_full_qa_manifest,
    write_source_outputs,
)
from .photometry.result_store import (
    VALIDATION_OK,
    active_sources,
    finish_photometry_run,
    latest_cutout_for_key,
    mark_work_item_state,
    measurement_id_for,
    photometry_config_hash,
    record_measurement,
    record_output_product,
    record_photometry_failure,
    source_by_name,
    start_photometry_run,
    upsert_source_summary,
    upsert_work_item,
    valid_measurement_exists,
)


TERMINAL_WORK_STATES = {
    "photometry_valid",
    "persisted",
    "failed_fit",
    "failed_input",
    "failed_download",
    "failed_validation",
    "blocked_calibration_missing",
    "blocked_no_download",
    "blocked_storage_backpressure",
}


@dataclass(slots=True)
class WorkflowSummary:
    sources_total: int = 0
    sources_complete: int = 0
    sources_partial: int = 0
    sources_failed: int = 0
    planned: int = 0
    already_valid: int = 0
    queued_download: int = 0
    downloaded: int = 0
    download_failed: int = 0
    queued_fit: int = 0
    measured: int = 0
    fit_failed: int = 0
    blocked: int = 0
    science_recommended: int = 0
    outputs_valid: int = 0
    outputs_rebuilt: int = 0
    outputs_failed: int = 0
    qa_plots_planned: int = 0
    qa_plots_written: int = 0
    qa_plots_failed: int = 0
    qa_seconds: float = 0.0
    cleanup_deleted: int = 0
    cleanup_deleted_bytes: int = 0
    backpressure_events: int = 0
    live_cutout_count: int = 0
    live_cutout_bytes: int = 0
    elapsed_seconds: float = 0.0
    states: dict[str, int] = field(default_factory=dict)
    operator_hints: list[str] = field(default_factory=list)
    event_log_path: str | None = None
    text_log_path: str | None = None
    summary_path: str | None = None


@dataclass(slots=True)
class _SourceState:
    source: dict[str, Any]
    planned: int = 0
    terminal: int = 0
    measured: int = 0
    skipped: int = 0
    failed: int = 0
    output_status: str | None = None
    cleanup_status: str | None = None
    finalized: bool = False
    measurements: list[MeasurementResult] = field(default_factory=list)
    full_qa_pending: bool = False
    full_qa_status: str | None = None


class _WorkflowLogger:
    def __init__(self, config: Config, workflow_run_id: str, run_id: str | None):
        root = config.project.log_root / "runs"
        root.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = root / f"{workflow_run_id}.jsonl"
        self.text_path = root / f"{workflow_run_id}.log"
        self.workflow_run_id = workflow_run_id
        self.run_id = run_id

    def emit(
        self,
        conn,
        event_type: str,
        message: str,
        *,
        source_id: str | None = None,
        product_id: int | None = None,
        cutout_id: int | None = None,
        work_item_id: str | None = None,
        payload: dict[str, Any] | None = None,
        console=None,
        verbose: bool = False,
    ) -> None:
        event = {
            "time": utcnow(),
            "workflow_run_id": self.workflow_run_id,
            "run_id": self.run_id,
            "event": event_type,
            "source_id": source_id,
            "product_id": product_id,
            "cutout_id": cutout_id,
            "work_item_id": work_item_id,
            "message": message,
            "payload": payload or {},
        }
        conn.execute(
            """
            INSERT INTO workflow_events(
              workflow_run_id, run_id, event_time, event_type, source_id, product_id,
              cutout_id, work_item_id, message, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.workflow_run_id,
                self.run_id,
                event["time"],
                event_type,
                source_id,
                product_id,
                cutout_id,
                work_item_id,
                message,
                canonical_json(payload or {}),
            ),
        )
        conn.commit()
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
        with self.text_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{event['time']} {event_type} {message}\n")
        if verbose and console is not None:
            console.print(message)


def validate_project_catalog(config: Config) -> dict[str, Any]:
    """Run non-mutating project/catalog preflight checks."""

    raw = read_source_catalog(config)
    normalized = normalize_sources(raw, config)
    report = validate_sources(normalized, config)
    checks = {
        "catalog_path": str(config.catalog.path),
        "target_id_column": config.catalog.source_id_column,
        "source_name_column": config.catalog.source_name_column,
        "ra_column": config.catalog.ra_column,
        "dec_column": config.catalog.dec_column,
        "n_rows_input": report.n_rows_input,
        "n_rows_valid": report.n_rows_valid,
        "n_rows_invalid": report.n_rows_invalid,
        "warnings": report.warnings,
        "errors": report.errors,
        "database_path": str(config.project.database_path),
        "cutout_dir": str(config.project.data_root / "cutouts"),
        "calibration_cache": str(config.calibration.cache_root),
        "download_workers": config.runtime.max_download_workers,
        "fit_workers": config.runtime.max_fit_workers,
        "max_inflight_cutouts": config.runtime.max_inflight_cutouts,
        "max_live_cutout_gb": config.runtime.max_live_cutout_gb,
    }
    if not report.valid:
        checks["valid"] = False
        return checks
    duplicate_names = normalized[normalized["source_id"].duplicated(keep=False)]["source_id"].tolist()
    checks["duplicate_target_ids"] = sorted(set(duplicate_names))
    checks["valid"] = not duplicate_names
    return checks


def run_catalog_workflow(
    conn,
    config: Config,
    *,
    run_id: str | None = None,
    source_ids: list[str] | None = None,
    source_name: str | None = None,
    limit_sources: int | None = None,
    resume: bool = True,
    update: bool = False,
    download_missing: bool | None = None,
    sync_calibration: bool = False,
    cleanup_cutouts: str | None = None,
    keep_failed_cutouts: bool | None = None,
    qa_level: str | None = None,
    force_photometry_rerun: bool = False,
    max_download_workers: int | None = None,
    max_fit_workers: int | None = None,
    max_inflight_cutouts: int | None = None,
    max_live_cutout_gb: float | None = None,
    max_open_fits_files: int | None = None,
    dry_run: bool = False,
    progress: bool = True,
    verbose: bool = False,
    console=None,
) -> WorkflowSummary:
    """Run integrated discovery/planning/download/photometry/output/cleanup.

    Discovery remains owned by the existing CLI discovery path.  The integrated
    manager consumes active source/product matches, plans photometry first, then
    downloads only missing work through the existing downloader event stream.
    """

    started = time.perf_counter()
    effective_download_missing = config.workflow.download_missing if download_missing is None else download_missing
    effective_force_remeasure = force_photometry_rerun or not config.workflow.skip_valid_measurements
    effective_qa_level = qa_level or config.photometry.qa_level
    cleanup_policy = _normalize_cleanup_policy(cleanup_cutouts or config.cleanup.cutouts)
    keep_failed = config.cleanup.keep_failed_cutouts if keep_failed_cutouts is None else keep_failed_cutouts
    max_fit_workers = max(1, int(max_fit_workers or config.runtime.max_fit_workers))
    max_download_workers = max(1, int(max_download_workers or config.runtime.max_download_workers))
    max_inflight_cutouts = max(1, int(max_inflight_cutouts or config.runtime.max_inflight_cutouts))
    max_live_cutout_gb = float(config.runtime.max_live_cutout_gb if max_live_cutout_gb is None else max_live_cutout_gb)
    max_open_fits_files = max(1, int(max_open_fits_files or config.runtime.max_open_fits_files))

    old_download_workers = config.download.max_workers
    old_download_concurrency = config.download.concurrency
    config.download.max_workers = max_download_workers
    config.download.concurrency = max_download_workers

    workflow_run_id = _start_workflow_run(conn, run_id, config, {
        "resume": resume,
        "update": update,
        "download_missing": effective_download_missing,
        "cleanup_cutouts": cleanup_policy,
        "keep_failed_cutouts": keep_failed,
        "qa_level": effective_qa_level,
        "qa_workers": config.photometry.qa.full_plot_workers,
        "qa_dpi": config.photometry.qa.measurement_plot_dpi,
        "qa_colorbars": config.photometry.qa.measurement_plot_colorbars,
        "max_download_workers": max_download_workers,
        "max_fit_workers": max_fit_workers,
        "max_inflight_cutouts": max_inflight_cutouts,
        "max_live_cutout_gb": max_live_cutout_gb,
        "max_open_fits_files": max_open_fits_files,
        "dry_run": dry_run,
    })
    logger = _WorkflowLogger(config, workflow_run_id, run_id)
    summary = WorkflowSummary(
        event_log_path=_rel_or_str(logger.jsonl_path, config),
        text_log_path=_rel_or_str(logger.text_path, config),
    )
    photometry_run_id = start_photometry_run(conn, config, run_id)
    logger.emit(
        conn,
        "workflow_runtime",
        (
            f"Workflow runtime: qa_level={effective_qa_level} "
            f"qa_workers={config.photometry.qa.full_plot_workers} "
            f"qa_dpi={config.photometry.qa.measurement_plot_dpi} "
            f"qa_colorbars={config.photometry.qa.measurement_plot_colorbars} "
            f"download_workers={max_download_workers} fit_workers={max_fit_workers}"
        ),
        payload={
            "qa_level": effective_qa_level,
            "qa_workers": config.photometry.qa.full_plot_workers,
            "qa_dpi": config.photometry.qa.measurement_plot_dpi,
            "qa_colorbars": config.photometry.qa.measurement_plot_colorbars,
            "max_download_workers": max_download_workers,
            "max_fit_workers": max_fit_workers,
        },
        console=console,
        verbose=verbose,
    )
    fits_semaphore = BoundedSemaphore(max_open_fits_files)
    source_states: dict[str, _SourceState] = {}
    fit_futures: dict[Future, dict[str, Any]] = {}
    finalized_sources: set[str] = set()

    try:
        if not _has_active_sources(conn):
            if dry_run:
                logger.emit(conn, "catalog_dry_run", "Catalog ingest would be refreshed", console=console, verbose=verbose)
            else:
                catalog_version_id, report, stats = ingest_catalog(conn, config, run_id)
                logger.emit(
                    conn,
                    "catalog_ingested",
                    f"Catalog ingested: {catalog_version_id}",
                    payload={"catalog_version_id": catalog_version_id, **stats, "valid_rows": report.n_rows_valid},
                    console=console,
                    verbose=verbose,
                )

        selected_source_ids = _select_source_ids(conn, source_ids=source_ids, source_name=source_name, limit=limit_sources)
        sources = active_sources(conn, source_ids=selected_source_ids, limit=limit_sources)
        summary.sources_total = len(sources)
        source_lookup = {source["source_id"]: source for source in sources}
        if not sources:
            raise SpxCutoutDBError("no active sources selected")

        if sync_calibration and not dry_run:
            cal_summary = sync_calibrations(conn, config, products=["required"])
            logger.emit(
                conn,
                "calibration_sync",
                f"Calibration sync: valid={cal_summary.valid} failed={cal_summary.failed} missing={len(cal_summary.missing)}",
                payload=asdict(cal_summary),
                console=console,
                verbose=verbose,
            )

        plan_started = time.perf_counter()
        items, counts = build_photometry_plan(
            conn,
            config,
            photometry_run_id=photometry_run_id,
            source_ids=[source["source_id"] for source in sources],
            force_remeasure=effective_force_remeasure,
        )
        summary.states = counts
        summary.planned = len(items)
        logger.emit(
            conn,
            "workflow_plan",
            f"Workflow plan: planned={len(items)} states={_format_counts(counts)}",
            payload={"counts": counts, "seconds": time.perf_counter() - plan_started},
            console=console,
            verbose=verbose,
        )

        if dry_run:
            summary.queued_download = sum(1 for item in items if item["state"] == "cutout_missing_or_invalid")
            summary.queued_fit = sum(1 for item in items if item["state"] == "cutout_valid_measurement_missing")
            _attach_operator_hints(summary)
            _finish_workflow_run(conn, workflow_run_id, "success", summary)
            finish_photometry_run(conn, photometry_run_id, "success", asdict(summary))
            return summary

        for source in sources:
            source_items = [item for item in items if item["source"]["source_id"] == source["source_id"]]
            state = _SourceState(source=source, planned=len(source_items))
            source_states[source["source_id"]] = state
            _record_source_state(conn, workflow_run_id, state, "planned")

        missing_items: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_fit_workers) as fit_pool:
            for item in items:
                state = item["state"]
                if state == "photometry_valid":
                    if (
                        effective_qa_level == "full"
                        and not validate_full_qa_measurement(
                            config=config,
                            source=item["source"],
                            measurement_id=item["measurement_id"],
                        )
                    ):
                        if _cutout_file_exists(config, item.get("cutout") or {}):
                            logger.emit(
                                conn,
                                "fit_queued_for_full_qa",
                                f"Remeasuring for missing/stale full QA: source={item['source']['source_id']} cutout={item['plan_row']['cutout_key']}",
                                source_id=item["source"]["source_id"],
                                product_id=item["plan_row"].get("product_id"),
                                cutout_id=(item.get("cutout") or {}).get("cutout_id"),
                                work_item_id=item["work_item_id"],
                                console=console,
                                verbose=verbose,
                            )
                            _submit_fit(conn, config, photometry_run_id, fit_pool, fit_futures, item, fits_semaphore, summary, logger, console, verbose)
                            continue
                        hint = (
                            "Full QA PNGs are missing or stale for some valid measurements, "
                            "but the cutout files were already cleaned up. Valid measurements "
                            "were kept and downloads were not restarted; use --force-photometry-rerun "
                            "with available cutouts to regenerate those QA plots."
                        )
                        if hint not in summary.operator_hints:
                            summary.operator_hints.append(hint)
                        logger.emit(
                            conn,
                            "full_qa_missing_no_cutout",
                            f"Full QA missing but cutout is unavailable: source={item['source']['source_id']} cutout={item['plan_row']['cutout_key']}",
                            source_id=item["source"]["source_id"],
                            product_id=item["plan_row"].get("product_id"),
                            cutout_id=(item.get("cutout") or {}).get("cutout_id"),
                            work_item_id=item["work_item_id"],
                            console=console,
                            verbose=verbose,
                        )
                    summary.already_valid += 1
                    _mark_terminal(conn, workflow_run_id, source_states, item, skipped=True)
                    continue
                if state == "cutout_valid_measurement_missing":
                    _submit_fit(conn, config, photometry_run_id, fit_pool, fit_futures, item, fits_semaphore, summary, logger, console, verbose)
                    continue
                if state == "calibration_missing":
                    summary.blocked += 1
                    _record_item_failure_once(conn, photometry_run_id, item, "calibration", item["reason"] or "missing calibration")
                    mark_work_item_state(conn, item["work_item_id"], "blocked_calibration_missing", item["reason"])
                    _mark_terminal(conn, workflow_run_id, source_states, item, failed=True)
                    continue
                if state == "cutout_missing_or_invalid" and effective_download_missing:
                    missing_items.append(item)
                    continue
                summary.blocked += 1
                reason = item["reason"] or "cutout missing or invalid and downloads are disabled"
                _record_item_failure_once(conn, photometry_run_id, item, "input", reason)
                mark_work_item_state(conn, item["work_item_id"], "blocked_no_download", reason)
                _mark_terminal(conn, workflow_run_id, source_states, item, failed=True)

            summary.queued_download = len(missing_items)
            missing_by_key = {item["plan_row"]["cutout_key"]: item for item in missing_items}
            pending_plan_rows = [item["plan_row"] for item in missing_items]
            cursor = 0
            while cursor < len(pending_plan_rows):
                _drain_fit_futures(
                    conn,
                    config,
                    workflow_run_id,
                    photometry_run_id,
                    source_states,
                    fit_futures,
                    summary,
                    logger,
                    finalized_sources,
                    cleanup_policy,
                    keep_failed,
                    qa_level,
                    console,
                    verbose,
                    wait_timeout=0.0,
                )
                batch_limit = _download_batch_limit(
                    conn,
                    config,
                    max_inflight_cutouts=max_inflight_cutouts,
                    max_live_cutout_gb=max_live_cutout_gb,
                )
                if batch_limit <= 0:
                    summary.backpressure_events += 1
                    live_count, live_bytes = _live_cutout_usage(conn, config)
                    logger.emit(
                        conn,
                        "backpressure",
                        "Download submission paused by live cutout limits",
                        payload={"live_cutout_count": live_count, "live_cutout_bytes": live_bytes},
                        console=console,
                        verbose=verbose,
                    )
                    if fit_futures:
                        _drain_fit_futures(
                            conn,
                            config,
                            workflow_run_id,
                            photometry_run_id,
                            source_states,
                            fit_futures,
                            summary,
                            logger,
                            finalized_sources,
                            cleanup_policy,
                            keep_failed,
                            qa_level,
                            console,
                            verbose,
                            wait_timeout=0.25,
                        )
                        continue
                    _drain_full_qa_pending(
                        conn,
                        config,
                        workflow_run_id,
                        photometry_run_id,
                        source_states,
                        summary,
                        logger,
                        cleanup_policy,
                        keep_failed,
                        console,
                        verbose,
                    )
                    relieved = _relieve_storage_backpressure(
                        conn,
                        config,
                        workflow_run_id,
                        source_states,
                        summary,
                        cleanup_policy,
                        keep_failed,
                        logger,
                        console,
                        verbose,
                    )
                    if relieved:
                        continue
                    _block_remaining_for_backpressure(
                        conn,
                        workflow_run_id,
                        photometry_run_id,
                        pending_plan_rows[cursor:],
                        missing_by_key,
                        source_states,
                        summary,
                        logger,
                        console,
                        verbose,
                    )
                    cursor = len(pending_plan_rows)
                    break

                batch = pending_plan_rows[cursor:cursor + batch_limit]
                cursor += len(batch)
                for event in iter_download_plan_results(
                    conn,
                    run_id,
                    config,
                    plan_rows=batch,
                    progress=progress,
                    verbose=verbose,
                    console=console,
                ):
                    _drain_fit_futures(
                        conn,
                        config,
                        workflow_run_id,
                        photometry_run_id,
                        source_states,
                        fit_futures,
                        summary,
                        logger,
                        finalized_sources,
                        cleanup_policy,
                        keep_failed,
                        qa_level,
                        console,
                        verbose,
                        wait_timeout=0.0,
                    )
                    if isinstance(event, dict):
                        logger.emit(
                            conn,
                            str(event.get("event") or "download_event"),
                            f"Downloader event: {event.get('event')}",
                            source_id=event.get("source_id"),
                            payload=event,
                            console=console,
                            verbose=verbose and event.get("event") == "file_retry",
                        )
                        continue
                    _handle_download_event(
                        conn,
                        config,
                        workflow_run_id,
                        photometry_run_id,
                        fit_pool,
                        fit_futures,
                        missing_by_key,
                        event,
                        fits_semaphore,
                        source_states,
                        summary,
                        logger,
                        console,
                        verbose,
                    )

            while fit_futures:
                _drain_fit_futures(
                    conn,
                    config,
                    workflow_run_id,
                    photometry_run_id,
                    source_states,
                    fit_futures,
                    summary,
                    logger,
                    finalized_sources,
                    cleanup_policy,
                    keep_failed,
                    qa_level,
                    console,
                    verbose,
                    wait_timeout=0.25,
                )

        for source_id, state in source_states.items():
            if source_id not in finalized_sources:
                _finalize_source(
                    conn,
                    config,
                    workflow_run_id,
                    photometry_run_id,
                    state,
                    summary,
                    logger,
                    cleanup_policy,
                    keep_failed,
                    qa_level,
                    console,
                    verbose,
                )
                finalized_sources.add(source_id)

        _drain_full_qa_pending(
            conn,
            config,
            workflow_run_id,
            photometry_run_id,
            source_states,
            summary,
            logger,
            cleanup_policy,
            keep_failed,
            console,
            verbose,
        )

        live_count, live_bytes = _live_cutout_usage(conn, config)
        summary.live_cutout_count = live_count
        summary.live_cutout_bytes = live_bytes
        summary.elapsed_seconds = time.perf_counter() - started
        _attach_operator_hints(summary)
        status = "partial_success" if summary.download_failed or summary.fit_failed or summary.outputs_failed or summary.blocked else "success"
        summary_path = _write_run_summary(config, workflow_run_id, summary)
        summary.summary_path = _rel_or_str(summary_path, config)
        _finish_workflow_run(conn, workflow_run_id, status, summary)
        finish_photometry_run(conn, photometry_run_id, status, asdict(summary), summary.summary_path)
        logger.emit(
            conn,
            "workflow_complete",
            f"Workflow complete: status={status} measured={summary.measured} outputs={summary.outputs_rebuilt}",
            payload=asdict(summary),
            console=console,
            verbose=verbose,
        )
        return summary
    except Exception:
        summary.elapsed_seconds = time.perf_counter() - started
        _finish_workflow_run(conn, workflow_run_id, "failed", summary)
        finish_photometry_run(conn, photometry_run_id, "failed", asdict(summary))
        raise
    finally:
        config.download.max_workers = old_download_workers
        config.download.concurrency = old_download_concurrency


def summarize_workflow_project(
    conn,
    config: Config,
    *,
    rebuild_missing_outputs: bool = False,
    qa_level: str | None = None,
    run_id: str | None = None,
    console=None,
    verbose: bool = False,
) -> WorkflowSummary:
    workflow_run_id = _start_workflow_run(conn, run_id, config, {"summary": True, "rebuild_missing_outputs": rebuild_missing_outputs})
    logger = _WorkflowLogger(config, workflow_run_id, run_id)
    summary = WorkflowSummary(event_log_path=_rel_or_str(logger.jsonl_path, config), text_log_path=_rel_or_str(logger.text_path, config))
    photometry_run_id = start_photometry_run(conn, config, run_id)
    sources = active_sources(conn)
    summary.sources_total = len(sources)
    for source in sources:
        state = _SourceState(source=source, planned=_planned_count_for_source(conn, source["source_id"]))
        measurements = _load_measurements_for_source(conn, source["source_id"])
        has_measurements = len(measurements) > 0
        paths = source_output_paths(config, source)
        outputs_ok = validate_source_outputs(paths, config=config, source=source, measurements=measurements)
        if outputs_ok:
            summary.outputs_valid += 1
            summary.sources_complete += 1
            continue
        if rebuild_missing_outputs and has_measurements:
            _finalize_source(
                conn,
                config,
                workflow_run_id,
                photometry_run_id,
                state,
                summary,
                logger,
                "never",
                True,
                qa_level,
                console,
                verbose,
                cleanup=False,
            )
        elif has_measurements:
            summary.sources_partial += 1
        else:
            summary.sources_failed += 1
    live_count, live_bytes = _live_cutout_usage(conn, config)
    summary.live_cutout_count = live_count
    summary.live_cutout_bytes = live_bytes
    summary_path = _write_catalog_summary(conn, config, workflow_run_id, summary)
    summary.summary_path = _rel_or_str(summary_path, config)
    _finish_workflow_run(conn, workflow_run_id, "success", summary)
    finish_photometry_run(conn, photometry_run_id, "success", asdict(summary), summary.summary_path)
    return summary


def _start_workflow_run(conn, run_id: str | None, config: Config, args: dict[str, Any]) -> str:
    payload = {"run_id": run_id, "args": args, "config_hash": config_hash(config), "started_at": utcnow()}
    workflow_run_id = f"workflow_{stable_hash(payload)[:16]}"
    conn.execute(
        """
        INSERT OR REPLACE INTO workflow_runs(
          workflow_run_id, run_id, started_at, status, config_hash, args_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (workflow_run_id, run_id, utcnow(), "running", config_hash(config), canonical_json(args)),
    )
    conn.commit()
    return workflow_run_id


def _finish_workflow_run(conn, workflow_run_id: str, status: str, summary: WorkflowSummary) -> None:
    conn.execute(
        """
        UPDATE workflow_runs
        SET finished_at = ?, status = ?, counts_json = ?, summary_path = ?
        WHERE workflow_run_id = ?
        """,
        (utcnow(), status, canonical_json(asdict(summary)), summary.summary_path, workflow_run_id),
    )
    conn.commit()


def _has_active_sources(conn) -> bool:
    return int(conn.execute("SELECT COUNT(*) FROM sources WHERE active = 1").fetchone()[0]) > 0


def _select_source_ids(conn, *, source_ids: list[str] | None, source_name: str | None, limit: int | None) -> list[str] | None:
    selected = list(source_ids or [])
    if source_name:
        source = source_by_name(conn, source_name)
        if source is None:
            raise SpxCutoutDBError(f"source not found: {source_name}")
        selected.append(source["source_id"])
    if limit and not selected:
        rows = conn.execute(
            "SELECT source_id FROM sources WHERE active = 1 ORDER BY COALESCE(priority, 999999), source_id LIMIT ?",
            (limit,),
        ).fetchall()
        selected.extend(row["source_id"] for row in rows)
    return list(dict.fromkeys(selected)) or None


def _submit_fit(
    conn,
    config: Config,
    photometry_run_id: str,
    fit_pool: ThreadPoolExecutor,
    fit_futures: dict[Future, dict[str, Any]],
    item: dict[str, Any],
    fits_semaphore: BoundedSemaphore,
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    console,
    verbose: bool,
) -> None:
    cutout = latest_cutout_for_key(conn, item["plan_row"]["cutout_key"])
    if cutout is None or cutout.get("validation_status") not in VALIDATION_OK:
        reason = "validated cutout is not available for fitting"
        _record_item_failure_once(conn, photometry_run_id, item, "validation", reason)
        mark_work_item_state(conn, item["work_item_id"], "failed_validation", reason)
        return
    item = {**item, "cutout": cutout}
    mark_work_item_state(conn, item["work_item_id"], "measuring", None)
    future = fit_pool.submit(_measure_item_without_db_write, config, item, fits_semaphore)
    fit_futures[future] = item
    summary.queued_fit += 1
    logger.emit(
        conn,
        "fit_queued",
        f"Fit queued: source={item['source']['source_id']} cutout={item['plan_row']['cutout_key']}",
        source_id=item["source"]["source_id"],
        product_id=item["plan_row"].get("product_id"),
        cutout_id=cutout.get("cutout_id"),
        work_item_id=item["work_item_id"],
        console=console,
        verbose=verbose,
    )


def _measure_item_without_db_write(config: Config, item: dict[str, Any], fits_semaphore: BoundedSemaphore) -> MeasurementResult:
    cutout = item["cutout"]
    path = resolve_project_path(config, cutout["local_path"])
    with fits_semaphore:
        return measure_cutout(
            cutout_path=path,
            source=item["source"],
            cutout_row=cutout,
            calibration_resolution=item["calibration"],
            config=config,
            measurement_id=item["measurement_id"],
            work_item_id=item["work_item_id"],
        )


def _drain_fit_futures(
    conn,
    config: Config,
    workflow_run_id: str,
    photometry_run_id: str,
    source_states: dict[str, _SourceState],
    fit_futures: dict[Future, dict[str, Any]],
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    finalized_sources: set[str],
    cleanup_policy: str,
    keep_failed_cutouts: bool,
    qa_level: str | None,
    console,
    verbose: bool,
    *,
    wait_timeout: float,
) -> None:
    if not fit_futures:
        return
    done, _ = wait(set(fit_futures), timeout=wait_timeout, return_when=FIRST_COMPLETED)
    for future in done:
        item = fit_futures.pop(future)
        source_id = item["source"]["source_id"]
        try:
            measurement = future.result()
        except Exception as exc:  # noqa: BLE001 - per-fit failure boundary
            summary.fit_failed += 1
            reason = f"{exc.__class__.__name__}: {exc}"
            mark_work_item_state(conn, item["work_item_id"], "failed_fit", reason)
            _record_item_failure_once(conn, photometry_run_id, item, "fit", reason, exception_class=exc.__class__.__name__)
            _mark_terminal(conn, workflow_run_id, source_states, item, failed=True)
            logger.emit(
                conn,
                "fit_failed",
                f"Fit failed: source={source_id} cutout={item['plan_row']['cutout_key']} reason={reason}",
                source_id=source_id,
                product_id=item["plan_row"].get("product_id"),
                cutout_id=(item.get("cutout") or {}).get("cutout_id"),
                work_item_id=item["work_item_id"],
                console=console,
                verbose=verbose,
            )
        else:
            record_measurement(
                conn,
                photometry_run_id=photometry_run_id,
                work_item_id=item["work_item_id"],
                cutout_id=(item.get("cutout") or {}).get("cutout_id"),
                result=measurement,
                config=config,
            )
            source_states[source_id].measurements.append(measurement)
            summary.measured += 1
            if measurement.row.get("science_recommended"):
                summary.science_recommended += 1
            _mark_terminal(conn, workflow_run_id, source_states, item, measured=True)
            logger.emit(
                conn,
                "measurement_done",
                f"Measurement done: source={source_id} cutout={measurement.row.get('cutout_key')}",
                source_id=source_id,
                product_id=item["plan_row"].get("product_id"),
                cutout_id=(item.get("cutout") or {}).get("cutout_id"),
                work_item_id=item["work_item_id"],
                payload={
                    "measurement_id": measurement.row.get("measurement_id"),
                    "snr": measurement.row.get("selected_snr"),
                    "science_recommended": measurement.row.get("science_recommended"),
                    "flags": measurement.row.get("photometry_flags"),
                },
                console=console,
                verbose=verbose,
            )
        _finalize_ready_sources(
            conn,
            config,
            workflow_run_id,
            photometry_run_id,
            source_states,
            summary,
            logger,
            finalized_sources,
            cleanup_policy,
            keep_failed_cutouts,
            qa_level,
            console,
            verbose,
        )


def _handle_download_event(
    conn,
    config: Config,
    workflow_run_id: str,
    photometry_run_id: str,
    fit_pool: ThreadPoolExecutor,
    fit_futures: dict[Future, dict[str, Any]],
    missing_by_key: dict[str, dict[str, Any]],
    completed: CompletedDownload,
    fits_semaphore: BoundedSemaphore,
    source_states: dict[str, _SourceState],
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    console,
    verbose: bool,
) -> None:
    key = completed.plan_row["cutout_key"]
    item = missing_by_key.get(key)
    if item is None:
        return
    if not completed.result.success:
        summary.download_failed += 1
        reason = completed.result.reason or completed.result.status
        _record_item_failure_once(conn, photometry_run_id, item, "download", reason)
        mark_work_item_state(conn, item["work_item_id"], "failed_download", reason)
        _mark_terminal(conn, workflow_run_id, source_states, item, failed=True)
        logger.emit(
            conn,
            "download_failed",
            f"Download failed: source={item['source']['source_id']} cutout={key} reason={reason}",
            source_id=item["source"]["source_id"],
            product_id=item["plan_row"].get("product_id"),
            work_item_id=item["work_item_id"],
            console=console,
            verbose=verbose,
        )
        return
    summary.downloaded += 1
    refreshed = _refresh_downloaded_item(conn, config, photometry_run_id, item)
    state = refreshed["state"]
    if state == "photometry_valid":
        summary.already_valid += 1
        _mark_terminal(conn, workflow_run_id, source_states, refreshed, skipped=True)
        return
    if state == "cutout_valid_measurement_missing":
        _submit_fit(conn, config, photometry_run_id, fit_pool, fit_futures, refreshed, fits_semaphore, summary, logger, console, verbose)
        return
    summary.blocked += 1
    reason = refreshed.get("reason") or f"downloaded item not measurable: {state}"
    failure_type = "calibration" if state == "calibration_missing" else "validation"
    final_state = "blocked_calibration_missing" if state == "calibration_missing" else "failed_validation"
    _record_item_failure_once(conn, photometry_run_id, refreshed, failure_type, reason)
    mark_work_item_state(conn, refreshed["work_item_id"], final_state, reason)
    _mark_terminal(conn, workflow_run_id, source_states, refreshed, failed=True)


def _block_remaining_for_backpressure(
    conn,
    workflow_run_id: str,
    photometry_run_id: str,
    remaining_plan_rows: list[dict[str, Any]],
    missing_by_key: dict[str, dict[str, Any]],
    source_states: dict[str, _SourceState],
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    console,
    verbose: bool,
) -> None:
    reason = "live cutout storage/backpressure limit prevents safe download submission"
    for row in remaining_plan_rows:
        item = missing_by_key.get(row["cutout_key"])
        if item is None:
            continue
        summary.blocked += 1
        _record_item_failure_once(conn, photometry_run_id, item, "storage_backpressure", reason, retryable=True)
        mark_work_item_state(conn, item["work_item_id"], "blocked_storage_backpressure", reason)
        _mark_terminal(conn, workflow_run_id, source_states, item, failed=True)
        logger.emit(
            conn,
            "blocked_storage_backpressure",
            f"Download blocked by storage backpressure: source={item['source']['source_id']} cutout={row['cutout_key']}",
            source_id=item["source"]["source_id"],
            product_id=item["plan_row"].get("product_id"),
            work_item_id=item["work_item_id"],
            payload={"reason": reason},
            console=console,
            verbose=verbose,
        )


def _relieve_storage_backpressure(
    conn,
    config: Config,
    workflow_run_id: str,
    source_states: dict[str, _SourceState],
    summary: WorkflowSummary,
    cleanup_policy: str,
    keep_failed_cutouts: bool,
    logger: _WorkflowLogger,
    console,
    verbose: bool,
) -> bool:
    if cleanup_policy == "never":
        return False
    before_count, before_bytes = _live_cutout_usage(conn, config)
    deleted_total = 0
    deleted_bytes_total = 0
    for source in active_sources(conn):
        measurements = _load_measurements_for_source(conn, source["source_id"])
        if not measurements:
            continue
        paths = source_output_paths(config, source)
        if not validate_source_outputs(paths, config=config, source=source, measurements=measurements):
            continue
        if config.photometry.qa_level == "full" and not validate_full_qa_outputs(config=config, source=source, measurements=measurements):
            continue
        deleted, deleted_bytes = _cleanup_source_cutouts(
            conn,
            config,
            workflow_run_id,
            source,
            policy=cleanup_policy,
            keep_failed_cutouts=keep_failed_cutouts,
            outputs_valid=True,
        )
        deleted_total += deleted
        deleted_bytes_total += deleted_bytes
        if source["source_id"] in source_states:
            source_states[source["source_id"]].cleanup_status = "complete"
    summary.cleanup_deleted += deleted_total
    summary.cleanup_deleted_bytes += deleted_bytes_total
    after_count, after_bytes = _live_cutout_usage(conn, config)
    if deleted_total:
        logger.emit(
            conn,
            "backpressure_cleanup",
            f"Storage backpressure cleanup deleted {deleted_total} cutout(s)",
            payload={
                "before_count": before_count,
                "before_bytes": before_bytes,
                "after_count": after_count,
                "after_bytes": after_bytes,
                "deleted_bytes": deleted_bytes_total,
            },
            console=console,
            verbose=verbose,
        )
    return after_count < before_count or after_bytes < before_bytes


def _refresh_downloaded_item(conn, config: Config, photometry_run_id: str, item: dict[str, Any]) -> dict[str, Any]:
    plan_row = item["plan_row"]
    source = item["source"]
    cutout = latest_cutout_for_key(conn, plan_row["cutout_key"])
    calibration = resolve_required_calibrations(conn, config, cutout or plan_row)
    state = "cutout_missing_or_invalid"
    reason = "download did not produce a valid cutout"
    if not calibration.ok:
        state = "calibration_missing"
        reason = calibration.reason
    elif cutout and cutout.get("validation_status") in VALIDATION_OK and _cutout_file_exists(config, cutout):
        spectral_id = calibration.products["spectral_wcs"]["calibration_id"]
        solid_id = calibration.products["solid_angle_pixel_map"]["calibration_id"]
        measurement_id = measurement_id_for(
            source_id=source["source_id"],
            cutout_key=plan_row["cutout_key"],
            cutout_sha256=cutout.get("sha256"),
            spectral_wcs_calibration_id=spectral_id,
            solid_angle_calibration_id=solid_id,
            config=config,
        )
        if valid_measurement_exists(conn, measurement_id):
            state = "photometry_valid"
            reason = "matching photometry already exists"
        else:
            state = "cutout_valid_measurement_missing"
            reason = "downloaded cutout is valid and needs photometry"
    refreshed = upsert_work_item(
        conn,
        photometry_run_id=photometry_run_id,
        source=source,
        plan_row=plan_row,
        cutout=cutout,
        calibration_resolution=calibration,
        state=state,
        reason=reason,
        config=config,
    )
    return refreshed


def _mark_terminal(
    conn,
    workflow_run_id: str,
    source_states: dict[str, _SourceState],
    item: dict[str, Any],
    *,
    measured: bool = False,
    skipped: bool = False,
    failed: bool = False,
) -> None:
    source_id = item["source"]["source_id"]
    state = source_states[source_id]
    state.terminal += 1
    if measured:
        state.measured += 1
    if skipped:
        state.skipped += 1
    if failed:
        state.failed += 1
    _record_source_state(conn, workflow_run_id, state, "running")


def _record_source_state(conn, workflow_run_id: str, state: _SourceState, source_state: str) -> None:
    conn.execute(
        """
        INSERT INTO workflow_source_states(
          workflow_run_id, source_id, state, planned_count, terminal_count,
          measured_count, skipped_count, failed_count, output_status,
          cleanup_status, reason, updated_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workflow_run_id, source_id) DO UPDATE SET
          state=excluded.state,
          planned_count=excluded.planned_count,
          terminal_count=excluded.terminal_count,
          measured_count=excluded.measured_count,
          skipped_count=excluded.skipped_count,
          failed_count=excluded.failed_count,
          output_status=excluded.output_status,
          cleanup_status=excluded.cleanup_status,
          reason=excluded.reason,
          updated_at=excluded.updated_at,
          payload_json=excluded.payload_json
        """,
        (
            workflow_run_id,
            state.source["source_id"],
            source_state,
            state.planned,
            state.terminal,
            state.measured,
            state.skipped,
            state.failed,
            state.output_status,
            state.cleanup_status,
            None,
            utcnow(),
            canonical_json({"source_name": state.source.get("source_name")}),
        ),
    )
    conn.commit()


def _finalize_ready_sources(
    conn,
    config: Config,
    workflow_run_id: str,
    photometry_run_id: str,
    source_states: dict[str, _SourceState],
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    finalized_sources: set[str],
    cleanup_policy: str,
    keep_failed_cutouts: bool,
    qa_level: str | None,
    console,
    verbose: bool,
) -> None:
    for source_id, state in source_states.items():
        if source_id in finalized_sources or state.terminal < state.planned:
            continue
        _finalize_source(
            conn,
            config,
            workflow_run_id,
            photometry_run_id,
            state,
            summary,
            logger,
            cleanup_policy,
            keep_failed_cutouts,
            qa_level,
            console,
            verbose,
        )
        finalized_sources.add(source_id)


def _drain_full_qa_pending(
    conn,
    config: Config,
    workflow_run_id: str,
    photometry_run_id: str,
    source_states: dict[str, _SourceState],
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    cleanup_policy: str,
    keep_failed_cutouts: bool,
    console,
    verbose: bool,
) -> None:
    pending = [state for state in source_states.values() if state.full_qa_pending and state.measurements]
    if not pending:
        return
    qa_started = time.perf_counter()
    qa_total = sum(len(state.measurements) for state in pending)
    summary.qa_plots_planned += qa_total
    logger.emit(
        conn,
        "full_qa_start",
        f"Full QA plotting started: sources={len(pending)} measurements={qa_total} workers={config.photometry.qa.full_plot_workers}",
        payload={"sources": len(pending), "measurements": qa_total, "workers": config.photometry.qa.full_plot_workers},
        console=console,
        verbose=verbose,
    )

    progress_state = {"last": 0}

    def progress_callback(event: dict[str, Any]) -> None:
        written = int(event.get("qa_written") or 0)
        total = int(event.get("qa_total") or qa_total)
        phase = str(event.get("phase") or "qa_plot")
        if phase == "qa_start" or written == total or written - progress_state["last"] >= 25:
            progress_state["last"] = written
            logger.emit(
                conn,
                "full_qa_progress",
                f"Full QA progress: {written}/{total}",
                payload={"phase": phase, "qa_written": written, "qa_total": total},
                console=console,
                verbose=verbose,
            )

    write_full_measurement_qa_batch(
        config=config,
        source_measurements=[(state.source, state.measurements) for state in pending],
        progress_callback=progress_callback,
    )
    qa_seconds = time.perf_counter() - qa_started
    summary.qa_seconds += qa_seconds
    for state in pending:
        measurements = _load_measurements_for_source(conn, state.source["source_id"])
        if measurements:
            write_full_qa_manifest(config=config, source=state.source, measurements=measurements)
        if measurements and validate_full_qa_outputs(config=config, source=state.source, measurements=measurements):
            state.full_qa_status = "valid"
            state.full_qa_pending = False
            written = _record_full_qa_products(conn, photometry_run_id, config, state.source, measurements)
            summary.qa_plots_written += written
            if cleanup_policy != "never":
                deleted, deleted_bytes = _cleanup_source_cutouts(
                    conn,
                    config,
                    workflow_run_id,
                    state.source,
                    policy=cleanup_policy,
                    keep_failed_cutouts=keep_failed_cutouts,
                    outputs_valid=True,
                )
                state.cleanup_status = "complete"
                summary.cleanup_deleted += deleted
                summary.cleanup_deleted_bytes += deleted_bytes
            else:
                state.cleanup_status = "skipped"
        else:
            state.full_qa_status = "failed"
            failed = len(measurements or state.measurements)
            summary.qa_plots_failed += failed
            summary.outputs_failed += 1
        paths = source_output_paths(config, state.source)
        upsert_source_summary(
            conn,
            photometry_run_id=photometry_run_id,
            source_id=state.source["source_id"],
            status="source_partial_success" if state.failed else "source_complete",
            n_planned=state.planned,
            n_measured=_measurement_count_for_source(conn, state.source["source_id"]),
            n_failed=state.failed,
            n_science_recommended=_science_count_for_source(conn, state.source["source_id"]),
            paths=paths,
            config=config,
            summary={"workflow_run_id": workflow_run_id, "terminal": state.terminal, "full_qa_status": state.full_qa_status},
        )
    logger.emit(
        conn,
        "full_qa_complete",
        f"Full QA plotting complete: written={summary.qa_plots_written} failed={summary.qa_plots_failed} seconds={qa_seconds:.2f}",
        payload={"qa_plots_written": summary.qa_plots_written, "qa_plots_failed": summary.qa_plots_failed, "qa_seconds": qa_seconds},
        console=console,
        verbose=verbose,
    )


def _finalize_source(
    conn,
    config: Config,
    workflow_run_id: str,
    photometry_run_id: str,
    state: _SourceState,
    summary: WorkflowSummary,
    logger: _WorkflowLogger,
    cleanup_policy: str,
    keep_failed_cutouts: bool,
    qa_level: str | None,
    console,
    verbose: bool,
    *,
    cleanup: bool = True,
) -> None:
    source = state.source
    paths = source_output_paths(config, source)
    measurements = _load_measurements_for_source(conn, source["source_id"])
    failures = _load_failures_for_source(conn, source["source_id"])
    outputs_ok_before = validate_source_outputs(paths, config=config, source=source, measurements=measurements)
    should_write = (not outputs_ok_before) and (
        config.workflow.regenerate_missing_outputs or state.measured > 0 or state.failed > 0
    )
    if should_write:
        write_level = qa_level or config.photometry.qa_level
        if write_level == "full" and not measurements:
            write_level = "standard"
        written = write_source_outputs(
            config=config,
            source=source,
            measurements=measurements,
            failures=failures,
            qa_level="standard" if write_level == "full" else write_level,
            write_full_qa=False,
        )
        if not validate_source_outputs(written, config=config, source=source, measurements=measurements):
            summary.outputs_failed += 1
            state.output_status = "failed"
            _record_source_state(conn, workflow_run_id, state, "source_output_failed")
            raise RuntimeError(f"output validation failed for source {source['source_id']}")
        for product_type, key in [
            ("spectrum_csv", "csv"),
            ("sed_plot", "sed"),
            ("qa_summary", "qa"),
            ("provenance_json", "provenance"),
            ("measurement_index", "index"),
            ("output_manifest", "manifest"),
        ]:
            record_output_product(
                conn,
                photometry_run_id=photometry_run_id,
                source_id=source["source_id"],
                product_type=product_type,
                path=written[key],
                config=config,
                commit=False,
            )
        status = "source_failed" if state.failed and state.measured == 0 and state.skipped == 0 else (
            "source_partial_success" if state.failed else "source_complete"
        )
        upsert_source_summary(
            conn,
            photometry_run_id=photometry_run_id,
            source_id=source["source_id"],
            status=status,
            n_planned=state.planned,
            n_measured=_measurement_count_for_source(conn, source["source_id"]),
            n_failed=state.failed,
            n_science_recommended=_science_count_for_source(conn, source["source_id"]),
            paths=written,
            config=config,
            summary={"workflow_run_id": workflow_run_id, "terminal": state.terminal},
        )
        state.output_status = "rebuilt" if outputs_ok_before else "written"
        summary.outputs_rebuilt += 1
    else:
        state.output_status = "valid"
        summary.outputs_valid += 1

    full_qa_required = (qa_level or config.photometry.qa_level) == "full" and bool(measurements)
    full_qa_current = False
    if full_qa_required:
        full_qa_current = validate_full_qa_outputs(config=config, source=source, measurements=measurements)
        if not full_qa_current and state.measurements:
            state.full_qa_pending = True
            state.full_qa_status = "pending"
        elif not full_qa_current and full_qa_files_exist(config=config, source=source, measurements=measurements):
            write_full_qa_manifest(config=config, source=source, measurements=measurements)
            full_qa_current = validate_full_qa_outputs(config=config, source=source, measurements=measurements)
            state.full_qa_status = "valid" if full_qa_current else "stale"
        elif full_qa_current:
            state.full_qa_status = "valid"
        else:
            state.full_qa_status = "missing_unavailable"

    if cleanup and (not full_qa_required or full_qa_current):
        deleted, deleted_bytes = _cleanup_source_cutouts(
            conn,
            config,
            workflow_run_id,
            source,
            policy=cleanup_policy,
            keep_failed_cutouts=keep_failed_cutouts,
            outputs_valid=validate_source_outputs(source_output_paths(config, source), config=config, source=source, measurements=measurements),
        )
        state.cleanup_status = "complete" if cleanup_policy != "never" else "skipped"
        summary.cleanup_deleted += deleted
        summary.cleanup_deleted_bytes += deleted_bytes
    elif cleanup and full_qa_required and not full_qa_current:
        state.cleanup_status = "deferred_full_qa" if state.full_qa_pending else "blocked_full_qa_missing"
    state.finalized = True
    source_status = "source_complete" if state.failed == 0 else ("source_failed" if state.measured == 0 and state.skipped == 0 else "source_partial")
    _record_source_state(conn, workflow_run_id, state, source_status)
    if state.failed == 0:
        summary.sources_complete += 1
    elif state.measured or state.skipped:
        summary.sources_partial += 1
    else:
        summary.sources_failed += 1
    logger.emit(
        conn,
        "source_finalized",
        f"Source finalized: {source.get('source_name') or source['source_id']} outputs={state.output_status} cleanup={state.cleanup_status}",
        source_id=source["source_id"],
        payload={"planned": state.planned, "terminal": state.terminal, "measured": state.measured, "failed": state.failed},
        console=console,
        verbose=verbose,
    )


def _load_measurements_for_source(conn, source_id: str) -> list[Any]:
    rows = conn.execute(
        """
        SELECT row_json, provenance_json
        FROM photometry_measurements
        WHERE source_id = ?
        ORDER BY wavelength_um, measurement_id
        """,
        (source_id,),
    ).fetchall()
    measurements = []
    for row in rows:
        measurement_row = json.loads(row["row_json"]) if row["row_json"] else {}
        provenance = json.loads(row["provenance_json"]) if row["provenance_json"] else {}
        measurements.append(SimpleNamespace(row=measurement_row, provenance=provenance, qa_arrays={}))
    return measurements


def _load_failures_for_source(conn, source_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT work_item_id, failure_type, status, reason, exception_class, retryable, created_at
        FROM photometry_failures
        WHERE source_id = ? AND resolved_at IS NULL
        ORDER BY created_at
        """,
        (source_id,),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _cleanup_source_cutouts(
    conn,
    config: Config,
    workflow_run_id: str,
    source: dict[str, Any],
    *,
    policy: str,
    keep_failed_cutouts: bool,
    outputs_valid: bool,
) -> tuple[int, int]:
    if policy == "never":
        return 0, 0
    if not outputs_valid:
        return 0, 0
    cutout_root = (config.project.data_root / "cutouts").resolve()
    rows = conn.execute(
        """
        SELECT *
        FROM cutouts
        WHERE source_id = ? AND active = 1 AND file_exists = 1
        ORDER BY cutout_key
        """,
        (source["source_id"],),
    ).fetchall()
    deleted = 0
    deleted_bytes = 0
    for row in rows:
        status = str(row["validation_status"] or "")
        if keep_failed_cutouts and status not in VALIDATION_OK:
            _record_cleanup(conn, workflow_run_id, source, row, policy, "skipped", "failed cutout retained", 0)
            continue
        path = resolve_project_path(config, row["local_path"])
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(cutout_root):
                _record_cleanup(conn, workflow_run_id, source, row, policy, "skipped", "outside project cutout root", 0)
                continue
        except FileNotFoundError:
            resolved = path
        existing = conn.execute(
            "SELECT status FROM cleanup_ledger WHERE cutout_key = ? AND policy = ?",
            (row["cutout_key"], policy),
        ).fetchone()
        if existing and existing["status"] == "deleted":
            continue
        size = path.stat().st_size if path.exists() else 0
        if path.exists():
            path.unlink()
            deleted += 1
            deleted_bytes += size
        conn.execute("UPDATE cutouts SET file_exists = 0 WHERE cutout_id = ?", (row["cutout_id"],))
        _record_cleanup(conn, workflow_run_id, source, row, policy, "deleted", "safe temporary cutout cleanup", size)
    conn.commit()
    return deleted, deleted_bytes


def _record_cleanup(conn, workflow_run_id: str, source: dict[str, Any], row, policy: str, status: str, reason: str, deleted_bytes: int) -> None:
    conn.execute(
        """
        INSERT INTO cleanup_ledger(
          workflow_run_id, source_id, cutout_id, cutout_key, local_path, policy,
          status, reason, deleted_bytes, deleted_at, created_at, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cutout_key, policy) DO UPDATE SET
          workflow_run_id=excluded.workflow_run_id,
          source_id=excluded.source_id,
          cutout_id=excluded.cutout_id,
          local_path=excluded.local_path,
          status=excluded.status,
          reason=excluded.reason,
          deleted_bytes=excluded.deleted_bytes,
          deleted_at=excluded.deleted_at,
          payload_json=excluded.payload_json
        """,
        (
            workflow_run_id,
            source["source_id"],
            row["cutout_id"],
            row["cutout_key"],
            row["local_path"],
            policy,
            status,
            reason,
            int(deleted_bytes or 0),
            utcnow() if status == "deleted" else None,
            utcnow(),
            canonical_json({"source_name": source.get("source_name")}),
        ),
    )


def _record_item_failure_once(
    conn,
    photometry_run_id: str,
    item: dict[str, Any],
    failure_type: str,
    reason: str,
    *,
    exception_class: str | None = None,
    retryable: bool = False,
) -> None:
    existing = conn.execute(
        """
        SELECT 1 FROM photometry_failures
        WHERE work_item_id = ? AND failure_type = ? AND status = 'open'
        LIMIT 1
        """,
        (item.get("work_item_id"), failure_type),
    ).fetchone()
    if existing:
        return
    record_photometry_failure(
        conn,
        photometry_run_id=photometry_run_id,
        work_item_id=item.get("work_item_id"),
        source_id=item.get("source", {}).get("source_id"),
        product_id=item.get("plan_row", {}).get("product_id"),
        cutout_id=(item.get("cutout") or {}).get("cutout_id"),
        failure_type=failure_type,
        reason=reason,
        exception_class=exception_class,
        retryable=retryable,
    )


def _record_full_qa_products(conn, photometry_run_id: str, config: Config, source: dict[str, Any], measurements: list) -> int:
    written = 0
    for measurement in measurements:
        measurement_id = str(getattr(measurement, "row", {}).get("measurement_id") or "")
        if not measurement_id:
            continue
        path = full_qa_measurement_path(config, source, measurement_id)
        if not path.exists() or path.stat().st_size <= 0:
            continue
        record_output_product(
            conn,
            photometry_run_id=photometry_run_id,
            source_id=source["source_id"],
            measurement_id=measurement_id,
            product_type="measurement_qa_png",
            path=path,
            config=config,
            commit=False,
        )
        written += 1
    conn.commit()
    return written


def _download_batch_limit(
    conn,
    config: Config,
    *,
    max_inflight_cutouts: int,
    max_live_cutout_gb: float,
) -> int:
    live_count, live_bytes = _live_cutout_usage(conn, config)
    byte_limit = int(max_live_cutout_gb * 1024**3)
    if live_count >= max_inflight_cutouts:
        return 0
    if byte_limit and live_bytes >= byte_limit:
        return 0
    return max(1, max_inflight_cutouts - live_count)


def _live_cutout_usage(conn, config: Config) -> tuple[int, int]:
    root = (config.project.data_root / "cutouts").resolve()
    rows = conn.execute("SELECT local_path FROM cutouts WHERE file_exists = 1").fetchall()
    count = 0
    total = 0
    for row in rows:
        path = resolve_project_path(config, row["local_path"])
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
        except FileNotFoundError:
            continue
        if path.exists():
            count += 1
            total += path.stat().st_size
    return count, total


def _cutout_file_exists(config: Config, cutout: dict[str, Any]) -> bool:
    path = cutout.get("local_path")
    if not path:
        return False
    return resolve_project_path(config, path).exists()


def _measurement_count_for_source(conn, source_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM photometry_measurements WHERE source_id = ?", (source_id,)).fetchone()[0])


def _science_count_for_source(conn, source_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM photometry_measurements WHERE source_id = ? AND science_recommended = 1",
            (source_id,),
        ).fetchone()[0]
    )


def _planned_count_for_source(conn, source_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM photometry_work_items WHERE source_id = ?", (source_id,)).fetchone()[0])


def _write_run_summary(config: Config, workflow_run_id: str, summary: WorkflowSummary) -> Path:
    root = config.photometry.output_root / "summaries"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"workflow_summary_{workflow_run_id}.json"
    path.write_text(json.dumps(asdict(summary), indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def _write_catalog_summary(conn, config: Config, workflow_run_id: str, summary: WorkflowSummary) -> Path:
    root = config.photometry.output_root / "summaries"
    root.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        """
        SELECT s.source_id, s.source_name,
               COALESCE(ps.n_planned, 0) AS n_planned,
               COALESCE(ps.n_measured, 0) AS n_measured,
               COALESCE(ps.n_failed, 0) AS n_failed,
               COALESCE(ps.n_science_recommended, 0) AS n_science_recommended,
               ps.source_status, ps.spectrum_path, ps.sed_plot_path,
               ps.qa_summary_path, ps.provenance_path
        FROM sources s
        LEFT JOIN photometry_source_summaries ps ON ps.source_id = s.source_id
        WHERE s.active = 1
        ORDER BY COALESCE(s.priority, 999999), s.source_id
        """
    ).fetchall()
    df = pd.DataFrame([{key: row[key] for key in row.keys()} for row in rows])
    csv_path = root / "catalog_summary.csv"
    json_path = root / "catalog_summary.json"
    df.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps({"workflow_run_id": workflow_run_id, "summary": asdict(summary)}, indent=2, sort_keys=True), encoding="utf-8")
    return csv_path


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _attach_operator_hints(summary: WorkflowSummary) -> None:
    hints: list[str] = list(summary.operator_hints)
    calibration_missing = int(summary.states.get("calibration_missing", 0) or 0)
    if calibration_missing:
        hint = (
            "Required calibration is missing for "
            f"{calibration_missing} planned work item(s); affected cutouts are not downloaded "
            "until calibration is available. Run `spxcutdb calibration sync --project <project> "
            "--product required` or rerun `spxcutdb run ... --sync-calibration`."
        )
        if hint not in hints:
            hints.append(hint)
    missing_cutouts = int(summary.states.get("cutout_missing_or_invalid", 0) or 0)
    if missing_cutouts and summary.queued_download == 0 and summary.downloaded == 0 and summary.blocked:
        hint = (
            "Missing cutouts were not submitted to the downloader. Check `--download-missing` "
            "and storage backpressure limits."
        )
        if hint not in hints:
            hints.append(hint)
    summary.operator_hints = hints


def _normalize_cleanup_policy(value: str) -> str:
    if value == "successful":
        return "success-after-source"
    if value == "none":
        return "never"
    return value


def _rel_or_str(path: Path, config: Config) -> str:
    try:
        return str(Path(path).resolve().relative_to(config.project.root))
    except ValueError:
        return str(path)
