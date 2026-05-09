from __future__ import annotations

from spherex_cutoutdb.catalog import ingest_catalog
from spherex_cutoutdb.config import load_config, write_default_config
from spherex_cutoutdb.database import connect, initialize_schema, table_count


def test_database_init_and_source_upsert(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    cfg = load_config(tmp_path, cfg_path)
    conn = connect(cfg.project.database_path)
    initialize_schema(conn)
    assert table_count(conn, "schema_migrations") == 1
    _, report, stats = ingest_catalog(conn, cfg, "run_test")
    assert report.n_rows_valid == 2
    assert stats["new"] == 2
    assert table_count(conn, "sources") == 2
    conn.close()
