"""Photometry work-item planning."""

from __future__ import annotations

from collections import Counter
from typing import Any

from spherex_cutoutdb.calibration import resolve_required_calibrations
from spherex_cutoutdb.config import Config
from spherex_cutoutdb.planner import make_download_plan_records

from .result_store import (
    VALIDATION_OK,
    latest_cutout_for_key,
    measurement_id_for,
    photometry_config_hash,
    upsert_work_item,
    valid_measurement_exists,
)


def build_photometry_plan(
    conn,
    config: Config,
    *,
    photometry_run_id: str | None = None,
    source_ids: list[str] | None = None,
    force_remeasure: bool = False,
    commit: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Classify work items.

    ``commit=False`` batches work-item upserts into one commit for source-level
    photometry runs; the rows are still committed before this function returns.
    """

    plan_records = make_download_plan_records(conn, None, config, source_ids)
    items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    source_cache: dict[str, dict[str, Any]] = {}
    calibration_cache: dict[int | None, Any] = {}
    for record in plan_records:
        source = source_cache.get(record["source_id"])
        if source is None:
            row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (record["source_id"],)).fetchone()
            source = {key: row[key] for key in row.keys()}
            source_cache[record["source_id"]] = source
        cutout = latest_cutout_for_key(conn, record["cutout_key"])
        calibration_row = cutout or record
        detector_key = _maybe_int(calibration_row.get("detector_id"))
        calibration = calibration_cache.get(detector_key)
        if calibration is None:
            calibration = resolve_required_calibrations(conn, config, calibration_row)
            calibration_cache[detector_key] = calibration
        state, reason = _classify(conn, config, source, record, cutout, calibration, force_remeasure=force_remeasure)
        item = upsert_work_item(
            conn,
            photometry_run_id=photometry_run_id,
            source=source,
            plan_row=record,
            cutout=cutout,
            calibration_resolution=calibration,
            state=state,
            reason=reason,
            config=config,
            commit=commit,
        )
        items.append(item)
        counts[state] += 1
    if not commit:
        conn.commit()
    return items, dict(counts)


def _classify(
    conn,
    config: Config,
    source: dict[str, Any],
    plan_row: dict[str, Any],
    cutout: dict[str, Any] | None,
    calibration_resolution,
    force_remeasure: bool = False,
) -> tuple[str, str | None]:
    if not calibration_resolution.ok:
        return "calibration_missing", calibration_resolution.reason
    if cutout and cutout.get("validation_status") in VALIDATION_OK:
        measurement_id = measurement_id_for(
            source_id=source["source_id"],
            cutout_key=plan_row["cutout_key"],
            cutout_sha256=cutout.get("sha256"),
            spectral_wcs_calibration_id=calibration_resolution.products["spectral_wcs"]["calibration_id"],
            solid_angle_calibration_id=calibration_resolution.products["solid_angle_pixel_map"]["calibration_id"],
            config=config,
        )
        if valid_measurement_exists(conn, measurement_id) and not force_remeasure:
            return "photometry_valid", "matching photometry already exists"
        if not _cutout_file_exists(config, cutout):
            if force_remeasure and valid_measurement_exists(conn, measurement_id):
                return "cutout_missing_or_invalid", "force rerun requested but validated cutout file is missing"
            return "cutout_missing_or_invalid", "validated cutout record exists but file is missing"
        if force_remeasure and valid_measurement_exists(conn, measurement_id):
            return "cutout_valid_measurement_missing", "force rerun requested; matching photometry will be replaced"
        return "cutout_valid_measurement_missing", "valid cutout exists but photometry is missing or stale"
    return "cutout_missing_or_invalid", "cutout is missing or not valid"


def _cutout_file_exists(config: Config, cutout: dict[str, Any]) -> bool:
    path = cutout.get("local_path")
    if not path:
        return False
    full = config.project.root / path
    return full.exists()


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
