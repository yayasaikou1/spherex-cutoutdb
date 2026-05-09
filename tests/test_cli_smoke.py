from __future__ import annotations

from pathlib import Path

import pytest

from spherex_cutoutdb.cli import main


def test_cli_smoke_workflow(tmp_path, tiny_catalog_path):
    project = tmp_path / "project"
    mock_sia = Path(__file__).parent / "data" / "mock_sia_response.xml"
    assert main(["init", str(project), "--catalog", str(tiny_catalog_path)]) == 0
    assert main(["catalog", "validate", "--project", str(project)]) == 0
    assert main(["catalog", "ingest", "--project", str(project)]) == 0
    assert main(["discover", "--project", str(project), "--mock-sia", str(mock_sia), "--limit-sources", "1"]) == 0
    assert main(["plan", "--project", str(project), "--source-name", "M101", "--export-plan"]) == 0
    assert main([
        "download",
        "--project",
        str(project),
        "--dry-run",
        "--max-workers",
        "2",
        "--per-host-rate-limit",
        "0",
        "--per-host-max-concurrency",
        "2",
        "--retry-count",
        "2",
        "--timeout",
        "30",
        "--skip-existing",
    ]) == 0
    assert main(["export-manifest", "--project", str(project), "--format", "csv"]) == 0
    assert main(["coverage", "--project", str(project)]) == 0
    assert (project / "db" / "cutoutdb.sqlite").exists()
    assert (project / "manifests" / "latest_sources.csv").exists()


def test_cli_rejects_cal_collection(tmp_path, tiny_catalog_path):
    project = tmp_path / "project"
    assert main(["init", str(project), "--catalog", str(tiny_catalog_path)]) == 0
    assert main(["catalog", "ingest", "--project", str(project)]) == 0
    rc = main(["discover", "--project", str(project), "--collections", "spherex_qr2_cal"])
    assert rc == 2


def test_cli_download_empty_plan_does_not_crash(tmp_path, tiny_catalog_path):
    project = tmp_path / "project"
    assert main(["init", str(project), "--catalog", str(tiny_catalog_path)]) == 0
    assert main(["download", "--project", str(project), "--max-downloads", "1", "--max-workers", "2"]) == 0


def test_cli_sync_accepts_max_workers(tmp_path, tiny_catalog_path):
    project = tmp_path / "project"
    mock_sia = Path(__file__).parent / "data" / "mock_sia_response.xml"
    assert main(["init", str(project), "--catalog", str(tiny_catalog_path)]) == 0
    assert main([
        "sync",
        "--project",
        str(project),
        "--mock-sia",
        str(mock_sia),
        "--limit-sources",
        "1",
        "--max-workers",
        "2",
        "--skip-download",
    ]) == 0


def test_cli_calibration_and_photometry_help():
    for argv in [
        ["--help"],
        ["config", "--help"],
        ["config", "show", "--help"],
        ["config", "validate", "--help"],
        ["config", "defaults", "--help"],
        ["config", "diff", "--help"],
        ["run", "--help"],
        ["summary", "--help"],
        ["discover", "--help"],
        ["validate", "--help"],
    ]:
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
    with pytest.raises(SystemExit) as cal:
        main(["calibration", "--help"])
    assert cal.value.code == 0
    with pytest.raises(SystemExit) as alias:
        main(["calib", "--help"])
    assert alias.value.code == 0
    with pytest.raises(SystemExit) as phot:
        main(["photometry", "--help"])
    assert phot.value.code == 0
    with pytest.raises(SystemExit) as rerun:
        main(["photometry", "rerun", "--help"])
    assert rerun.value.code == 0
    with pytest.raises(SystemExit) as clean_results:
        main(["photometry", "clean-results", "--help"])
    assert clean_results.value.code == 0
