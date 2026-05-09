"""Resolve required calibration products for a cutout."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spherex_cutoutdb.config import Config

from .registry import find_calibration


@dataclass(slots=True)
class CalibrationResolution:
    ok: bool
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    exact_match: bool = False
    detector_release_match: bool = False
    header_reference_match: bool = False
    match_quality: str = "missing"
    reason: str | None = None

    def path_for(self, config: Config, product_type: str) -> Path | None:
        row = self.products.get(product_type)
        if row is None:
            return None
        path = Path(row["relative_path"])
        return path if path.is_absolute() else config.project.root / path


def resolve_required_calibrations(conn, config: Config, cutout_row: dict[str, Any]) -> CalibrationResolution:
    products: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    detector_id = _maybe_int(cutout_row.get("detector_id"))
    header_references = _header_calibration_references(cutout_row)
    for product_type in config.calibration.required_products:
        # The Level-2 product processing date is not the calibration product
        # date. QR2 Spectral WCS and SAPM products are detector/release keyed
        # calibration assets with their own version/date identifiers.
        row = find_calibration(
            conn,
            config,
            product_type=product_type,
            detector_id=detector_id,
        )
        if row is None:
            missing.append(product_type)
            continue
        products[product_type] = row
    if missing:
        return CalibrationResolution(
            ok=False,
            products=products,
            missing=missing,
            exact_match=False,
            detector_release_match=False,
            header_reference_match=False,
            match_quality="missing",
            reason=f"missing required calibration product(s): {', '.join(missing)}",
        )
    detector_release_match = all(
        str(row.get("release") or "").upper() == str(config.calibration.release).upper()
        and (detector_id is None or row.get("detector_id") in {None, detector_id})
        for row in products.values()
    )
    header_reference_match = bool(header_references) and all(
        _row_matches_header_reference(row, header_references.get(product_type))
        for product_type, row in products.items()
    )
    exact_match = header_reference_match
    match_quality = (
        "exact_match"
        if exact_match
        else "header_reference_mismatch"
        if header_references
        else "detector_release_match"
        if detector_release_match
        else "detector_release_mismatch"
    )
    if not detector_release_match and not config.calibration.allow_latest_fallback:
        return CalibrationResolution(
            ok=False,
            products=products,
            missing=[],
            exact_match=False,
            detector_release_match=detector_release_match,
            header_reference_match=header_reference_match,
            match_quality=match_quality,
            reason="calibration detector/release mismatch",
        )
    return CalibrationResolution(
        ok=True,
        products=products,
        missing=[],
        exact_match=exact_match,
        detector_release_match=detector_release_match,
        header_reference_match=header_reference_match,
        match_quality=match_quality,
    )


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _header_calibration_references(cutout_row: dict[str, Any]) -> dict[str, str]:
    refs: dict[str, str] = {}
    aliases = {
        "spectral_wcs": [
            "spectral_wcs_calibration_id",
            "spectral_wcs_sha256",
            "spectral_wcs_filename",
            "WCS_CALID",
            "CWAVE_CALID",
        ],
        "solid_angle_pixel_map": [
            "solid_angle_calibration_id",
            "solid_angle_sha256",
            "solid_angle_filename",
            "SAPM_CALID",
            "SOLIDANG",
        ],
    }
    for product_type, keys in aliases.items():
        for key in keys:
            value = cutout_row.get(key)
            if value:
                refs[product_type] = str(value)
                break
    return refs


def _row_matches_header_reference(row: dict[str, Any], reference: str | None) -> bool:
    if not reference:
        return False
    ref = str(reference)
    fields = [
        row.get("calibration_id"),
        row.get("sha256"),
        row.get("filename"),
        row.get("relative_path"),
        row.get("source_url"),
    ]
    return any(ref == str(field) or ref in str(field) for field in fields if field)
