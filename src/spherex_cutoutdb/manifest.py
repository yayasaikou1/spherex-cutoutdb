"""Manifest export helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from .config import Config
from .database import record_failure, utcnow
from .summary import coverage_dataframe

TABLES = ["sources", "discovery", "plan", "cutouts", "failures", "runs", "coverage"]


def export_manifests(
    conn,
    run_id: str | None,
    config: Config,
    formats: list[str] | None = None,
    tables: list[str] | None = None,
    output_dir: Path | None = None,
) -> list[Path]:
    selected_formats = formats or config.exports.formats
    selected_tables = TABLES if not tables or "all" in tables else tables
    root = output_dir or config.project.manifest_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for table_name in selected_tables:
        df = dataframe_for_manifest(conn, table_name, run_id)
        for fmt in selected_formats:
            try:
                latest = root / f"latest_{_manifest_filename(table_name, fmt)}"
                run_path = root / "runs" / f"{run_id or 'run'}_{_manifest_filename(table_name, fmt)}"
                export_dataframe(df, latest, fmt)
                export_dataframe(df, run_path, fmt)
                for path in [latest, run_path]:
                    sha = _file_sha(path)
                    conn.execute(
                        """
                        INSERT INTO manifest_exports(run_id, table_name, format, path, n_rows, sha256, exported_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            table_name,
                            fmt,
                            _relative(path, config),
                            len(df),
                            sha,
                            utcnow(),
                        ),
                    )
                    paths.append(path)
            except Exception as exc:  # noqa: BLE001 - record and continue
                record_failure(
                    conn,
                    {
                        "run_id": run_id,
                        "phase": "export",
                        "status": "open",
                        "reason": f"failed exporting {table_name} as {fmt}",
                        "exception_class": exc.__class__.__name__,
                        "exception_message": str(exc),
                    },
                )
    conn.commit()
    return paths


def dataframe_for_manifest(conn, table_name: str, run_id: str | None = None) -> pd.DataFrame:
    if table_name == "sources":
        return pd.read_sql_query(
            """
            SELECT source_id, source_name, ra_deg, dec_deg, cutout_size_arcsec,
                   active, row_hash, catalog_version_id
            FROM sources ORDER BY source_id
            """,
            conn,
        )
    if table_name == "discovery":
        return pd.read_sql_query(
            """
            SELECT
              m.source_id, s.source_name, s.ra_deg AS source_ra_deg, s.dec_deg AS source_dec_deg,
              p.collection, p.obs_collection, p.obs_id, p.observation_id, p.planning_period,
              p.detector_id, p.bandpass, p.energy_bandpassname, p.em_min, p.em_max,
              p.t_min, p.t_max, p.access_url, p.access_format, p.access_estsize,
              p.cloud_access_json, p.parent_filename, p.processing_version, p.processing_date,
              p.product_signature, p.last_seen_at AS discovered_at
            FROM source_product_matches m
            JOIN sources s ON s.source_id = m.source_id
            JOIN discovery_products p ON p.product_id = m.product_id
            WHERE m.active = 1
            ORDER BY m.source_id, p.product_id
            """,
            conn,
        )
    if table_name == "plan":
        params = (run_id,) if run_id else ()
        clause = "WHERE run_id = ?" if run_id else ""
        return pd.read_sql_query(f"SELECT * FROM download_plan {clause} ORDER BY plan_id", conn, params=params)
    if table_name == "cutouts":
        return pd.read_sql_query("SELECT * FROM cutouts ORDER BY source_id, cutout_id", conn)
    if table_name == "failures":
        return pd.read_sql_query("SELECT * FROM failures ORDER BY failure_id", conn)
    if table_name == "runs":
        return pd.read_sql_query("SELECT * FROM runs ORDER BY started_at", conn)
    if table_name == "coverage":
        return coverage_dataframe(conn)
    raise ValueError(f"unknown manifest table: {table_name}")


def export_table(conn, table_name: str, path: Path, format: str, run_id: str | None = None) -> Path:
    df = dataframe_for_manifest(conn, table_name, run_id)
    export_dataframe(df, path, format)
    return path


def export_dataframe(df: pd.DataFrame, path: Path, format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = format.lower()
    if fmt == "csv":
        df.to_csv(path, index=False)
    elif fmt == "parquet":
        df.to_parquet(path, index=False)
    elif fmt == "json":
        df.to_json(path, orient="records", indent=2)
    elif fmt == "ecsv":
        from astropy.table import Table

        Table.from_pandas(df).write(path, format="ascii.ecsv", overwrite=True)
    else:
        raise ValueError(f"unsupported manifest format: {format}")


def _manifest_filename(table_name: str, format: str) -> str:
    ext = {"parquet": "parquet", "csv": "csv", "json": "json", "ecsv": "ecsv"}[format]
    stem = "source_coverage" if table_name == "coverage" else table_name
    return f"{stem}.{ext}"


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, config: Config) -> str:
    try:
        return str(path.relative_to(config.project.root))
    except ValueError:
        return str(path)
