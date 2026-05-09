"""Download planning from discovered source-product matches."""

from __future__ import annotations

import pandas as pd

from .cloud_access import select_access_method
from .config import Config
from .database import insert_download_plan, stable_hash
from .filenames import deterministic_cutout_path
from .irsa_cutouts import arcsec_to_url_size_deg, build_cutout_url


VALIDATION_OK = {"passed", "passed_with_warnings"}


def compute_cutout_key(
    source_id: str,
    product_signature: str,
    ra: float,
    dec: float,
    size_arcsec: float,
    access_method: str,
) -> str:
    payload = {
        "source_id": source_id,
        "product_signature": product_signature,
        "cutout_ra_deg": round(float(ra), 8),
        "cutout_dec_deg": round(float(dec), 8),
        "cutout_size_arcsec": round(float(size_arcsec), 4),
        "access_method": access_method,
    }
    return stable_hash(payload)


def choose_local_path(row: dict, config: Config, cutout_key: str, size_arcsec: float):
    return deterministic_cutout_path(
        config.project.data_root,
        row["source_id"],
        row["collection"],
        row.get("planning_period"),
        row.get("processing_version"),
        row.get("detector_id"),
        row.get("parent_filename"),
        size_arcsec,
        cutout_key,
    )


def plan_downloads(
    conn,
    run_id: str | None,
    config: Config,
    source_ids: list[str] | None = None,
) -> pd.DataFrame:
    records = make_download_plan_records(conn, run_id, config, source_ids)
    plan_df = pd.DataFrame(records)
    insert_download_plan(conn, run_id, plan_df)
    return plan_df


def make_download_plan_records(
    conn,
    run_id: str | None,
    config: Config,
    source_ids: list[str] | None = None,
) -> list[dict]:
    """Build downloader-compatible plan records without inserting them."""

    rows = _candidate_rows(conn, source_ids)
    records: list[dict] = []
    seen_keys: set[str] = set()
    for raw in rows:
        row = dict(raw)
        size_arcsec = _effective_size(row, config)
        access_decision = select_access_method(row, "cutout", config)
        cutout_size_deg = arcsec_to_url_size_deg(size_arcsec)
        cutout_url = None
        action = "download"
        reason = "no existing valid cutout"
        existing_cutout_id = None
        product_signature = row.get("product_signature") or ""
        cutout_key = compute_cutout_key(
            row["source_id"],
            product_signature,
            row["ra_deg"],
            row["dec_deg"],
            size_arcsec,
            access_decision.access_method,
        )
        local_path = choose_local_path(row, config, cutout_key, size_arcsec)
        rel_path = _relative_path(local_path, config)

        if cutout_key in seen_keys:
            action = "skip_duplicate"
            reason = "duplicate desired cutout in current plan"
        elif not access_decision.allowed:
            action = (
                "fail_no_official_cloud_cutout"
                if access_decision.access_method == "cloud_cutout"
                else "fail_no_access_url"
            )
            reason = access_decision.reason
        else:
            if access_decision.access_method == "onprem_cutout":
                cutout_url = build_cutout_url(row["access_url"], row["ra_deg"], row["dec_deg"], cutout_size_deg)
            existing = conn.execute(
                "SELECT * FROM cutouts WHERE cutout_key = ?", (cutout_key,)
            ).fetchone()
            final_exists = local_path.exists()
            if existing:
                existing_cutout_id = existing["cutout_id"]
                if (
                    config.planning.skip_existing_valid
                    and final_exists
                    and existing["validation_status"] in VALIDATION_OK
                ):
                    action = "skip_valid"
                    reason = "existing file validates"
                elif final_exists and existing["validation_status"] in (None, "", "stale"):
                    action = "validate_existing"
                    reason = "existing file needs validation"
                elif final_exists and str(existing["validation_status"] or "").startswith("failed"):
                    if config.planning.redownload_invalid:
                        action = "redownload_invalid"
                        reason = "existing file failed validation"
                    else:
                        action = "validate_existing"
                        reason = "existing invalid file retained by config"
                elif final_exists:
                    action = "validate_existing"
                    reason = "existing file requires metadata refresh"
                else:
                    action = "download"
                    reason = "existing DB record file is missing"
            elif final_exists:
                action = "validate_existing"
                reason = "file exists without cutout DB record"

        seen_keys.add(cutout_key)
        records.append(
            {
                "run_id": run_id,
                "source_id": row["source_id"],
                "product_id": row.get("product_id"),
                "match_id": row.get("match_id"),
                "cutout_key": cutout_key,
                "cutout_ra_deg": float(row["ra_deg"]),
                "cutout_dec_deg": float(row["dec_deg"]),
                "cutout_size_arcsec": size_arcsec,
                "cutout_size_deg": cutout_size_deg,
                "cutout_url": cutout_url,
                "local_path": rel_path,
                "access_method": access_decision.access_method,
                "action": action,
                "reason": reason,
                "existing_cutout_id": existing_cutout_id,
                "priority": row.get("priority"),
                "parent_access_url": row.get("access_url"),
                "cloud_access_json": row.get("cloud_access_json"),
                "parent_filename": row.get("parent_filename"),
                "collection": row.get("collection"),
                "observation_id": row.get("observation_id"),
                "detector_id": row.get("detector_id"),
                "planning_period": row.get("planning_period"),
                "processing_version": row.get("processing_version"),
                "processing_date": row.get("processing_date"),
                "bandpass": row.get("bandpass"),
                "em_min": row.get("em_min"),
                "em_max": row.get("em_max"),
            }
        )
    return records


def _candidate_rows(conn, source_ids: list[str] | None = None):
    params: list[str] = []
    clause = "WHERE s.active = 1 AND m.active = 1 AND m.coverage_status = 'covered'"
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        clause += f" AND s.source_id IN ({placeholders})"
        params.extend(source_ids)
    sql = f"""
    SELECT
      m.match_id, s.source_id, s.source_name, s.ra_deg, s.dec_deg, s.cutout_size_arcsec,
      s.priority, p.*
    FROM source_product_matches m
    JOIN sources s ON s.source_id = m.source_id
    JOIN discovery_products p ON p.product_id = m.product_id
    {clause}
    ORDER BY COALESCE(s.priority, 999999), s.source_id, p.collection, p.product_id
    """
    return conn.execute(sql, tuple(params)).fetchall()


def _effective_size(row: dict, config: Config) -> float:
    size = row.get("cutout_size_arcsec")
    if size is None or pd.isna(size):
        size = config.cutouts.default_size_arcsec
    size = float(size)
    if not (config.cutouts.min_size_arcsec <= size <= config.cutouts.max_size_arcsec):
        raise ValueError(f"cutout size outside configured limits for {row['source_id']}: {size}")
    return size


def _relative_path(path, config: Config) -> str:
    try:
        return str(path.relative_to(config.project.root))
    except ValueError:
        return str(path)
