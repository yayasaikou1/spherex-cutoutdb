from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
import time

import numpy as np
import pandas as pd
import yaml

import spherex_cutoutdb.integrated_workflow as integrated
from spherex_cutoutdb.calibration import sync_calibrations
from spherex_cutoutdb.catalog import ingest_catalog
from spherex_cutoutdb.cli import _load_batch_config_overrides, main
from spherex_cutoutdb.config import load_config
from spherex_cutoutdb.database import connect, initialize_schema, stable_hash, utcnow
from spherex_cutoutdb.downloader import CompletedDownload
from spherex_cutoutdb.models import DownloadResult
from spherex_cutoutdb.photometry.measure import MeasurementResult
from spherex_cutoutdb.planner import make_download_plan_records

from test_photometry_pipeline import _insert_product_and_cutout, _make_calibrations, _photometry_project


def test_init_target_id_column_name_sets_source_identity(tmp_path, tiny_catalog_path):
    project = tmp_path / "project"

    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(tiny_catalog_path),
        "--target-id-column",
        "Name",
    ]) == 0

    cfg = load_config(project)
    assert cfg.catalog.source_id_column == "Name"
    assert cfg.catalog.source_name_column == "Name"
    assert cfg.catalog.generate_missing_source_id is False
    assert cfg.catalog.allow_missing_name is False


def test_integrated_run_measures_existing_cutout_without_downloader(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)

    def fail_downloader(*args, **kwargs):
        raise AssertionError("downloader should not be called for valid existing cutouts")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)

    summary = integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)

    assert summary.measured == 1
    assert summary.downloaded == 0
    assert summary.cleanup_deleted == 1
    assert (tmp_path / "results" / "spectra" / "M101.csv").exists()
    conn.close()


def test_integrated_run_full_qa_writes_measurement_png_registry_and_then_cleans(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    cutout = _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)
    cutout_path = tmp_path / cutout["local_path"]
    assert cutout_path.exists()

    def fail_downloader(*args, **kwargs):
        raise AssertionError("downloader should not be called for valid existing cutouts")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)

    summary = integrated.run_catalog_workflow(
        conn,
        cfg,
        download_missing=True,
        qa_level="full",
        cleanup_cutouts="success-after-source",
        progress=False,
    )

    qa_pngs = sorted((tmp_path / "results" / "qa").glob("*/measurements/*_qa.png"))
    manifest = yaml.safe_load((tmp_path / "results" / "provenance" / "M101_output_manifest.json").read_text(encoding="utf-8"))
    registry_count = conn.execute(
        "SELECT COUNT(*) FROM photometry_output_products WHERE product_type = 'measurement_qa_png'"
    ).fetchone()[0]
    assert summary.measured == 1
    assert summary.qa_plots_written == 1
    assert len(qa_pngs) == 1
    assert manifest["full_qa"]["complete"] is True
    assert registry_count == 1
    assert not cutout_path.exists()
    conn.close()


def test_integrated_full_qa_rerun_rebuilds_missing_png_without_duplicate_measurement(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)

    first = integrated.run_catalog_workflow(
        conn,
        cfg,
        download_missing=True,
        qa_level="full",
        cleanup_cutouts="never",
        progress=False,
    )
    assert first.measured == 1
    qa_png = next((tmp_path / "results" / "qa").glob("*/measurements/*_qa.png"))
    qa_png.unlink()

    def fail_downloader(*args, **kwargs):
        raise AssertionError("QA rebuild from an existing cutout must not download")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)
    second = integrated.run_catalog_workflow(
        conn,
        cfg,
        download_missing=True,
        qa_level="full",
        cleanup_cutouts="never",
        progress=False,
    )

    measurement_count = conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0]
    assert second.measured == 1
    assert measurement_count == 1
    assert qa_png.exists() and qa_png.stat().st_size > 0
    conn.close()


def test_integrated_full_qa_missing_after_cutout_cleanup_does_not_redownload(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    cutout = _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=18.0)

    first = integrated.run_catalog_workflow(
        conn,
        cfg,
        download_missing=True,
        qa_level="standard",
        cleanup_cutouts="success-after-source",
        progress=False,
    )
    assert first.measured == 1
    assert not (tmp_path / cutout["local_path"]).exists()

    def fail_downloader(*args, **kwargs):
        raise AssertionError("valid photometry must not redownload only to make QA PNGs")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)
    second = integrated.run_catalog_workflow(
        conn,
        cfg,
        download_missing=True,
        qa_level="full",
        cleanup_cutouts="success-after-source",
        progress=False,
    )

    qa_pngs = sorted((tmp_path / "results" / "qa").glob("*/measurements/*_qa.png"))
    assert second.already_valid == 1
    assert second.measured == 0
    assert second.downloaded == 0
    assert qa_pngs == []
    assert any("Full QA PNGs are missing" in hint for hint in second.operator_hints)
    conn.close()


def test_valid_measurement_skips_download_after_cutout_cleanup(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    record = _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=-12.0)

    first = integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)
    assert first.measured == 1
    assert (tmp_path / record["local_path"]).exists() is False

    def fail_downloader(*args, **kwargs):
        raise AssertionError("valid photometry must prevent redownload")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)
    second = integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)

    assert second.already_valid == 1
    assert second.downloaded == 0
    assert second.measured == 0
    conn.close()


def test_missing_outputs_rebuild_from_db_without_download(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=21.0)
    integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)
    csv_path = tmp_path / "results" / "spectra" / "M101.csv"
    assert csv_path.exists()
    csv_path.unlink()

    def fail_downloader(*args, **kwargs):
        raise AssertionError("summary output rebuild must not call downloader")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)
    rebuilt = integrated.summarize_workflow_project(conn, cfg, rebuild_missing_outputs=True, qa_level="minimal")

    assert rebuilt.outputs_rebuilt >= 1
    assert csv_path.exists()
    assert pd.read_csv(csv_path).shape[0] == 1
    conn.close()


def test_stale_output_manifest_rebuilds_from_db_without_download(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=22.0)
    integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)
    csv_path = tmp_path / "results" / "spectra" / "M101.csv"
    manifest_path = tmp_path / "results" / "provenance" / "M101_output_manifest.json"
    assert csv_path.exists()
    assert manifest_path.exists()
    csv_path.write_text(csv_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    def fail_downloader(*args, **kwargs):
        raise AssertionError("stale output rebuild must not call downloader")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)
    rebuilt = integrated.summarize_workflow_project(conn, cfg, rebuild_missing_outputs=True, qa_level="minimal")

    assert rebuilt.outputs_rebuilt >= 1
    assert pd.read_csv(csv_path).shape[0] == 1
    conn.close()


def test_missing_cutouts_are_submitted_to_downloader_in_batches(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    for idx in range(3):
        _insert_product_match(conn, source, product_label=f"missing_{idx}")
    captured_batches: list[int] = []

    def fake_iter(conn_arg, run_id, config, *, plan_rows=None, **kwargs):
        rows = list(plan_rows or [])
        captured_batches.append(len(rows))
        for row in rows:
            yield CompletedDownload(
                plan_row=row,
                started_at=utcnow(),
                result=DownloadResult(
                    plan_id=row.get("plan_id"),
                    cutout_key=row["cutout_key"],
                    local_path=Path(row["local_path"]),
                    success=False,
                    status="failed",
                    reason="mock failure",
                ),
            )

    monkeypatch.setattr(integrated, "iter_download_plan_results", fake_iter)

    summary = integrated.run_catalog_workflow(
        conn,
        cfg,
        download_missing=True,
        max_inflight_cutouts=2,
        cleanup_cutouts="never",
        progress=False,
    )

    assert captured_batches == [2, 1]
    assert summary.queued_download == 3
    assert summary.download_failed == 3
    conn.close()


def test_storage_backpressure_blocks_download_when_no_fit_can_relieve(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    other = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'SPHERExDemo'").fetchone())
    _insert_product_match(conn, source, product_label="missing_backpressure")
    live_path = tmp_path / "data" / "cutouts" / "other_live.fits"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_bytes(b"live")
    conn.execute(
        """
        INSERT INTO cutouts(
          cutout_key, source_id, local_path, file_exists, file_size_bytes,
          access_method, validation_status, active
        ) VALUES (?, ?, ?, 1, ?, ?, ?, 1)
        """,
        (
            "other-live-key",
            other["source_id"],
            "data/cutouts/other_live.fits",
            live_path.stat().st_size,
            "onprem_cutout",
            "passed",
        ),
    )
    conn.commit()

    def fail_downloader(*args, **kwargs):
        raise AssertionError("backpressure should pause downloads instead of submitting one more file")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)

    summary = integrated.run_catalog_workflow(
        conn,
        cfg,
        source_ids=["M101"],
        download_missing=True,
        max_inflight_cutouts=1,
        cleanup_cutouts="never",
        progress=False,
    )

    assert summary.backpressure_events == 1
    assert summary.blocked == 1
    assert summary.downloaded == 0
    failure = conn.execute(
        "SELECT failure_type, retryable FROM photometry_failures WHERE failure_type = 'storage_backpressure'"
    ).fetchone()
    assert failure is not None
    assert failure["retryable"] == 1
    conn.close()


def test_storage_backpressure_cleans_current_outputs_then_resumes(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    other = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'SPHERExDemo'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=14.0)
    first = integrated.run_catalog_workflow(
        conn,
        cfg,
        source_ids=["M101"],
        download_missing=True,
        cleanup_cutouts="never",
        qa_level="minimal",
        progress=False,
    )
    assert first.measured == 1
    _insert_product_match(conn, other, product_label="missing_after_cleanup")
    submitted: list[int] = []

    def fake_iter(conn_arg, run_id, config, *, plan_rows=None, **kwargs):
        rows = list(plan_rows or [])
        submitted.append(len(rows))
        for row in rows:
            yield CompletedDownload(
                plan_row=row,
                started_at=utcnow(),
                result=DownloadResult(
                    plan_id=row.get("plan_id"),
                    cutout_key=row["cutout_key"],
                    local_path=Path(row["local_path"]),
                    success=False,
                    status="failed",
                    reason="mock after cleanup",
                ),
            )

    monkeypatch.setattr(integrated, "iter_download_plan_results", fake_iter)
    summary = integrated.run_catalog_workflow(
        conn,
        cfg,
        source_ids=["SPHERExDemo"],
        download_missing=True,
        max_inflight_cutouts=1,
        cleanup_cutouts="success-after-source",
        progress=False,
    )

    assert summary.backpressure_events == 1
    assert summary.cleanup_deleted == 1
    assert submitted == [1]
    assert summary.download_failed == 1
    assert summary.blocked == 0
    conn.close()


def test_calibration_missing_blocks_download_with_operator_hint(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_match(conn, source, product_label="missing_calibration")

    def fail_downloader(*args, **kwargs):
        raise AssertionError("downloader must not run while required calibration is missing")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)

    summary = integrated.run_catalog_workflow(
        conn,
        cfg,
        source_ids=["M101"],
        download_missing=True,
        cleanup_cutouts="never",
        progress=False,
    )

    assert summary.states == {"calibration_missing": 1}
    assert summary.queued_download == 0
    assert summary.downloaded == 0
    assert summary.blocked == 1
    assert any("calibration sync" in hint for hint in summary.operator_hints)
    state = conn.execute("SELECT state FROM photometry_work_items").fetchone()[0]
    assert state == "blocked_calibration_missing"
    conn.close()


def test_photometry_starts_before_all_downloads_finish(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=10.0, product_label="valid_for_fit")
    _insert_product_match(conn, source, product_label="missing_for_download")
    measure_started = Event()

    def fake_measure_cutout(**kwargs):
        measure_started.set()
        time.sleep(0.15)
        return _fake_measurement(kwargs["measurement_id"], kwargs["work_item_id"], kwargs["source"], kwargs["cutout_row"])

    def fake_iter(*args, **kwargs):
        assert measure_started.wait(timeout=2.0)
        for row in kwargs.get("plan_rows") or []:
            yield CompletedDownload(
                plan_row=row,
                started_at=utcnow(),
                result=DownloadResult(
                    plan_id=row.get("plan_id"),
                    cutout_key=row["cutout_key"],
                    local_path=Path(row["local_path"]),
                    success=False,
                    status="failed",
                    reason="mock delayed failure",
                ),
            )

    monkeypatch.setattr(integrated, "measure_cutout", fake_measure_cutout)
    monkeypatch.setattr(integrated, "iter_download_plan_results", fake_iter)

    summary = integrated.run_catalog_workflow(conn, cfg, download_missing=True, cleanup_cutouts="never", progress=False)

    assert summary.measured == 1
    assert summary.download_failed == 1
    conn.close()


def test_cleanup_never_deletes_calibrations(tmp_path, tiny_catalog_path):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    cal_paths = [Path(row["relative_path"]) for row in conn.execute("SELECT relative_path FROM calibration_products")]
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=12.0)

    summary = integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)

    assert summary.cleanup_deleted == 1
    for rel in cal_paths:
        assert (cfg.project.root / rel).exists()
    assert conn.execute("SELECT COUNT(*) FROM cleanup_ledger WHERE status = 'deleted'").fetchone()[0] == 1
    conn.close()


def test_integrated_cli_recommended_workflow_smoke(tmp_path):
    project = tmp_path / "project"
    catalog = tmp_path / "named_catalog.csv"
    batch_config = tmp_path / "batch_config.example.yaml"
    catalog.write_text("Name,RA_deg,DEC_deg\nTDE_A,210.80227,54.34895\n", encoding="utf-8")
    batch_config.write_text(
        yaml.safe_dump(
            {
                "workflow": {"download_missing": True, "skip_valid_measurements": True},
                "cutouts": {"default_size_arcsec": 60},
                "runtime": {"max_download_workers": 2, "max_fit_workers": 1, "max_inflight_cutouts": 4},
                "cleanup": {"cutouts": "success-after-source", "keep_failed_cutouts": True},
            }
        ),
        encoding="utf-8",
    )
    mock_sia = Path(__file__).parent / "data" / "mock_sia_response.xml"

    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    assert main(["validate", "--project", str(project), "--catalog", str(catalog)]) == 0
    assert main([
        "discover",
        "--project",
        str(project),
        "--resume",
        "--mock-sia",
        str(mock_sia),
        "--limit-sources",
        "1",
    ]) == 0
    rc = main([
        "run",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--batch-config",
        str(batch_config),
        "--download-missing",
        "--resume",
        "--cleanup-cutouts",
        "success-after-source",
        "--dry-run",
        "--no-progress",
    ]) == 0
    assert main(["summary", "--project", str(project), "--rebuild-missing-outputs"]) == 0


def test_run_without_discovery_fails_with_recommended_command(tmp_path):
    project = tmp_path / "project_no_discovery"
    catalog = tmp_path / "named_catalog.csv"
    catalog.write_text("Name,RA_deg,DEC_deg\nTDE_A,210.80227,54.34895\n", encoding="utf-8")
    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    assert main(["run", "--project", str(project), "--catalog", str(catalog), "--no-progress"]) == 10


def test_run_discover_from_fresh_project(tmp_path):
    project = tmp_path / "project_discover"
    catalog = tmp_path / "named_catalog.csv"
    catalog.write_text("Name,RA_deg,DEC_deg\nTDE_A,210.80227,54.34895\n", encoding="utf-8")
    mock_sia = Path(__file__).parent / "data" / "mock_sia_response.xml"
    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    rc = main([
        "run",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--discover",
        "--mock-sia",
        str(mock_sia),
        "--no-download",
        "--no-progress",
    ])
    assert rc in {0, 1}
    cfg = load_config(project)
    conn = connect(cfg.project.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_product_matches").fetchone()[0] > 0
    finally:
        conn.close()


def test_batch_config_rejects_unknown_sections(tmp_path):
    project = tmp_path / "bad_batch_project"
    catalog = tmp_path / "catalog.csv"
    batch_config = tmp_path / "bad_batch.yaml"
    catalog.write_text("Name,RA_deg,DEC_deg\nTDE_A,1.0,2.0\n", encoding="utf-8")
    batch_config.write_text("project:\n  root: elsewhere\n", encoding="utf-8")

    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    assert main([
        "run",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--batch-config",
        str(batch_config),
        "--dry-run",
    ]) == 2


def test_batch_config_cutout_default_is_used_by_planner(tmp_path):
    project = tmp_path / "cutout_config_project"
    catalog = tmp_path / "catalog.csv"
    batch_config = tmp_path / "batch_config.example.yaml"
    catalog.write_text("Name,RA_deg,DEC_deg\nTDE_A,12.0,34.0\n", encoding="utf-8")
    batch_config.write_text(
        yaml.safe_dump(
            {
                "cutouts": {
                    "default_size_arcsec": 42.0,
                    "min_size_arcsec": 20.0,
                    "max_size_arcsec": 3600.0,
                    "size_column": "cutout_size_arcsec",
                    "size_unit_for_url": "deg",
                }
            }
        ),
        encoding="utf-8",
    )

    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
        "--default-cutout-size-arcsec",
        "60",
    ]) == 0

    overrides = _load_batch_config_overrides(batch_config)
    cfg = load_config(project, project / "spherex_cutoutdb.yaml", overrides)
    conn = connect(cfg.project.database_path)
    try:
        initialize_schema(conn)
        _, report, _ = ingest_catalog(conn, cfg, None)
        assert report.valid
        source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'TDE_A'").fetchone())
        _insert_product_match(conn, source, product_label="size_from_batch")

        records = make_download_plan_records(conn, None, cfg, ["TDE_A"])
        assert len(records) == 1
        assert records[0]["cutout_size_arcsec"] == 42.0

        conn.execute("UPDATE sources SET cutout_size_arcsec = 25.0 WHERE source_id = 'TDE_A'")
        conn.commit()
        source_records = make_download_plan_records(conn, None, cfg, ["TDE_A"])
        assert source_records[0]["cutout_size_arcsec"] == 25.0
    finally:
        conn.close()


def test_run_update_uses_existing_discovery_path(tmp_path):
    project = tmp_path / "project_update"
    catalog = tmp_path / "named_catalog_update.csv"
    catalog.write_text("Name,RA_deg,DEC_deg\nTDE_A,210.80227,54.34895\n", encoding="utf-8")
    mock_sia = Path(__file__).parent / "data" / "mock_sia_response.xml"
    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    rc = main([
        "run",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--update",
        "--mock-sia",
        str(mock_sia),
        "--no-download",
        "--no-progress",
    ])
    assert rc in {0, 1}
    cfg = load_config(project)
    conn = connect(cfg.project.database_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM source_product_matches").fetchone()[0] > 0
    finally:
        conn.close()


def test_interrupted_style_resume_does_not_duplicate_measurements_or_downloads(tmp_path, tiny_catalog_path, monkeypatch):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=16.0)
    first = integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)
    assert first.measured == 1

    def fail_downloader(*args, **kwargs):
        raise AssertionError("resume should not redownload current valid measurements")

    monkeypatch.setattr(integrated, "iter_download_plan_results", fail_downloader)
    second = integrated.run_catalog_workflow(conn, cfg, download_missing=True, resume=True, qa_level="minimal", progress=False)

    assert second.already_valid == 1
    assert conn.execute("SELECT COUNT(*) FROM photometry_measurements").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM cleanup_ledger WHERE status = 'deleted'").fetchone()[0] == 1
    conn.close()


def test_failed_cutouts_are_retained_by_default(tmp_path, tiny_catalog_path):
    cfg, conn = _prepared_photometry_project(tmp_path, tiny_catalog_path)
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    _insert_product_and_cutout(conn, cfg, tmp_path, source, flux_uJy=11.0, product_label="good")
    failed_path = tmp_path / "data" / "cutouts" / "failed_retained.fits"
    failed_path.parent.mkdir(parents=True, exist_ok=True)
    failed_path.write_bytes(b"not a fits file")
    conn.execute(
        """
        INSERT INTO cutouts(
          cutout_key, source_id, local_path, file_exists, file_size_bytes,
          access_method, validation_status, active
        ) VALUES (?, ?, ?, 1, ?, ?, ?, 1)
        """,
        (
            "failed-retained-key",
            source["source_id"],
            "data/cutouts/failed_retained.fits",
            failed_path.stat().st_size,
            "onprem_cutout",
            "failed_validation",
        ),
    )
    conn.commit()

    integrated.run_catalog_workflow(conn, cfg, download_missing=True, qa_level="minimal", progress=False)

    assert failed_path.exists()
    assert conn.execute(
        "SELECT COUNT(*) FROM cleanup_ledger WHERE cutout_key = 'failed-retained-key' AND status = 'skipped'"
    ).fetchone()[0] == 1
    conn.close()


def test_mock_10000_source_dry_run_is_bounded_and_has_no_cutouts(tmp_path):
    catalog = tmp_path / "large.csv"
    rows = ["Name,RA_deg,DEC_deg"]
    rows.extend(f"SRC_{idx:05d},{(idx % 360) + 0.1:.6f},{-30 + (idx % 60) * 0.1:.6f}" for idx in range(10000))
    catalog.write_text("\n".join(rows) + "\n", encoding="utf-8")
    project = tmp_path / "large_project"
    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    cfg = load_config(project)
    conn = connect(cfg.project.database_path)
    try:
        initialize_schema(conn)
        now = utcnow()
        conn.executemany(
            """
            INSERT INTO sources(
              source_id, source_name, ra_deg, dec_deg, active, row_hash,
              extra_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, 1, ?, '{}', ?, ?)
            """,
            [
                (
                    f"SRC_{idx:05d}",
                    f"SRC_{idx:05d}",
                    (idx % 360) + 0.1,
                    -30 + (idx % 60) * 0.1,
                    stable_hash({"idx": idx}),
                    now,
                    now,
                )
                for idx in range(10000)
            ],
        )
        conn.commit()
        summary = integrated.run_catalog_workflow(conn, cfg, dry_run=True, download_missing=True, progress=False)
        assert summary.sources_total == 10000
        assert summary.planned == 0
        assert conn.execute("SELECT COUNT(*) FROM cutouts").fetchone()[0] == 0
    finally:
        conn.close()


def _prepared_photometry_project(tmp_path: Path, tiny_catalog_path: Path):
    cfg, conn = _photometry_project(tmp_path, tiny_catalog_path)
    cal_summary = sync_calibrations(
        conn,
        cfg,
        products=["required"],
        detectors=[3],
        input_dir=_make_calibrations(tmp_path / "cal_input", detector=3),
    )
    assert cal_summary.failed == 0
    assert not cal_summary.missing
    return cfg, conn


def _insert_product_match(conn, source: dict, *, product_label: str) -> int:
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
            f"https://example.test/{product_label}.fits",
            "application/fits",
            f"{product_label}_D3_parent.fits",
            "l2b-v20",
            "2025-164",
            product_signature,
            product_signature,
            "{}",
        ),
    )
    product_id = conn.execute(
        "SELECT product_id FROM discovery_products WHERE product_signature = ?",
        (product_signature,),
    ).fetchone()[0]
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
    return product_id


def _fake_measurement(measurement_id: str, work_item_id: str, source: dict, cutout: dict) -> MeasurementResult:
    row = {
        "measurement_id": measurement_id,
        "work_item_id": work_item_id,
        "source_id": source["source_id"],
        "source_name": source.get("source_name"),
        "product_id": cutout.get("product_id"),
        "cutout_key": cutout["cutout_key"],
        "cutout_sha256": cutout.get("sha256"),
        "detector_id": cutout.get("detector_id"),
        "observation_id": cutout.get("observation_id"),
        "wavelength_um": 2.0,
        "bandwidth_um": 0.1,
        "point_flux_uJy": 10.0,
        "point_flux_err_uJy": 1.0,
        "joint_flux_uJy": 10.0,
        "joint_flux_err_uJy": 1.0,
        "selected_flux_uJy": 10.0,
        "selected_flux_err_uJy": 1.0,
        "selected_snr": 10.0,
        "science_mode": "point",
        "science_recommended": True,
        "detection_status": "detected",
        "photometry_flags": "",
        "image_flags": 0,
        "fit_quality": 1.0,
        "chi2_reduced": 1.0,
        "n_valid_pixels": 10,
        "background_uJy_per_pixel": 0.0,
        "background_unc_uJy_per_pixel": 0.0,
        "deblend_status": "not_run",
        "n_neighbors": 0,
        "calibration_exact_match": True,
        "spectral_wcs_calibration_id": "spectral",
        "solid_angle_calibration_id": "solid",
    }
    image = np.ones((5, 5), dtype=float)
    qa_arrays = {
        "raw_image": image,
        "background_2d": np.zeros_like(image),
        "data": image,
        "mask_source_map": np.zeros_like(image),
        "point_model": image * 0.8,
        "point_residual_sigma": image * 0.1,
        "joint_model": image * 0.9,
        "joint_residual_sigma": image * 0.05,
    }
    return MeasurementResult(row=row, provenance={"mock": True}, qa_arrays=qa_arrays)
