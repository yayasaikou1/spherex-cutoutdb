"""Coverage and run summary helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.table import Table

from .config import Config


def coverage_dataframe(conn, active_only: bool = True) -> pd.DataFrame:
    source_clause = "WHERE s.active = 1" if active_only else ""
    sql = f"""
    SELECT
      s.source_id,
      s.source_name,
      s.ra_deg,
      s.dec_deg,
      COUNT(DISTINCT m.product_id) AS n_discovered_parent_mefs,
      COUNT(DISTINCT CASE WHEN c.validation_status IN ('passed','passed_with_warnings') THEN c.cutout_id END) AS n_valid_cutouts,
      COUNT(DISTINCT CASE WHEN c.validation_status LIKE 'failed%' THEN c.cutout_id END) AS n_failed_cutouts,
      COUNT(DISTINCT c.detector_id) AS n_detectors,
      GROUP_CONCAT(DISTINCT CASE WHEN c.detector_id IS NOT NULL THEN 'D' || c.detector_id END) AS detectors,
      COUNT(DISTINCT c.planning_period) AS n_planning_periods,
      COUNT(DISTINCT c.processing_version) AS n_processing_versions,
      MIN(c.em_min) AS em_min_min,
      MAX(c.em_max) AS em_max_max,
      MIN(p.t_min) AS first_t_min,
      MAX(p.t_max) AS last_t_max
    FROM sources s
    LEFT JOIN source_product_matches m ON m.source_id = s.source_id AND m.active = 1
    LEFT JOIN discovery_products p ON p.product_id = m.product_id
    LEFT JOIN cutouts c ON c.source_id = s.source_id AND c.active = 1
    {source_clause}
    GROUP BY s.source_id, s.source_name, s.ra_deg, s.dec_deg
    ORDER BY s.source_id
    """
    df = pd.read_sql_query(sql, conn)
    if df.empty:
        df["coverage_status"] = []
        return df
    df["coverage_status"] = df.apply(
        lambda row: "covered"
        if int(row["n_discovered_parent_mefs"] or 0) > 0
        else "not_covered",
        axis=1,
    )
    return df


def compute_summary_counts(conn, run_id: str | None = None) -> dict[str, Any]:
    counts = {
        "input_sources": _count(conn, "sources"),
        "valid_sources": _scalar(conn, "SELECT COUNT(*) FROM sources WHERE active = 1"),
        "sources_with_spherex_coverage": _scalar(
            conn,
            "SELECT COUNT(DISTINCT source_id) FROM source_product_matches WHERE active = 1 AND coverage_status = 'covered'",
        ),
        "discovered_parent_mefs": _count(conn, "discovery_products"),
        "cutouts_planned": _scalar(
            conn,
            "SELECT COUNT(*) FROM download_plan WHERE run_id IS ? OR ? IS NULL",
            (run_id, run_id),
        ),
        "already_present_and_validated": _scalar(
            conn,
            "SELECT COUNT(*) FROM download_plan WHERE action = 'skip_valid' AND (run_id IS ? OR ? IS NULL)",
            (run_id, run_id),
        ),
        "newly_downloaded": _scalar(
            conn,
            "SELECT COUNT(*) FROM cutouts WHERE download_run_id IS ? OR ? IS NULL",
            (run_id, run_id),
        ),
        "failed": _scalar(
            conn,
            "SELECT COUNT(*) FROM failures WHERE status IN ('open','retryable','nonretryable') AND (run_id IS ? OR ? IS NULL)",
            (run_id, run_id),
        ),
        "total_downloaded_volume": _scalar(
            conn,
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM cutouts WHERE download_run_id IS ? OR ? IS NULL",
            (run_id, run_id),
        ),
    }
    return counts


def write_summary_json(conn, run_id: str | None, config: Config) -> Path:
    counts = compute_summary_counts(conn, run_id)
    coverage = coverage_dataframe(conn).head(50).to_dict(orient="records")
    failures = pd.read_sql_query(
        "SELECT source_id, product_id, phase, reason FROM failures ORDER BY failure_id DESC LIMIT 50",
        conn,
    ).to_dict(orient="records")
    out = config.project.manifest_root / "runs" / f"{run_id or 'latest'}_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        json.dump({"run_id": run_id, "counts": counts, "coverage_preview": coverage, "failures": failures}, handle, indent=2)
    return out


def print_summary(conn, run_id: str | None, config: Config, console: Console | None = None) -> None:
    console = console or Console()
    counts = compute_summary_counts(conn, run_id)
    table = Table(title="SPHEREx cutout database run summary")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    for key, value in counts.items():
        table.add_row(key.replace("_", " "), str(value))
    console.print(table)

    coverage = coverage_dataframe(conn).head(10)
    if not coverage.empty:
        cov = Table(title="Per-source coverage preview")
        columns = [
            "source_id",
            "n_discovered_parent_mefs",
            "n_valid_cutouts",
            "n_failed_cutouts",
            "detectors",
            "coverage_status",
        ]
        for column in columns:
            cov.add_column(column)
        for _, row in coverage.iterrows():
            cov.add_row(*(str(row.get(column, "")) for column in columns))
        console.print(cov)


def _count(conn, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _scalar(conn, sql: str, params: tuple[Any, ...] = ()) -> int:
    value = conn.execute(sql, params).fetchone()[0]
    return int(value or 0)
