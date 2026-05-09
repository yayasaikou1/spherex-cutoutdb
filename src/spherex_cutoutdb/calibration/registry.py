"""SQLite-backed calibration registry."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from spherex_cutoutdb.config import Config
from spherex_cutoutdb.database import canonical_json, stable_hash, utcnow

from .validate import CalibrationValidation, validate_calibration_file


def infer_product_type(path: Path) -> str | None:
    text = "/".join(path.parts).lower()
    if "spectral_wcs" in text or "cwave" in text or "cband" in text:
        return "spectral_wcs"
    if "solid_angle" in text or "sapm" in text:
        return "solid_angle_pixel_map"
    return None


def infer_detector_id(path: Path) -> int | None:
    text = "/".join(path.parts)
    match = re.search(r"(?:^|[/_.-])D([1-6])(?:[/_.-]|$)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"det(?:ector)?[_-]?([1-6])", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def infer_calibration_version(path: Path) -> str | None:
    text = "/".join(path.parts)
    match = re.search(r"(cal-[A-Za-z0-9_.-]+-v[A-Za-z0-9_.-]+-\d{4}[-_]?\d{3})", text)
    if match:
        return match.group(1).replace("_", "-")
    match = re.search(r"(v\d+(?:[_.-]\d+)*)", path.stem, re.IGNORECASE)
    return match.group(1) if match else None


def infer_processing_date(path: Path) -> str | None:
    text = "/".join(path.parts)
    match = re.search(r"(\d{4}[-_]\d{3}|\d{8})", text)
    return match.group(1).replace("_", "-") if match else None


def calibration_id_for(
    *,
    release: str,
    product_type: str,
    detector_id: int | None,
    calibration_version: str | None,
    processing_date: str | None,
    sha256: str | None,
    relative_path: str,
) -> str:
    return stable_hash(
        {
            "release": release.lower(),
            "product_type": product_type,
            "detector_id": detector_id,
            "calibration_version": calibration_version,
            "processing_date": processing_date,
            "sha256": sha256,
            "relative_path": relative_path,
        }
    )


def register_calibration_file(
    conn,
    config: Config,
    path: Path,
    *,
    product_type: str | None = None,
    detector_id: int | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    path = Path(path)
    product = product_type or infer_product_type(path)
    if product is None:
        raise ValueError(f"could not infer calibration product type from {path}")
    validation = validate_calibration_file(path, product)
    detector = detector_id if detector_id is not None else infer_detector_id(path)
    version = infer_calibration_version(path)
    proc_date = infer_processing_date(path)
    rel_path = _rel_or_str(path, config)
    calibration_id = calibration_id_for(
        release=config.calibration.release,
        product_type=product,
        detector_id=detector,
        calibration_version=version,
        processing_date=proc_date,
        sha256=validation.sha256,
        relative_path=rel_path,
    )
    now = utcnow()
    conn.execute(
        """
        INSERT INTO calibration_products(
          calibration_id, release, product_type, detector_id, calibration_version,
          processing_date, filename, relative_path, source_url, file_size_bytes, sha256,
          validation_status, validation_reason, hdu_summary_json, header_metadata_json,
          first_seen_at, last_validated_at, active
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(calibration_id) DO UPDATE SET
          detector_id=excluded.detector_id,
          calibration_version=excluded.calibration_version,
          processing_date=excluded.processing_date,
          relative_path=excluded.relative_path,
          source_url=COALESCE(excluded.source_url, calibration_products.source_url),
          file_size_bytes=excluded.file_size_bytes,
          sha256=excluded.sha256,
          validation_status=excluded.validation_status,
          validation_reason=excluded.validation_reason,
          hdu_summary_json=excluded.hdu_summary_json,
          header_metadata_json=excluded.header_metadata_json,
          last_validated_at=excluded.last_validated_at,
          active=1
        """,
        (
            calibration_id,
            config.calibration.release,
            product,
            detector,
            version,
            proc_date,
            path.name,
            rel_path,
            source_url,
            validation.file_size_bytes,
            validation.sha256,
            validation.status,
            validation.reason,
            canonical_json(validation.hdu_summary),
            canonical_json(validation.header_metadata),
            now,
            now,
            1,
        ),
    )
    conn.commit()
    return {
        "calibration_id": calibration_id,
        "release": config.calibration.release,
        "product_type": product,
        "detector_id": detector,
        "calibration_version": version,
        "processing_date": proc_date,
        "relative_path": rel_path,
        "source_url": source_url,
        "validation": validation,
    }


def find_calibration(
    conn,
    config: Config,
    *,
    product_type: str,
    detector_id: int | None,
    processing_date: str | None = None,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT *
        FROM calibration_products
        WHERE release = ?
          AND product_type = ?
          AND (? IS NULL OR detector_id = ?)
          AND validation_status = 'valid'
          AND active = 1
        ORDER BY
          CASE WHEN processing_date = ? THEN 0 ELSE 1 END,
          last_validated_at DESC,
          calibration_id
        """,
        (
            config.calibration.release,
            product_type,
            detector_id,
            detector_id,
            processing_date,
        ),
    ).fetchall()
    if not rows:
        return None
    return {key: rows[0][key] for key in rows[0].keys()}


def cached_calibration_files(config: Config) -> list[Path]:
    root = config.calibration.cache_root
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.fits") if path.is_file())


def _rel_or_str(path: Path, config: Config) -> str:
    try:
        return str(path.resolve().relative_to(config.project.root))
    except ValueError:
        return str(path)
