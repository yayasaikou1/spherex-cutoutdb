"""Validation for photometry calibration products."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from spherex_cutoutdb.validator import compute_sha256


@dataclass(slots=True)
class CalibrationValidation:
    path: Path
    product_type: str
    status: str
    reason: str
    file_size_bytes: int = 0
    sha256: str | None = None
    hdu_summary: dict[str, Any] = field(default_factory=dict)
    header_metadata: dict[str, Any] = field(default_factory=dict)


def validate_calibration_file(path: Path, product_type: str) -> CalibrationValidation:
    path = Path(path)
    if not path.exists() or not path.is_file():
        return CalibrationValidation(path, product_type, "failed", "file does not exist")
    file_size = path.stat().st_size
    if file_size <= 0:
        return CalibrationValidation(path, product_type, "failed", "file is empty")
    sha256 = compute_sha256(path)
    try:
        with fits.open(path, memmap=False, lazy_load_hdus=False) as hdul:
            hdu_summary = _hdu_summary(hdul)
            if product_type == "spectral_wcs":
                status, reason = _validate_spectral_wcs(hdul)
            elif product_type == "solid_angle_pixel_map":
                status, reason = _validate_solid_angle(hdul)
            else:
                status, reason = "failed", f"unsupported calibration product: {product_type}"
            return CalibrationValidation(
                path=path,
                product_type=product_type,
                status=status,
                reason=reason,
                file_size_bytes=file_size,
                sha256=sha256,
                hdu_summary=hdu_summary,
                header_metadata=_header_metadata(hdul),
            )
    except Exception as exc:  # noqa: BLE001 - validation result boundary
        return CalibrationValidation(
            path=path,
            product_type=product_type,
            status="failed",
            reason=str(exc),
            file_size_bytes=file_size,
            sha256=sha256,
        )


def _validate_spectral_wcs(hdul: fits.HDUList) -> tuple[str, str]:
    names = {hdu.name.upper(): hdu for hdu in hdul}
    missing = [name for name in ["CWAVE", "CBAND"] if name not in names]
    if missing:
        return "failed", f"missing required HDU(s): {', '.join(missing)}"
    cwave = np.asarray(names["CWAVE"].data, dtype=float)
    cband = np.asarray(names["CBAND"].data, dtype=float)
    if cwave.shape != cband.shape or cwave.ndim != 2:
        return "failed", "CWAVE and CBAND must be matching 2D arrays"
    if not np.isfinite(cwave).any() or not np.isfinite(cband).any():
        return "failed", "CWAVE/CBAND contain no finite values"
    if np.nanmedian(cwave) <= 0 or np.nanmedian(cband) <= 0:
        return "failed", "CWAVE/CBAND median values must be positive"
    return "valid", "validated"


def _validate_solid_angle(hdul: fits.HDUList) -> tuple[str, str]:
    data = None
    for hdu in hdul:
        candidate = getattr(hdu, "data", None)
        if candidate is not None and np.asarray(candidate).ndim == 2:
            data = np.asarray(candidate, dtype=float)
            break
    if data is None:
        return "failed", "no 2D solid-angle image found"
    finite = np.isfinite(data)
    if not finite.any():
        return "failed", "solid-angle map contains no finite values"
    if np.nanmedian(data[finite]) <= 0:
        return "failed", "solid-angle map median must be positive"
    return "valid", "validated"


def _hdu_summary(hdul: fits.HDUList) -> dict[str, Any]:
    rows = []
    for hdu in hdul:
        data = getattr(hdu, "data", None)
        rows.append(
            {
                "name": hdu.name,
                "type": hdu.__class__.__name__,
                "shape": list(data.shape) if data is not None and hasattr(data, "shape") else None,
                "dtype": str(data.dtype) if data is not None and hasattr(data, "dtype") else None,
            }
        )
    return {"hdu_count": len(rows), "hdu_names": [row["name"] for row in rows], "hdu_rows": rows}


def _header_metadata(hdul: fits.HDUList) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for hdu_name in ["PRIMARY", "CWAVE", "CBAND"]:
        try:
            hdu = hdul[0] if hdu_name == "PRIMARY" else hdul[hdu_name]
        except Exception:
            continue
        for key in ["DETECTOR", "DETID", "VERSION", "PROCVER", "PROCDATE", "DATE", "CALID"]:
            if key in hdu.header:
                metadata[f"{hdu_name.lower()}.{key}"] = str(hdu.header[key])
    return metadata
