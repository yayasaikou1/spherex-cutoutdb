"""SQLite schema creation and data-access helpers."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from . import __version__
from .config import Config, config_hash


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    digest.update(canonical_json(value).encode("utf-8"))
    return digest.hexdigest()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    sql = importlib.resources.files("spherex_cutoutdb").joinpath("schema.sql").read_text()
    conn.executescript(sql)
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    return row_to_dict(conn.execute(sql, params).fetchone())


def query_df(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def table_count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def start_run(conn: sqlite3.Connection, command: str, args: dict[str, Any], config: Config) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = args.get("run_id") or f"run_{stamp}_{stable_hash({'command': command, 'args': args})[:8]}"
    snapshot_dir = config.project.root / "provenance" / "config_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{run_id}_config.yaml"
    snapshot = config.model_dump(mode="json")
    with snapshot_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(snapshot, handle, sort_keys=False)
    run_dir = config.project.root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "effective_config.yaml").open("w", encoding="utf-8") as handle:
        yaml.safe_dump(snapshot, handle, sort_keys=False)
    with (run_dir / "effective_config.json").open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=True)
    with (run_dir / "cli_overrides.json").open("w", encoding="utf-8") as handle:
        json.dump(args.get("_effective_cli_overrides") or {}, handle, indent=2, sort_keys=True, default=str)

    dep_versions = {
        "python": platform.python_version(),
        "pandas": pd.__version__,
    }
    conn.execute(
        """
        INSERT OR REPLACE INTO runs(
          run_id, command, args_json, config_hash, config_snapshot_path,
          package_version, python_version, dependency_versions_json, started_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            command,
            canonical_json(args),
            config_hash(config),
            str(snapshot_path.relative_to(config.project.root)),
            __version__,
            platform.python_version(),
            canonical_json(dep_versions),
            utcnow(),
            "running",
        ),
    )
    conn.commit()
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    counts: dict[str, Any] | None = None,
    summary_path: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs
        SET finished_at = ?, status = ?, counts_json = ?, summary_path = ?
        WHERE run_id = ?
        """,
        (utcnow(), status, canonical_json(counts or {}), summary_path, run_id),
    )
    conn.commit()


def record_event(
    conn: sqlite3.Connection,
    run_id: str | None,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    source_id: str | None = None,
    product_id: int | None = None,
    cutout_id: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO run_events(
          run_id, event_time, event_type, source_id, product_id, cutout_id, message, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            utcnow(),
            event_type,
            source_id,
            product_id,
            cutout_id,
            message,
            canonical_json(payload or {}),
        ),
    )
    conn.commit()


def insert_catalog_version(
    conn: sqlite3.Connection,
    catalog_version_id: str,
    path: str,
    normalized_path: str | None,
    catalog_hash: str,
    n_rows_input: int,
    n_rows_valid: int,
    n_rows_invalid: int,
    config_hash_value: str,
    run_id: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO source_catalog_versions(
          catalog_version_id, path, normalized_path, catalog_hash, n_rows_input,
          n_rows_valid, n_rows_invalid, ingested_at, config_hash, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_version_id,
            path,
            normalized_path,
            catalog_hash,
            n_rows_input,
            n_rows_valid,
            n_rows_invalid,
            utcnow(),
            config_hash_value,
            run_id,
        ),
    )
    conn.commit()


def upsert_sources(conn: sqlite3.Connection, catalog_version_id: str, sources_df: pd.DataFrame) -> dict[str, int]:
    now = utcnow()
    stats = {"new": 0, "updated": 0, "unchanged": 0, "retired": 0}
    active_ids = set(sources_df["source_id"].astype(str).tolist())
    existing_ids = {
        row["source_id"]
        for row in conn.execute("SELECT source_id FROM sources WHERE active = 1").fetchall()
    }
    for _, row in sources_df.iterrows():
        source_id = str(row["source_id"])
        existing = fetch_one(conn, "SELECT row_hash FROM sources WHERE source_id = ?", (source_id,))
        params = (
            source_id,
            row.get("source_name"),
            float(row["ra_deg"]),
            float(row["dec_deg"]),
            _maybe_float(row.get("cutout_size_arcsec")),
            row.get("source_type"),
            _maybe_int(row.get("priority")),
            1 if bool(row.get("active", True)) else 0,
            catalog_version_id,
            row["row_hash"],
            row.get("extra_json") or "{}",
            now,
            now,
            None,
        )
        conn.execute(
            """
            INSERT INTO sources(
              source_id, source_name, ra_deg, dec_deg, cutout_size_arcsec, source_type,
              priority, active, catalog_version_id, row_hash, extra_json,
              first_seen_at, last_seen_at, retired_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
              source_name=excluded.source_name,
              ra_deg=excluded.ra_deg,
              dec_deg=excluded.dec_deg,
              cutout_size_arcsec=excluded.cutout_size_arcsec,
              source_type=excluded.source_type,
              priority=excluded.priority,
              active=excluded.active,
              catalog_version_id=excluded.catalog_version_id,
              row_hash=excluded.row_hash,
              extra_json=excluded.extra_json,
              last_seen_at=excluded.last_seen_at,
              retired_at=NULL
            """,
            params,
        )
        if existing is None:
            stats["new"] += 1
        elif existing["row_hash"] == row["row_hash"]:
            stats["unchanged"] += 1
        else:
            stats["updated"] += 1

    retired = existing_ids - active_ids
    for source_id in retired:
        conn.execute(
            "UPDATE sources SET active = 0, retired_at = ?, last_seen_at = ? WHERE source_id = ?",
            (now, now, source_id),
        )
        stats["retired"] += 1
    conn.commit()
    return stats


def _maybe_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _maybe_int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def upsert_discovery_products(
    conn: sqlite3.Connection, run_id: str | None, rows_df: pd.DataFrame
) -> dict[str, int]:
    now = utcnow()
    mapping: dict[str, int] = {}
    if rows_df.empty:
        return mapping
    for _, row in rows_df.iterrows():
        product_signature = row["product_signature"]
        conn.execute(
            """
            INSERT INTO discovery_products(
              collection, obs_collection, obs_publisher_did, obs_id, observation_id,
              planning_period, detector_id, bandpass, energy_bandpassname, em_min, em_max,
              em_res_power, t_min, t_max, t_exptime, s_ra, s_dec, s_region, s_pixel_scale,
              dist_to_point, access_url, access_format, access_estsize, cloud_access_json,
              parent_filename, processing_version, processing_date, product_signature,
              row_hash, raw_sia_json, first_discovered_at, last_seen_at, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(product_signature) DO UPDATE SET
              collection=excluded.collection,
              obs_collection=excluded.obs_collection,
              obs_publisher_did=excluded.obs_publisher_did,
              obs_id=excluded.obs_id,
              observation_id=excluded.observation_id,
              planning_period=excluded.planning_period,
              detector_id=excluded.detector_id,
              bandpass=excluded.bandpass,
              energy_bandpassname=excluded.energy_bandpassname,
              em_min=excluded.em_min,
              em_max=excluded.em_max,
              em_res_power=excluded.em_res_power,
              t_min=excluded.t_min,
              t_max=excluded.t_max,
              t_exptime=excluded.t_exptime,
              s_ra=excluded.s_ra,
              s_dec=excluded.s_dec,
              s_region=excluded.s_region,
              s_pixel_scale=excluded.s_pixel_scale,
              dist_to_point=excluded.dist_to_point,
              access_url=excluded.access_url,
              access_format=excluded.access_format,
              access_estsize=excluded.access_estsize,
              cloud_access_json=excluded.cloud_access_json,
              parent_filename=excluded.parent_filename,
              processing_version=excluded.processing_version,
              processing_date=excluded.processing_date,
              row_hash=excluded.row_hash,
              raw_sia_json=excluded.raw_sia_json,
              last_seen_at=excluded.last_seen_at,
              last_run_id=excluded.last_run_id
            """,
            (
                row.get("collection"),
                row.get("obs_collection"),
                row.get("obs_publisher_did"),
                row.get("obs_id"),
                row.get("observation_id"),
                row.get("planning_period"),
                _maybe_int(row.get("detector_id")),
                row.get("bandpass"),
                row.get("energy_bandpassname"),
                _maybe_float(row.get("em_min")),
                _maybe_float(row.get("em_max")),
                _maybe_float(row.get("em_res_power")),
                _maybe_float(row.get("t_min")),
                _maybe_float(row.get("t_max")),
                _maybe_float(row.get("t_exptime")),
                _maybe_float(row.get("s_ra")),
                _maybe_float(row.get("s_dec")),
                row.get("s_region"),
                _maybe_float(row.get("s_pixel_scale")),
                _maybe_float(row.get("dist_to_point")),
                row.get("access_url"),
                row.get("access_format"),
                _maybe_int(row.get("access_estsize")),
                row.get("cloud_access_json") or "{}",
                row.get("parent_filename"),
                row.get("processing_version"),
                row.get("processing_date"),
                product_signature,
                row.get("row_hash"),
                row.get("raw_sia_json") or "{}",
                now,
                now,
                run_id,
            ),
        )
        product_id = conn.execute(
            "SELECT product_id FROM discovery_products WHERE product_signature = ?",
            (product_signature,),
        ).fetchone()["product_id"]
        mapping[product_signature] = int(product_id)
    conn.commit()
    return mapping


def upsert_source_product_matches(
    conn: sqlite3.Connection, run_id: str | None, matches_df: pd.DataFrame
) -> None:
    if matches_df.empty:
        return
    now = utcnow()
    for _, row in matches_df.iterrows():
        conn.execute(
            """
            INSERT INTO source_product_matches(
              source_id, product_id, collection, query_ra_deg, query_dec_deg, search_radius_deg,
              dist_to_point, coverage_status, match_hash, active, first_seen_at, last_seen_at, last_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, product_id, search_radius_deg) DO UPDATE SET
              collection=excluded.collection,
              query_ra_deg=excluded.query_ra_deg,
              query_dec_deg=excluded.query_dec_deg,
              dist_to_point=excluded.dist_to_point,
              coverage_status=excluded.coverage_status,
              match_hash=excluded.match_hash,
              active=excluded.active,
              last_seen_at=excluded.last_seen_at,
              last_run_id=excluded.last_run_id
            """,
            (
                row["source_id"],
                _maybe_int(row.get("product_id")),
                row["collection"],
                float(row["query_ra_deg"]),
                float(row["query_dec_deg"]),
                float(row["search_radius_deg"]),
                _maybe_float(row.get("dist_to_point")),
                row.get("coverage_status", "covered"),
                row["match_hash"],
                1,
                now,
                now,
                run_id,
            ),
        )
    conn.commit()


def deactivate_source_product_matches(
    conn: sqlite3.Connection,
    source_id: str,
    collections: list[str] | tuple[str, ...] | None = None,
) -> None:
    params: list[Any] = [source_id]
    sql = "UPDATE source_product_matches SET active = 0 WHERE source_id = ?"
    if collections:
        placeholders = ",".join("?" for _ in collections)
        sql += f" AND collection IN ({placeholders})"
        params.extend(collections)
    conn.execute(sql, tuple(params))
    conn.commit()


def insert_download_plan(conn: sqlite3.Connection, run_id: str | None, plan_df: pd.DataFrame) -> None:
    if plan_df.empty:
        return
    now = utcnow()
    for _, row in plan_df.iterrows():
        conn.execute(
            """
            INSERT INTO download_plan(
              run_id, source_id, product_id, match_id, cutout_key, cutout_ra_deg,
              cutout_dec_deg, cutout_size_arcsec, cutout_size_deg, cutout_url, local_path,
              access_method, action, reason, existing_cutout_id, priority, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["source_id"],
                _maybe_int(row.get("product_id")),
                _maybe_int(row.get("match_id")),
                row["cutout_key"],
                float(row["cutout_ra_deg"]),
                float(row["cutout_dec_deg"]),
                float(row["cutout_size_arcsec"]),
                float(row["cutout_size_deg"]),
                row.get("cutout_url"),
                row["local_path"],
                row["access_method"],
                row["action"],
                row.get("reason"),
                _maybe_int(row.get("existing_cutout_id")),
                _maybe_int(row.get("priority")),
                now,
            ),
        )
    conn.commit()


def upsert_cutout_record(conn: sqlite3.Connection, row: dict[str, Any], *, commit: bool = True) -> int:
    conn.execute(
        """
        INSERT INTO cutouts(
          cutout_key, source_id, product_id, local_path, file_exists, file_size_bytes, sha256,
          access_method, parent_access_url, cloud_access_json, cutout_url_used, parent_filename,
          collection, observation_id, detector_id, planning_period, processing_version,
          processing_date, bandpass, em_min, em_max, cutout_ra_deg, cutout_dec_deg,
          cutout_size_arcsec, download_started_at, download_completed_at, download_run_id,
          validation_status, validation_run_id, validation_time, failure_reason,
          hdu_summary_json, wcs_summary_json, psf_metadata_json, header_metadata_json, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cutout_key) DO UPDATE SET
          source_id=excluded.source_id,
          product_id=excluded.product_id,
          local_path=excluded.local_path,
          file_exists=excluded.file_exists,
          file_size_bytes=excluded.file_size_bytes,
          sha256=excluded.sha256,
          access_method=excluded.access_method,
          parent_access_url=excluded.parent_access_url,
          cloud_access_json=excluded.cloud_access_json,
          cutout_url_used=excluded.cutout_url_used,
          parent_filename=excluded.parent_filename,
          collection=excluded.collection,
          observation_id=excluded.observation_id,
          detector_id=excluded.detector_id,
          planning_period=excluded.planning_period,
          processing_version=excluded.processing_version,
          processing_date=excluded.processing_date,
          bandpass=excluded.bandpass,
          em_min=excluded.em_min,
          em_max=excluded.em_max,
          cutout_ra_deg=excluded.cutout_ra_deg,
          cutout_dec_deg=excluded.cutout_dec_deg,
          cutout_size_arcsec=excluded.cutout_size_arcsec,
          download_started_at=COALESCE(excluded.download_started_at, cutouts.download_started_at),
          download_completed_at=COALESCE(excluded.download_completed_at, cutouts.download_completed_at),
          download_run_id=COALESCE(excluded.download_run_id, cutouts.download_run_id),
          validation_status=COALESCE(excluded.validation_status, cutouts.validation_status),
          validation_run_id=COALESCE(excluded.validation_run_id, cutouts.validation_run_id),
          validation_time=COALESCE(excluded.validation_time, cutouts.validation_time),
          failure_reason=excluded.failure_reason,
          hdu_summary_json=COALESCE(excluded.hdu_summary_json, cutouts.hdu_summary_json),
          wcs_summary_json=COALESCE(excluded.wcs_summary_json, cutouts.wcs_summary_json),
          psf_metadata_json=COALESCE(excluded.psf_metadata_json, cutouts.psf_metadata_json),
          header_metadata_json=COALESCE(excluded.header_metadata_json, cutouts.header_metadata_json),
          active=excluded.active
        """,
        (
            row["cutout_key"],
            row["source_id"],
            _maybe_int(row.get("product_id")),
            row["local_path"],
            1 if row.get("file_exists") else 0,
            _maybe_int(row.get("file_size_bytes")),
            row.get("sha256"),
            row.get("access_method", "onprem_cutout"),
            row.get("parent_access_url"),
            row.get("cloud_access_json") or "{}",
            row.get("cutout_url_used"),
            row.get("parent_filename"),
            row.get("collection"),
            row.get("observation_id"),
            _maybe_int(row.get("detector_id")),
            row.get("planning_period"),
            row.get("processing_version"),
            row.get("processing_date"),
            row.get("bandpass"),
            _maybe_float(row.get("em_min")),
            _maybe_float(row.get("em_max")),
            _maybe_float(row.get("cutout_ra_deg")),
            _maybe_float(row.get("cutout_dec_deg")),
            _maybe_float(row.get("cutout_size_arcsec")),
            row.get("download_started_at"),
            row.get("download_completed_at"),
            row.get("download_run_id"),
            row.get("validation_status"),
            row.get("validation_run_id"),
            row.get("validation_time"),
            row.get("failure_reason"),
            row.get("hdu_summary_json"),
            row.get("wcs_summary_json"),
            row.get("psf_metadata_json"),
            row.get("header_metadata_json"),
            1 if row.get("active", True) else 0,
        ),
    )
    cutout_id = conn.execute(
        "SELECT cutout_id FROM cutouts WHERE cutout_key = ?", (row["cutout_key"],)
    ).fetchone()["cutout_id"]
    if commit:
        conn.commit()
    return int(cutout_id)


def record_validation(conn: sqlite3.Connection, row: dict[str, Any], *, commit: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO validation_results(
          run_id, cutout_id, local_path, status, reason, warnings_json, errors_json,
          file_size_bytes, sha256, required_hdus_present, image_shape, flags_shape,
          variance_shape, zodi_shape, psf_shape, wcwave_summary_json, spatial_wcs_valid,
          spectral_wcs_valid, validated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.get("run_id"),
            _maybe_int(row.get("cutout_id")),
            row["local_path"],
            row["status"],
            row.get("reason"),
            canonical_json(row.get("warnings") or []),
            canonical_json(row.get("errors") or []),
            _maybe_int(row.get("file_size_bytes")),
            row.get("sha256"),
            1 if row.get("required_hdus_present") else 0,
            canonical_json(row.get("image_shape")),
            canonical_json(row.get("flags_shape")),
            canonical_json(row.get("variance_shape")),
            canonical_json(row.get("zodi_shape")),
            canonical_json(row.get("psf_shape")),
            canonical_json(row.get("wcwave_summary") or {}),
            1 if row.get("spatial_wcs_valid") else 0,
            1 if row.get("spectral_wcs_valid") else 0,
            row.get("validated_at") or utcnow(),
        ),
    )
    if row.get("cutout_id"):
        conn.execute(
            """
            UPDATE cutouts
            SET validation_status = ?, validation_run_id = ?, validation_time = ?,
                failure_reason = ?, file_size_bytes = ?, sha256 = ?, file_exists = ?,
                hdu_summary_json = ?, wcs_summary_json = ?, psf_metadata_json = ?,
                header_metadata_json = ?
            WHERE cutout_id = ?
            """,
            (
                row["status"],
                row.get("run_id"),
                row.get("validated_at") or utcnow(),
                None if str(row["status"]).startswith("passed") else row.get("reason"),
                _maybe_int(row.get("file_size_bytes")),
                row.get("sha256"),
                1,
                canonical_json(row.get("hdu_summary") or {}),
                canonical_json(row.get("wcs_summary") or {}),
                canonical_json(row.get("psf_metadata") or {}),
                canonical_json(row.get("header_metadata") or {}),
                _maybe_int(row.get("cutout_id")),
            ),
        )
    if commit:
        conn.commit()


def record_failure(conn: sqlite3.Connection, row: dict[str, Any], *, commit: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO failures(
          run_id, source_id, product_id, plan_id, cutout_id, phase, status, reason,
          exception_class, exception_message, url, local_path, attempt, max_attempts,
          created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.get("run_id"),
            row.get("source_id"),
            _maybe_int(row.get("product_id")),
            _maybe_int(row.get("plan_id")),
            _maybe_int(row.get("cutout_id")),
            row["phase"],
            row.get("status", "open"),
            row["reason"],
            row.get("exception_class"),
            row.get("exception_message"),
            row.get("url"),
            row.get("local_path"),
            _maybe_int(row.get("attempt")),
            _maybe_int(row.get("max_attempts")),
            row.get("created_at") or utcnow(),
            row.get("resolved_at"),
        ),
    )
    if commit:
        conn.commit()


def get_latest_plan_rows(
    conn: sqlite3.Connection,
    run_id: str | None = None,
    actions: tuple[str, ...] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT * FROM download_plan"
    clauses = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if actions:
        placeholders = ",".join("?" for _ in actions)
        clauses.append(f"action IN ({placeholders})")
        params.extend(actions)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY plan_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row_to_dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
