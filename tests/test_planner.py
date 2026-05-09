from __future__ import annotations

from pathlib import Path

from spherex_cutoutdb.catalog import ingest_catalog
from spherex_cutoutdb.config import ensure_project_directories, load_config, write_default_config
from spherex_cutoutdb.database import connect, initialize_schema, upsert_cutout_record, upsert_discovery_products, upsert_source_product_matches
from spherex_cutoutdb.irsa_sia import build_match_rows, normalize_sia_dataframe, read_votable_file
from spherex_cutoutdb.planner import plan_downloads


def _setup_discovery(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, cfg_path)
    ensure_project_directories(cfg)
    conn = connect(cfg.project.database_path)
    initialize_schema(conn)
    ingest_catalog(conn, cfg, "run_cat")
    source = dict(conn.execute("SELECT * FROM sources WHERE source_id = 'M101'").fetchone())
    sia = read_votable_file(Path(__file__).parent / "data" / "mock_sia_response.xml")
    rows = normalize_sia_dataframe(sia, source_id="M101", collection="spherex_qr2")
    product_ids = upsert_discovery_products(conn, "run_disc", rows)
    matches = build_match_rows(source, rows, product_ids, cfg)
    upsert_source_product_matches(conn, "run_disc", matches)
    return cfg, conn


def test_planner_download_then_skip_valid(tmp_path, tiny_catalog_path):
    cfg, conn = _setup_discovery(tmp_path, tiny_catalog_path)
    plan = plan_downloads(conn, "run_plan_1", cfg)
    assert len(plan) == 1
    assert plan.iloc[0]["action"] == "download"
    final_path = cfg.project.root / plan.iloc[0]["local_path"]
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"placeholder")
    upsert_cutout_record(
        conn,
        {
            "cutout_key": plan.iloc[0]["cutout_key"],
            "source_id": plan.iloc[0]["source_id"],
            "product_id": int(plan.iloc[0]["product_id"]),
            "local_path": plan.iloc[0]["local_path"],
            "file_exists": True,
            "file_size_bytes": final_path.stat().st_size,
            "sha256": "abc",
            "access_method": "onprem_cutout",
            "validation_status": "passed",
        },
    )
    plan2 = plan_downloads(conn, "run_plan_2", cfg)
    assert plan2.iloc[0]["action"] == "skip_valid"
    conn.close()
