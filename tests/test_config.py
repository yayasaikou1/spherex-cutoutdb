from __future__ import annotations

import pytest
import yaml

from spherex_cutoutdb.cli import main
from spherex_cutoutdb.config import Config, config_hash, load_config, write_default_config
from spherex_cutoutdb.exceptions import ConfigError


def test_default_config_round_trip(tmp_path, tiny_catalog_path):
    path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, path)
    assert cfg.project.database_path.name == "cutoutdb.sqlite"
    assert "spherex_qr2" in cfg.discovery.collections
    assert "spherex_qr2_cal" not in cfg.discovery.collections
    assert cfg.download.max_workers == 64
    assert cfg.download.concurrency == 4096
    assert cfg.calibration.required_products == ["spectral_wcs", "solid_angle_pixel_map"]
    assert cfg.calibration.download_source == "cloud"
    assert cfg.calibration.prefer_cloud is True
    assert cfg.calibration.use_official_ibe is True
    assert cfg.calibration.download_max_workers == 64
    assert cfg.photometry.output_schema_version == "photometry_native_v6"
    assert cfg.photometry.code_version == "photometry_psf_wls_v6_nozodi_failclosed_bkgfallback"
    assert cfg.photometry.qa.full_plot_workers == 32
    assert cfg.photometry.background.engine == "photutils"
    assert cfg.photometry.psf.oversampling_factor == 10
    assert "SOURCE" in cfg.photometry.masks.background_exclude_bits
    assert "SOURCE" not in cfg.photometry.masks.fit_exclude_bits
    assert cfg.runtime.sqlite_writer == "manager"
    assert cfg.runtime.max_source_workers == 64
    assert cfg.runtime.max_fit_workers == 32
    assert cfg.runtime.max_open_fits_files == 512
    assert cfg.runtime.max_download_workers == 32
    assert cfg.download.per_host_rate_limit_per_second == 4096
    assert cfg.download.per_host_max_concurrency == 2048
    assert config_hash(cfg) == config_hash(load_config(tmp_path, path))


def test_repository_default_config_matches_model_defaults():
    data = yaml.safe_load(open("DEFAULT_CONFIG.yaml", encoding="utf-8"))
    cfg = Config.model_validate(data)
    assert cfg.model_dump(mode="json") == Config().model_dump(mode="json")


def test_download_max_workers_defaults_to_concurrency():
    data = Config().model_dump()
    data["download"]["concurrency"] = 3
    data["download"]["max_workers"] = None
    cfg = Config.model_validate(data)
    assert cfg.download.max_workers == 3


def test_download_rate_limit_can_be_disabled_and_host_concurrency_set():
    data = Config().model_dump()
    data["download"]["per_host_rate_limit_per_second"] = 0
    data["download"]["per_host_max_concurrency"] = 4
    cfg = Config.model_validate(data)
    assert cfg.download.per_host_rate_limit_per_second == 0
    assert cfg.download.per_host_max_concurrency == 4


def test_invalid_cutout_size_fails():
    data = Config().model_dump()
    data["cutouts"]["default_size_arcsec"] = -1
    with pytest.raises(ValueError):
        Config.model_validate(data)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("workflow", "ignored_toggle"),
        ("calibration", "ignored_product_policy"),
        ("photometry", "ignored_science_knob"),
        ("runtime", "ignored_worker_limit"),
        ("cleanup", "ignored_cleanup_policy"),
        ("download", "ignored_downloader_knob"),
    ],
)
def test_config_rejects_unknown_nested_keys(section, key):
    data = Config().model_dump()
    data[section][key] = "must fail"
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Config.model_validate(data)


def test_config_rejects_unknown_top_level_section(tmp_path, tiny_catalog_path):
    path = write_default_config(tmp_path, tiny_catalog_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["silently_ignored_science_section"] = {"enabled": True}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        load_config(tmp_path, path)


def test_cal_collection_rejected(tmp_path, tiny_catalog_path):
    path = write_default_config(tmp_path, tiny_catalog_path)
    data = yaml.safe_load(path.read_text())
    data["discovery"]["collections"] = ["spherex_qr2_cal"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path, path)


def test_cloud_cutout_requires_official_key():
    data = Config().model_dump()
    data["cloud"]["prefer_cloud_for_cutouts"] = True
    data["cloud"]["official_cutout_capability_key"] = None
    with pytest.raises(ValueError):
        Config.model_validate(data)


def test_default_config_infers_name_catalog_columns(tmp_path):
    catalog = tmp_path / "input_catalog.csv"
    catalog.write_text(
        "ID,Name,RA,DEC,Obj. Type,Remarks,RA_deg,DEC_deg\n"
        "1,Target_A,01:00:00,+02:00:00,target,note,15.0,2.0\n",
        encoding="utf-8",
    )
    path = write_default_config(tmp_path, "input_catalog.csv")
    cfg = load_config(tmp_path, path)
    assert cfg.catalog.path == catalog.resolve()
    assert cfg.catalog.source_id_column == "Name"
    assert cfg.catalog.source_name_column == "Name"
    assert cfg.catalog.ra_column == "RA_deg"
    assert cfg.catalog.dec_column == "DEC_deg"
    assert cfg.catalog.optional_columns["source_type"] == "Obj. Type"
    assert cfg.catalog.optional_columns["notes"] == "Remarks"


def test_config_commands_show_validate_defaults_diff_and_record_overrides(tmp_path, capsys):
    project = tmp_path / "project"
    catalog = tmp_path / "named.csv"
    catalog.write_text("Name,RA_deg,DEC_deg\nA,1,2\n", encoding="utf-8")
    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(catalog),
        "--target-id-column",
        "Name",
    ]) == 0
    assert main(["config", "show", "--project", str(project), "--effective", "--hash"]) == 0
    assert "config_hash:" in capsys.readouterr().out
    assert main(["config", "validate", "--project", str(project)]) == 0
    assert "Configuration valid" in capsys.readouterr().out
    assert main(["config", "defaults"]) == 0
    assert "max_download_workers" in capsys.readouterr().out
    assert main(["config", "diff", "--project", str(project), "--against-defaults"]) == 0
    assert "project.root" in capsys.readouterr().out

    batch_config = tmp_path / "batch_config.example.yaml"
    batch_config.write_text(
        yaml.safe_dump(
            {
                "workflow": {"download_missing": True},
                "runtime": {"max_fit_workers": 2, "max_download_workers": 3},
                "cleanup": {"cutouts": "success-after-source"},
            }
        ),
        encoding="utf-8",
    )
    assert main([
        "config",
        "show",
        "--project",
        str(project),
        "--batch-config",
        str(batch_config),
        "--format",
        "json",
    ]) == 0
    config_show_output = capsys.readouterr().out
    assert '"download_missing": true' in config_show_output
    assert '"max_fit_workers": 2' in config_show_output
    assert '"max_download_workers": 3' in config_show_output
    assert '"cutouts": "success-after-source"' in config_show_output

    assert main(["config", "validate", "--project", str(project), "--batch-config", str(batch_config)]) == 0
    assert "Configuration valid" in capsys.readouterr().out
    assert main([
        "config",
        "diff",
        "--project",
        str(project),
        "--batch-config",
        str(batch_config),
        "--against-defaults",
    ]) == 0
    assert "runtime.max_fit_workers" in capsys.readouterr().out

    assert main(["catalog", "ingest", "--project", str(project)]) == 0
    cfg = load_config(project)
    run_rows = list((project / "runs").glob("run_*"))
    assert run_rows
    assert (run_rows[-1] / "effective_config.yaml").exists()
    assert (run_rows[-1] / "effective_config.json").exists()
    assert (run_rows[-1] / "cli_overrides.json").exists()
    assert config_hash(cfg)


def test_config_validate_rejects_duplicate_or_missing_name(tmp_path):
    project = tmp_path / "project"
    duplicate = tmp_path / "dupe.csv"
    duplicate.write_text("Name,RA_deg,DEC_deg\nA,1,2\nA,3,4\n", encoding="utf-8")
    assert main([
        "init",
        "--project",
        str(project),
        "--catalog",
        str(duplicate),
        "--target-id-column",
        "Name",
    ]) == 0
    assert main(["config", "validate", "--project", str(project)]) == 2

    missing = tmp_path / "missing.csv"
    missing.write_text("ID,RA_deg,DEC_deg\n1,1,2\n", encoding="utf-8")
    data = yaml.safe_load((project / "spherex_cutoutdb.yaml").read_text(encoding="utf-8"))
    data["catalog"]["path"] = str(missing)
    (project / "spherex_cutoutdb.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["config", "validate", "--project", str(project)]) == 2


def test_default_config_resolves_relative_catalog_from_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    catalog = tmp_path / "external.csv"
    catalog.write_text("ID,Name,RA_deg,DEC_deg\n1,A,1,2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    path = write_default_config(project, "external.csv")
    cfg = load_config(project, path)
    assert cfg.catalog.path == catalog.resolve()
