from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import os
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from astropy.io import fits
from rich.console import Console

import spherex_cutoutdb.calibration.sync as calibration_sync
from spherex_cutoutdb.calibration import sync_calibrations
from spherex_cutoutdb.calibration import resolve_required_calibrations
from spherex_cutoutdb.catalog import ingest_catalog
from spherex_cutoutdb.cli import main
from spherex_cutoutdb.config import ensure_project_directories, load_config, write_default_config
from spherex_cutoutdb.database import (
    connect,
    initialize_schema,
    record_validation,
    stable_hash,
    upsert_cutout_record,
)
from spherex_cutoutdb.photometry.measurement_plan import build_photometry_plan
from spherex_cutoutdb.photometry.outputs import source_output_paths
from spherex_cutoutdb.photometry.solid_angle import ARCSEC2_TO_SR, image_to_microjy_per_pixel
import spherex_cutoutdb.photometry.workflow as photometry_workflow
from spherex_cutoutdb.photometry.workflow import run_photometry, run_source_photometry
from spherex_cutoutdb.planner import make_download_plan_records
from spherex_cutoutdb.validator import validate_cutout


def test_solid_angle_conversion_units():
    image = np.ones((2, 2))
    variance = np.ones((2, 2)) * 4
    solid = np.ones((2, 2)) * ARCSEC2_TO_SR
    flux, var = image_to_microjy_per_pixel(image, variance, solid)
    assert np.allclose(flux, ARCSEC2_TO_SR * 1e12)
    assert np.allclose(var, 4 * (ARCSEC2_TO_SR * 1e12) ** 2)


def test_photometry_source_run_outputs_and_resume_skip(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    record = _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=-12.0)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    assert not cal_summary.missing

    items, counts = build_photometry_plan(conn, cfg, source_ids=["M101"])
    assert counts == {"cutout_valid_measurement_missing": 1}
    assert items[0]["plan_row"]["cutout_key"] == record["cutout_key"]

    summary = run_source_photometry(conn, cfg, source_id="M101", progress=False)

    assert summary.measured == 1
    assert summary.failed == 0
    csv_path = Path(summary.output_paths["csv"])
    assert csv_path.exists()
    df = pd.read_csv(csv_path)
    assert len(df) == 1
    assert df.loc[0, "point_flux_uJy"] < 0
    assert df.loc[0, "detection_status"] == "negative_flux"
    assert not bool(df.loc[0, "science_recommended"])
    assert (tmp_path / record["local_path"]).exists() is False

    _, counts2 = build_photometry_plan(conn, cfg, source_ids=["M101"])
    assert counts2 == {"photometry_valid": 1}
    conn.close()


def test_photometry_force_rerun_remeasures_current_identity(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    record = _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=10.0)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0

    first = run_source_photometry(conn, cfg, source_id="M101", progress=False)
    assert first.measured == 1
    row1 = conn.execute("SELECT measurement_id, selected_flux_uJy FROM photometry_measurements").fetchone()
    first_flux = float(row1["selected_flux_uJy"])
    assert first_flux > 0

    _, counts_skip = build_photometry_plan(conn, cfg, source_ids=["M101"])
    _, counts_force = build_photometry_plan(conn, cfg, source_ids=["M101"], force_remeasure=True)
    assert counts_skip == {"photometry_valid": 1}
    assert counts_force == {"cutout_valid_measurement_missing": 1}

    _make_photometry_cutout(tmp_path / record["local_path"], flux_uJy=45.0)
    second = run_source_photometry(conn, cfg, source_id="M101", progress=False, force_remeasure=True)
    assert second.measured == 1
    row2 = conn.execute("SELECT measurement_id, selected_flux_uJy FROM photometry_measurements").fetchone()
    assert row2["measurement_id"] == row1["measurement_id"]
    assert float(row2["selected_flux_uJy"]) > first_flux * 2
    conn.close()


def test_v5_photometry_rows_are_stale_after_v6_schema_bump(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    old_cfg = cfg.model_copy(deep=True)
    old_cfg.photometry.output_schema_version = "photometry_native_v5"
    old_cfg.photometry.code_version = "photometry_psf_wls_v5_bkg2d_targetprotect"
    old_cfg.photometry.cleanup.delete_successful_cutouts = False

    old = run_source_photometry(conn, old_cfg, source_id="M101", progress=False, qa_level="minimal")
    _, counts = build_photometry_plan(conn, cfg, source_ids=["M101"])

    assert old.measured == 1
    assert counts == {"cutout_valid_measurement_missing": 1}
    conn.close()


def test_photometry_clean_results_cli_allows_normal_rerun(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    summary = run_source_photometry(conn, cfg, source_id="M101", progress=False, qa_level="minimal")
    csv_path = Path(summary.output_paths["csv"])
    assert csv_path.exists()
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == 1
    conn.close()

    assert main(["photometry", "clean-results", "--project", str(tmp_path), "--source-id", "M101", "--yes"]) == 0

    conn = connect(cfg.project.database_path)
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == 0
    assert not csv_path.exists()
    _, counts = build_photometry_plan(conn, cfg, source_ids=["M101"])
    assert counts == {"cutout_valid_measurement_missing": 1}
    rerun = run_source_photometry(conn, cfg, source_id="M101", progress=False, qa_level="minimal")
    assert rerun.measured == 1
    assert Path(rerun.output_paths["csv"]).exists()
    conn.close()


def test_photometry_source_does_not_download_missing_cutouts(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    cfg.download.max_workers = 4
    cfg.download.concurrency = 4
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    records = [
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=12.0, product_label="synthetic_a"),
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0, product_label="synthetic_b"),
    ]
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0

    for record in records:
        path = tmp_path / record["local_path"]
        path.unlink()
        conn.execute("UPDATE cutouts SET file_exists = 0 WHERE cutout_key = ?", (record["cutout_key"],))
    conn.commit()

    summary = run_source_photometry(conn, cfg, source_id="M101", progress=False, qa_level="minimal")

    assert summary.downloaded == 0
    assert summary.measured == 0
    assert summary.failed == 2
    assert conn.execute("SELECT COUNT(*) FROM download_plan").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM photometry_failures WHERE failure_type = 'input'").fetchone()[0] == 2
    conn.close()


def test_photometry_progress_and_verbose_output(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0

    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=200)
    summary = run_photometry(
        conn,
        cfg,
        source_ids=["M101"],
        progress=False,
        verbose=True,
        console=console,
        qa_level="minimal",
    )

    text = output.getvalue()
    assert summary.measured == 1
    assert "Photometry run: sources=1" in text
    assert "Photometry sources: total=1" in text
    assert "Photometry source M101" in text
    assert "Photometry cutouts" in text
    assert "measured cutout=" in text
    assert "flux=" in text
    assert "Outputs for M101" in text
    assert "Cleanup for M101" in text
    conn.close()


def test_photometry_run_auto_caps_unsafe_legacy_full_qa_workers(tmp_path, tiny_catalog_path, capsys):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["runtime"]["max_source_workers"] = 64
    data["runtime"]["max_fit_workers"] = 32
    data["runtime"]["max_open_fits_files"] = 512
    data["runtime"]["global_max_open_fits_files"] = 512
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config(tmp_path, cfg_path)
    ensure_project_directories(cfg)
    conn = connect(cfg.project.database_path)
    try:
        initialize_schema(conn)
        ingest_catalog(conn, cfg, "cat_run")
        for idx in range(6):
            _insert_source(conn, f"EXTRA_{idx}", f"Extra {idx}", 10.0 + idx, -2.0 - idx)
    finally:
        conn.close()

    rc = main([
        "photometry",
        "run",
        "--project",
        str(tmp_path),
        "--qa-level",
        "full",
        "--cleanup-cutouts",
        "none",
        "--verbose",
        "--no-progress",
    ])

    assert rc == 0
    text = capsys.readouterr().out
    assert "Photometry run workers: effective=2 configured=64" in text
    assert "auto_capped_from=64" in text


def test_multi_source_run_persists_measurement_rows_before_catalog_completion(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    sources = [
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone()),
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'SPHERExDemo'").fetchone()),
    ]
    for idx, source in enumerate(sources):
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=14.0 + idx, product_label=f"persist_{idx}")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    observed_counts: list[int] = []
    original_write = photometry_workflow.write_source_outputs

    def observe_write(*args, **kwargs):
        db = connect(cfg.project.database_path)
        try:
            observed_counts.append(db.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0])
        finally:
            db.close()
        return original_write(*args, **kwargs)

    monkeypatch.setattr(photometry_workflow, "write_source_outputs", observe_write)

    summary = run_photometry(
        conn,
        cfg,
        source_ids=[source["source_id"] for source in sources],
        max_source_workers=2,
        progress=False,
        qa_level="minimal",
    )

    assert summary.measured == 2
    assert observed_counts
    assert min(observed_counts) >= 1
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == 2
    conn.close()


def test_photometry_run_progress_events_are_item_level_before_source_done(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    sources = [
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone()),
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'SPHERExDemo'").fetchone()),
    ]
    for idx, source in enumerate(sources):
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=16.0 + idx, product_label=f"progress_{idx}")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    events: list[dict] = []

    summary = run_photometry(
        conn,
        cfg,
        source_ids=[source["source_id"] for source in sources],
        max_source_workers=2,
        progress=False,
        qa_level="minimal",
        progress_callback=events.append,
    )

    names = [event.get("event") for event in events]
    assert summary.measured == 2
    assert "photometry_item" in names
    assert "photometry_source_done" in names
    assert names.index("photometry_item") < names.index("photometry_source_done")
    assert any((event.get("summary") or {}).get("measured") for event in events if event.get("event") == "photometry_item")
    conn.close()


def test_process_backend_reports_worker_pids_and_persists_before_outputs(tmp_path, tiny_catalog_path, monkeypatch):
    fake_pid = _patch_fake_process_backend(monkeypatch)
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    _insert_source(conn, "PROC_EXTRA", "Process Extra", 22.0, -4.0)
    sources = [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()]
    for idx, source in enumerate(sources):
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=24.0 + idx, product_label=f"process_{idx}")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    events: list[dict] = []
    observed_counts: list[int] = []
    original_write = photometry_workflow.write_source_outputs

    def observe_write(*args, **kwargs):
        db = connect(cfg.project.database_path)
        try:
            observed_counts.append(db.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0])
        finally:
            db.close()
        return original_write(*args, **kwargs)

    monkeypatch.setattr(photometry_workflow, "write_source_outputs", observe_write)

    summary = run_photometry(
        conn,
        cfg,
        source_ids=[source["source_id"] for source in sources],
        max_source_workers=2,
        progress=False,
        qa_level="minimal",
        worker_backend="process",
        progress_callback=events.append,
    )

    assert summary.worker_backend == "process"
    assert summary.worker_pid_count == 1
    assert any(event.get("worker_pid") == fake_pid for event in events)
    assert observed_counts and min(observed_counts) >= 1
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == len(sources)
    assert conn.execute("SELECT COUNT(*) FROM photometry_output_products WHERE product_type = 'spectrum_csv'").fetchone()[0] == len(sources)
    conn.close()


def test_process_backend_matches_single_source_measurement(tmp_path, tiny_catalog_path, monkeypatch):
    _patch_fake_process_backend(monkeypatch)
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    sources = [
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone()),
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'SPHERExDemo'").fetchone()),
    ]
    for idx, source in enumerate(sources):
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=30.0, product_label=f"equiv_{idx}")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0

    source_summary = run_source_photometry(conn, cfg, source_id="M101", progress=False, qa_level="minimal")
    process_summary = run_photometry(
        conn,
        cfg,
        source_ids=["SPHERExDemo"],
        max_source_workers=2,
        progress=False,
        qa_level="minimal",
        worker_backend="process",
    )

    assert source_summary.measured == 1
    assert process_summary.worker_backend == "process"
    assert process_summary.measured == 1
    rows = {
        row["source_id"]: row["selected_flux_uJy"]
        for row in conn.execute("SELECT source_id, selected_flux_uJy FROM photometry_measurements").fetchall()
    }
    assert np.isclose(rows["M101"], rows["SPHERExDemo"], rtol=0.02)
    conn.close()


def test_process_backend_records_failed_fit_without_stopping_successes(tmp_path, tiny_catalog_path, monkeypatch):
    _patch_fake_process_backend(monkeypatch)
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    sources = [
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone()),
        dict(conn.execute("SELECT * FROM sources WHERE source_id = 'SPHERExDemo'").fetchone()),
    ]
    good = _insert_product_and_cutout(conn, cfg, tmp_path, sources[0], flux_uJy=25.0, product_label="process_good")
    bad = _insert_product_and_cutout(conn, cfg, tmp_path, sources[1], flux_uJy=25.0, product_label="process_bad")
    (tmp_path / bad["local_path"]).write_bytes(b"not a fits file")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0

    summary = run_photometry(
        conn,
        cfg,
        source_ids=[source["source_id"] for source in sources],
        max_source_workers=2,
        progress=False,
        qa_level="minimal",
        worker_backend="process",
    )

    assert good["cutout_key"]
    assert summary.measured == 1
    assert summary.failed == 1
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == 1
    failure = conn.execute("SELECT failure_type, reason FROM photometry_failures").fetchone()
    assert failure["failure_type"] == "fit"
    assert "fits" in failure["reason"].lower()
    conn.close()


def test_full_qa_runs_after_compact_outputs_and_registry_are_durable(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0, product_label="full_qa_durable")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    observations: list[dict[str, object]] = []

    def observe_full_qa(*, config, source, measurements, progress_callback=None):
        paths = source_output_paths(config, source)
        db = connect(config.project.database_path)
        try:
            observations.append(
                {
                    "csv_exists": paths["csv"].exists(),
                    "provenance_exists": paths["provenance"].exists(),
                    "manifest_exists": paths["manifest"].exists(),
                    "registry_rows": db.execute("SELECT COUNT(*) FROM photometry_output_products").fetchone()[0],
                    "summary_rows": db.execute("SELECT COUNT(*) FROM photometry_source_summaries").fetchone()[0],
                    "measurements": len(measurements),
                }
            )
        finally:
            db.close()
        if progress_callback is not None:
            progress_callback({"phase": "qa_start", "qa_written": 0, "qa_total": len(measurements)})
            progress_callback({"phase": "qa_plot", "qa_written": len(measurements), "qa_total": len(measurements)})

    monkeypatch.setattr(photometry_workflow, "write_full_measurement_qa_outputs", observe_full_qa)

    summary = run_source_photometry(conn, cfg, source_id="M101", progress=False, qa_level="full")

    assert summary.measured == 1
    assert observations == [
        {
            "csv_exists": True,
            "provenance_exists": True,
            "manifest_exists": True,
            "registry_rows": 6,
            "summary_rows": 1,
            "measurements": 1,
        }
    ]
    conn.close()


def test_photometry_cli_full_qa_small_project_smoke(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg_path = tmp_path / "spherex_cutoutdb.yaml"
    config_data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    config_data["photometry"]["psf_template_radius_pixels"] = 2
    config_data["photometry"]["deblending"]["enabled"] = False
    config_data["photometry"]["cleanup"]["delete_successful_cutouts"] = False
    cfg_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    cfg = load_config(tmp_path, cfg_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    _insert_source(conn, "CLI_SMOKE", "CLI Smoke", 41.0, -7.0)
    sources = [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()]
    for idx, source in enumerate(sources):
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=22.0 + idx, product_label=f"cli_smoke_{idx}")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    conn.close()

    rc = main([
        "photometry",
        "run",
        "--project",
        str(tmp_path),
        "--limit-sources",
        "3",
        "--qa-level",
        "full",
        "--max-source-workers",
        "2",
        "--worker-backend",
        "process",
        "--qa-workers",
        "2",
        "--verbose",
        "--cleanup-cutouts",
        "none",
        "--no-progress",
    ])

    assert rc == 0
    spectra = sorted((tmp_path / "results" / "spectra").glob("*.csv"))
    qa_pngs = sorted((tmp_path / "results" / "qa").glob("*/measurements/*_qa.png"))
    assert len(spectra) == 3
    assert len(qa_pngs) == 3


def test_parallel_photometry_stress_has_no_sqlite_write_conflicts(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.photometry.cleanup.delete_successful_cutouts = False
    sources = [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()]
    for idx in range(6):
        _insert_source(conn, f"STRESS_{idx}", f"Stress {idx}", 30.0 + idx, 5.0 + idx)
    sources = [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()]
    for idx, source in enumerate(sources):
        _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=20.0 + idx, product_label=f"stress_{idx}")
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0

    summary = run_photometry(
        conn,
        cfg,
        source_ids=[source["source_id"] for source in sources],
        max_source_workers=8,
        progress=False,
        qa_level="minimal",
    )

    assert summary.measured == len(sources)
    assert summary.failed == 0
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == len(sources)
    assert conn.execute("SELECT COUNT(*) FROM photometry_output_products WHERE product_type = 'spectrum_csv'").fetchone()[0] == len(sources)
    assert conn.execute("SELECT COUNT(*) FROM photometry_source_summaries").fetchone()[0] == len(sources)
    conn.close()


def test_photometry_plan_does_not_match_calibration_to_l2_processing_date(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    record = _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=5.0)
    conn.execute(
        "UPDATE discovery_products SET processing_version = ?, processing_date = ? WHERE product_id = ?",
        ("l2b-v24", "2026-089", record["product_id"]),
    )
    conn.execute(
        "UPDATE cutouts SET processing_version = ?, processing_date = ? WHERE cutout_key = ?",
        ("l2b-v24", "2026-089", record["cutout_key"]),
    )
    conn.commit()
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    assert not cal_summary.missing

    items, counts = build_photometry_plan(conn, cfg, source_ids=["M101"])

    assert counts == {"cutout_valid_measurement_missing": 1}
    assert items[0]["calibration"].ok is True
    assert items[0]["calibration"].detector_release_match is True
    assert items[0]["calibration"].header_reference_match is False
    assert items[0]["calibration"].exact_match is False
    assert items[0]["calibration"].match_quality == "detector_release_match"
    conn.close()


def test_calibration_header_reference_exact_match_when_ids_match(tmp_path, tiny_catalog_path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    rows = {
        row["product_type"]: row["calibration_id"]
        for row in conn.execute("SELECT product_type, calibration_id FROM calibration_products")
    }

    resolution = resolve_required_calibrations(
        conn,
        cfg,
        {
            "detector_id": 3,
            "spectral_wcs_calibration_id": rows["spectral_wcs"],
            "solid_angle_calibration_id": rows["solid_angle_pixel_map"],
        },
    )

    assert resolution.ok
    assert resolution.detector_release_match
    assert resolution.header_reference_match
    assert resolution.exact_match
    assert resolution.match_quality == "exact_match"
    conn.close()


def test_calibration_sync_discovers_official_ibe_products(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.calibration.download_source = "ibe"

    def fake_listing(config, path):
        listings = {
            "spectral_wcs": [{"name": "cal-wcs-v4-2025-254", "size": "-"}],
            "solid_angle_pixel_map": [{"name": "cal-sapm-v2-2025-164", "size": "-"}],
            "spectral_wcs/cal-wcs-v4-2025-254/3": [
                {"name": "spectral_wcs_D3_spx_cal-wcs-v4-2025-254.fits", "size": "1"}
            ],
            "solid_angle_pixel_map/cal-sapm-v2-2025-164/3": [
                {"name": "solid_angle_pixel_map_D3_spx_cal-sapm-v2-2025-164.fits", "size": "1"}
            ],
        }
        return listings[path]

    def fake_download(url, target, config):
        target.parent.mkdir(parents=True, exist_ok=True)
        if "spectral_wcs" in target.name:
            _write_spectral_wcs(target)
        else:
            _write_solid_angle(target)

    monkeypatch.setattr(calibration_sync, "_read_ibe_listing", fake_listing)
    monkeypatch.setattr(calibration_sync, "_download_or_copy_url", fake_download)

    summary = sync_calibrations(conn, cfg, products=["required"], detectors=[3])

    assert summary.downloaded == 2
    assert summary.failed == 0
    assert not summary.missing
    rows = conn.execute(
        """
        SELECT product_type, detector_id, source_url, validation_status
        FROM calibration_products
        ORDER BY product_type
        """
    ).fetchall()
    assert [(row["product_type"], row["detector_id"], row["validation_status"]) for row in rows] == [
        ("solid_angle_pixel_map", 3, "valid"),
        ("spectral_wcs", 3, "valid"),
    ]
    assert all("https://irsa.ipac.caltech.edu/ibe/data/spherex/qr2/" in row["source_url"] for row in rows)
    conn.close()


def test_calibration_sync_prefers_cloud_s3_products(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.calibration.download_source = "cloud"

    def fake_prefixes(config, prefix):
        assert prefix in {"qr2/spectral_wcs/", "qr2/solid_angle_pixel_map/"}
        if prefix == "qr2/spectral_wcs/":
            return ["qr2/spectral_wcs/cal-wcs-v4-2025-254/"]
        return ["qr2/solid_angle_pixel_map/cal-sapm-v2-2025-164/"]

    def fake_objects(config, prefix):
        if prefix == "qr2/spectral_wcs/cal-wcs-v4-2025-254/":
            return [
                {
                    "key": "qr2/spectral_wcs/cal-wcs-v4-2025-254/3/spectral_wcs_D3_spx_cal-wcs-v4-2025-254.fits",
                    "name": "spectral_wcs_D3_spx_cal-wcs-v4-2025-254.fits",
                    "size": "1",
                }
            ]
        return [
            {
                "key": "qr2/solid_angle_pixel_map/cal-sapm-v2-2025-164/3/solid_angle_pixel_map_D3_spx_cal-sapm-v2-2025-164.fits",
                "name": "solid_angle_pixel_map_D3_spx_cal-sapm-v2-2025-164.fits",
                "size": "1",
            }
        ]

    def fail_ibe(*args, **kwargs):
        raise AssertionError("IBE fallback should not be used when cloud has all files")

    def fake_download(url, target, config):
        assert "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/qr2/" in url
        target.parent.mkdir(parents=True, exist_ok=True)
        if "spectral_wcs" in target.name:
            _write_spectral_wcs(target)
        else:
            _write_solid_angle(target)

    monkeypatch.setattr(calibration_sync, "_read_s3_common_prefixes", fake_prefixes)
    monkeypatch.setattr(calibration_sync, "_read_s3_objects", fake_objects)
    monkeypatch.setattr(calibration_sync, "_official_ibe_download_tasks", fail_ibe)
    monkeypatch.setattr(calibration_sync, "_download_or_copy_url", fake_download)

    summary = sync_calibrations(conn, cfg, products=["required"], detectors=[3])

    assert summary.downloaded == 2
    assert summary.failed == 0
    assert not summary.missing
    rows = conn.execute("SELECT source_url FROM calibration_products ORDER BY product_type").fetchall()
    assert all("https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/qr2/" in row["source_url"] for row in rows)
    conn.close()


def test_calibration_sync_auto_chooses_faster_source(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cfg.calibration.download_source = "auto"

    monkeypatch.setattr(
        calibration_sync,
        "_official_s3_download_tasks",
        lambda config, products, detectors: [
            calibration_sync.CalibrationDownloadTask(
                product="spectral_wcs",
                detector=3,
                url="https://cloud.example/spectral_wcs_D3.fits",
                target=config.calibration.cache_root / "QR2/spectral_wcs/v/D3/spectral_wcs_D3.fits",
                expected_size=1,
            )
        ],
    )
    monkeypatch.setattr(
        calibration_sync,
        "_official_ibe_download_tasks",
        lambda config, products, detectors: [
            calibration_sync.CalibrationDownloadTask(
                product="spectral_wcs",
                detector=3,
                url="https://ibe.example/spectral_wcs_D3.fits",
                target=config.calibration.cache_root / "QR2/spectral_wcs/v/D3/spectral_wcs_D3.fits",
                expected_size=1,
            )
        ],
    )
    monkeypatch.setattr(
        calibration_sync,
        "_probe_download_score",
        lambda url, config: 10.0 if "ibe.example" in url else 1.0,
    )

    seen_urls = []

    def fake_download(url, target, config):
        seen_urls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_spectral_wcs(target)

    monkeypatch.setattr(calibration_sync, "_download_or_copy_url", fake_download)

    summary = sync_calibrations(conn, cfg, products=["spectral_wcs"], detectors=[3])

    assert summary.downloaded == 1
    assert seen_urls == ["https://ibe.example/spectral_wcs_D3.fits"]
    assert summary.failed == 0
    conn.close()


class _FakeProcessPoolExecutor:
    def __init__(self, max_workers):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._executor.shutdown(wait=True, cancel_futures=True)

    def submit(self, fn, *args, **kwargs):
        return self._executor.submit(fn, *args, **kwargs)


def _patch_fake_process_backend(monkeypatch) -> int:
    fake_pid = os.getpid() + 100000
    real_worker = photometry_workflow._measure_item_process_worker

    def fake_worker(payload):
        out = real_worker(payload)
        out["worker_pid"] = fake_pid
        return out

    monkeypatch.setattr(photometry_workflow, "_process_pool_unavailable_reason", lambda: None)
    monkeypatch.setattr(photometry_workflow, "ProcessPoolExecutor", _FakeProcessPoolExecutor)
    monkeypatch.setattr(photometry_workflow, "_measure_item_process_worker", fake_worker)
    return fake_pid


def _photometry_project(tmp_path: Path, catalog: Path):
    cfg_path = write_default_config(tmp_path, catalog)
    cfg = load_config(
        tmp_path,
        cfg_path,
        {
            "photometry": {
                "psf_template_radius_pixels": 2,
                "cleanup": {"delete_successful_cutouts": True},
                "deblending": {"enabled": False},
            }
        },
    )
    ensure_project_directories(cfg)
    conn = connect(cfg.project.database_path)
    initialize_schema(conn)
    ingest_catalog(conn, cfg, "cat_run")
    return cfg, conn


def _insert_source(conn, source_id: str, source_name: str, ra_deg: float, dec_deg: float) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO sources(
          source_id, source_name, ra_deg, dec_deg, cutout_size_arcsec, active,
          row_hash, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, 1, ?, datetime('now'), datetime('now'))
        """,
        (
            source_id,
            source_name,
            ra_deg,
            dec_deg,
            60.0,
            stable_hash({"source_id": source_id, "ra_deg": ra_deg, "dec_deg": dec_deg}),
        ),
    )
    conn.commit()


def _insert_product_and_cutout(
    conn,
    cfg,
    tmp_path: Path,
    source: dict,
    *,
    flux_uJy: float,
    product_label: str = "synthetic",
) -> dict:
    product_signature = stable_hash({"product": product_label, "detector": 3})
    conn.execute(
        """
        INSERT INTO discovery_products(
          collection, obs_collection, obs_id, observation_id, detector_id, bandpass,
          access_url, access_format, parent_filename, processing_version, processing_date,
          product_signature, row_hash, raw_sia_json, first_discovered_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """,
        (
            "spherex_qr2",
            "spherex_qr2",
            f"OBS_{product_label}",
            f"OBS_{product_label}",
            3,
            "D3",
            "https://example.test/synthetic_parent.fits",
            "application/fits",
            f"{product_label}_D3_parent.fits",
            "l2b-v20",
            "2025-164",
            product_signature,
            product_signature,
            "{}",
        ),
    )
    product_id = conn.execute("SELECT product_id FROM discovery_products WHERE product_signature = ?", (product_signature,)).fetchone()[0]
    conn.execute(
        """
        INSERT INTO source_product_matches(
          source_id, product_id, collection, query_ra_deg, query_dec_deg, search_radius_deg,
          dist_to_point, coverage_status, match_hash, active, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))
        """,
        (
            source["source_id"],
            product_id,
            "spherex_qr2",
            source["ra_deg"],
            source["dec_deg"],
            1.0 / 3600.0,
            0.0,
            "covered",
            stable_hash({"source": source["source_id"], "product": product_id}),
        ),
    )
    conn.commit()
    record = next(row for row in make_download_plan_records(conn, None, cfg, [source["source_id"]]) if row["product_id"] == product_id)
    cutout_path = tmp_path / record["local_path"]
    _make_photometry_cutout(cutout_path, flux_uJy=flux_uJy, ra_deg=float(source["ra_deg"]), dec_deg=float(source["dec_deg"]))
    validation = validate_cutout(cutout_path, cfg)
    cutout_id = upsert_cutout_record(
        conn,
        {
            **record,
            "file_exists": True,
            "file_size_bytes": validation.file_size_bytes,
            "sha256": validation.sha256,
            "validation_status": validation.status,
        },
    )
    record_validation(
        conn,
        {
            "cutout_id": cutout_id,
            "local_path": record["local_path"],
            "status": validation.status,
            "reason": validation.reason,
            "warnings": validation.warnings,
            "errors": validation.errors,
            "file_size_bytes": validation.file_size_bytes,
            "sha256": validation.sha256,
            "required_hdus_present": validation.required_hdus_present,
            "image_shape": validation.image_shape,
            "flags_shape": validation.flags_shape,
            "variance_shape": validation.variance_shape,
            "zodi_shape": validation.zodi_shape,
            "psf_shape": validation.psf_shape,
            "wcwave_summary": validation.wcwave_summary,
            "spatial_wcs_valid": validation.wcs_summary.get("spatial_wcs_valid", False),
            "spectral_wcs_valid": validation.wcs_summary.get("spectral_wcs_valid", False),
            "hdu_summary": validation.hdu_summary,
            "wcs_summary": validation.wcs_summary,
            "psf_metadata": validation.psf_metadata,
            "header_metadata": validation.header_metadata,
        },
    )
    return record


def _make_photometry_cutout(path: Path, *, flux_uJy: float, ra_deg: float = 210.80227, dec_deg: float = 54.34895) -> None:
    shape = (25, 25)
    center = (12, 12)
    solid_factor = ARCSEC2_TO_SR * 1e12
    yy, xx = np.indices(shape)
    template = np.exp(-0.5 * (((xx - center[1]) / 1.1) ** 2 + ((yy - center[0]) / 1.1) ** 2))
    template = template / template.sum()
    background_mjy_sr = 1.0
    image = np.full(shape, background_mjy_sr, dtype="f4") + (flux_uJy * template / solid_factor).astype("f4")
    variance = np.full(shape, (2.0 / solid_factor) ** 2, dtype="f4")
    flags = np.zeros(shape, dtype="i2")
    zodi = np.zeros(shape, dtype="f4")
    header = fits.Header()
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRVAL1"] = ra_deg
    header["CRVAL2"] = dec_deg
    header["CRPIX1"] = center[1] + 1
    header["CRPIX2"] = center[0] + 1
    header["CD1_1"] = -0.0002777778
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = 0.0002777778
    header["DETECTOR"] = 3
    header["PROCVER"] = "l2b-v20"
    header["PROCDATE"] = "2025-164"
    psf = np.exp(-0.5 * (((np.indices((5, 5))[1] - 2) / 1.1) ** 2 + ((np.indices((5, 5))[0] - 2) / 1.1) ** 2)).astype("f4")
    psf = psf / psf.sum()
    hdus = [
        fits.PrimaryHDU(),
        fits.ImageHDU(image, header=header, name="IMAGE"),
        fits.ImageHDU(flags, name="FLAGS"),
        fits.ImageHDU(variance, name="VARIANCE"),
        fits.ImageHDU(zodi, name="ZODI"),
        fits.ImageHDU(psf, name="PSF"),
        fits.BinTableHDU.from_columns(
            [fits.Column(name="WAVELENGTH", array=np.array([2.0], dtype="f4"), format="E")],
            name="WCS-WAVE",
        ),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList(hdus).writeto(path, overwrite=True)


def _make_calibrations(root: Path, *, detector: int) -> Path:
    spec_dir = root / "spectral_wcs" / "cal-wcs-v1-2025-164" / f"D{detector}"
    sapm_dir = root / "solid_angle_pixel_map" / "cal-sapm-v1-2025-164" / f"D{detector}"
    spec_dir.mkdir(parents=True, exist_ok=True)
    sapm_dir.mkdir(parents=True, exist_ok=True)
    shape = (25, 25)
    _write_spectral_wcs(spec_dir / "spectral_wcs_D3_spx_cal-wcs-v1-2025-164.fits", shape=shape)
    _write_solid_angle(sapm_dir / "solid_angle_pixel_map_D3_spx_cal-sapm-v1-2025-164.fits", shape=shape)
    return root


def _write_spectral_wcs(path: Path, shape: tuple[int, int] = (25, 25)) -> None:
    fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(np.full(shape, 2.0, dtype="f4"), name="CWAVE"),
            fits.ImageHDU(np.full(shape, 0.1, dtype="f4"), name="CBAND"),
        ]
    ).writeto(path, overwrite=True)


def _write_solid_angle(path: Path, shape: tuple[int, int] = (25, 25)) -> None:
    fits.HDUList([fits.PrimaryHDU(np.ones(shape, dtype="f4"))]).writeto(path, overwrite=True)
