from __future__ import annotations

import pandas as pd
import pytest

from spherex_cutoutdb.catalog import normalize_sources, read_source_catalog, validate_sources
from spherex_cutoutdb.config import load_config, write_default_config


def test_catalog_validate_csv(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, cfg_path)
    raw = read_source_catalog(cfg)
    normalized = normalize_sources(raw, cfg)
    report = validate_sources(normalized, cfg)
    assert report.valid
    assert report.n_rows_valid == 2
    assert set(normalized.columns) >= {"source_id", "ra_deg", "dec_deg", "row_hash"}


def test_catalog_duplicate_source_id_fails(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, cfg_path)
    df = pd.DataFrame(
        {
            "source_id": ["A", "A"],
            "source_name": ["A", "A2"],
            "ra_deg": [1.0, 2.0],
            "dec_deg": [1.0, 2.0],
        }
    )
    report = validate_sources(normalize_sources(df, cfg), cfg)
    assert not report.valid
    assert any("duplicate source_id" in error for error in report.errors)


def test_catalog_invalid_radec_fails(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, cfg_path)
    df = pd.DataFrame({"source_id": ["A"], "ra_deg": [360.0], "dec_deg": [91.0]})
    report = validate_sources(normalize_sources(df, cfg), cfg)
    assert not report.valid
    assert report.n_rows_invalid == 1
