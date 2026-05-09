"""FITS/HDU/WCS/PSF validation without mutating downloaded files."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import math
import warnings
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS

from .config import Config
from .models import ValidationResult

ASTROPY_WCS_LOGGERS = ["astropy", "astropy.wcs", "astropy.wcs.wcs"]


def compute_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_cutout(
    path: Path,
    config: Config,
    expected: dict[str, Any] | None = None,
    *,
    precomputed_sha256: str | None = None,
    precomputed_sha256_file_size: int | None = None,
) -> ValidationResult:
    path = Path(path)
    if not path.exists() or not path.is_file():
        return ValidationResult(path=path, status="failed_invalid_fits", reason="file does not exist")
    file_size = path.stat().st_size
    if file_size <= 0:
        return ValidationResult(path=path, status="failed_invalid_fits", reason="file is empty")
    if (
        config.validation.compute_sha256
        and precomputed_sha256
        and precomputed_sha256_file_size == file_size
    ):
        sha = precomputed_sha256
    else:
        sha = compute_sha256(path) if config.validation.compute_sha256 else None

    try:
        with _suppress_astropy_wcs_info_logs(), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with fits.open(path, memmap=False, lazy_load_hdus=False) as hdul:
                if config.validation.check_fits_verify:
                    hdul.verify(option="warn")
                result = _validate_hdul(path, hdul, config, expected)
                result.file_size_bytes = file_size
                result.sha256 = sha
                result.warnings.extend(str(item.message) for item in caught)
                if result.status == "passed" and result.warnings:
                    result.status = "passed_with_warnings"
                return result
    except Exception as exc:  # noqa: BLE001 - returned as validation result
        return ValidationResult(
            path=path,
            status="failed_invalid_fits",
            reason=str(exc),
            errors=[str(exc)],
            file_size_bytes=file_size,
            sha256=sha,
        )


@contextmanager
def _suppress_astropy_wcs_info_logs():
    """Keep Astropy WCS INFO diagnostics from corrupting progress output."""

    previous = []
    for name in ASTROPY_WCS_LOGGERS:
        logger = logging.getLogger(name)
        previous.append((logger, logger.level))
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in previous:
            logger.setLevel(level)


def _validate_hdul(
    path: Path,
    hdul: fits.HDUList,
    config: Config,
    expected: dict[str, Any] | None = None,
) -> ValidationResult:
    hdu_summary = _hdu_summary(hdul)
    names = [name.upper() for name in hdu_summary["hdu_names"]]
    duplicate_names = sorted({name for name in names if name and names.count(name) > 1})
    if duplicate_names:
        return ValidationResult(
            path=path,
            status="failed_missing_hdu",
            reason=f"duplicate HDU names: {', '.join(duplicate_names)}",
            hdu_summary=hdu_summary,
            errors=[f"duplicate HDU names: {duplicate_names}"],
        )

    required = [name.upper() for name in config.validation.require_hdus]
    missing = [name for name in required if name not in names]
    if missing:
        status = "failed_psf_hdu" if missing == ["PSF"] else "failed_missing_hdu"
        if "WCS-WAVE" in missing and len(missing) == 1:
            status = "failed_invalid_spectral_wcs"
        return ValidationResult(
            path=path,
            status=status,
            reason=f"missing required HDU(s): {', '.join(missing)}",
            hdu_summary=hdu_summary,
            errors=[f"missing required HDU(s): {missing}"],
        )

    image_hdu = hdul["IMAGE"]
    plane_result = _check_image_planes(hdul)
    if plane_result["errors"]:
        return ValidationResult(
            path=path,
            status="failed_invalid_fits",
            reason="image-plane shape or dtype check failed",
            errors=plane_result["errors"],
            hdu_summary=hdu_summary,
            image_shape=plane_result["shapes"].get("IMAGE"),
            flags_shape=plane_result["shapes"].get("FLAGS"),
            variance_shape=plane_result["shapes"].get("VARIANCE"),
            zodi_shape=plane_result["shapes"].get("ZODI"),
        )

    spatial = check_spatial_wcs(image_hdu.header, image_hdu.data)
    if config.validation.require_spatial_wcs and not spatial["spatial_wcs_valid"]:
        return ValidationResult(
            path=path,
            status="failed_invalid_wcs",
            reason=spatial.get("reason", "spatial WCS invalid"),
            errors=[spatial.get("reason", "spatial WCS invalid")],
            hdu_summary=hdu_summary,
            wcs_summary=spatial,
            image_shape=plane_result["shapes"].get("IMAGE"),
            flags_shape=plane_result["shapes"].get("FLAGS"),
            variance_shape=plane_result["shapes"].get("VARIANCE"),
            zodi_shape=plane_result["shapes"].get("ZODI"),
        )

    spectral = check_spectral_wcs(hdul)
    warnings_list: list[str] = []
    if not spectral["spectral_wcs_valid"]:
        if config.validation.require_spectral_wcs_or_wcwave and not spectral.get("wcwave_summary"):
            return ValidationResult(
                path=path,
                status="failed_invalid_spectral_wcs",
                reason=spectral.get("reason", "spectral WCS/WCS-WAVE invalid"),
                errors=[spectral.get("reason", "spectral WCS/WCS-WAVE invalid")],
                hdu_summary=hdu_summary,
                wcs_summary={**spatial, **spectral},
                wcwave_summary=spectral.get("wcwave_summary", {}),
            )
        warnings_list.append(spectral.get("reason", "spectral WCS could not be instantiated"))

    psf_metadata = extract_psf_metadata(hdul)
    if not psf_metadata.get("psf_hdu_present"):
        return ValidationResult(
            path=path,
            status="failed_psf_hdu",
            reason="PSF HDU is absent or unreadable",
            hdu_summary=hdu_summary,
            wcs_summary={**spatial, **spectral},
            errors=["PSF HDU is absent or unreadable"],
        )
    if psf_metadata.get("known_psf_header_issue_status") == "warn":
        warnings_list.append("known PSF header issue indicators present; PSF HDU preserved unmodified")

    header_metadata = extract_header_metadata(hdul, config)
    wcs_summary = {**spatial, **spectral}
    return ValidationResult(
        path=path,
        status="passed_with_warnings" if warnings_list else "passed",
        reason="validated",
        warnings=warnings_list,
        file_size_bytes=path.stat().st_size,
        required_hdus_present=True,
        image_shape=plane_result["shapes"].get("IMAGE"),
        flags_shape=plane_result["shapes"].get("FLAGS"),
        variance_shape=plane_result["shapes"].get("VARIANCE"),
        zodi_shape=plane_result["shapes"].get("ZODI"),
        psf_shape=psf_metadata.get("psf_shape"),
        hdu_summary=hdu_summary,
        wcs_summary=wcs_summary,
        wcwave_summary=spectral.get("wcwave_summary", {}),
        psf_metadata=psf_metadata,
        header_metadata=header_metadata,
    )


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
    return {
        "hdu_count": len(hdul),
        "hdu_names": [row["name"] for row in rows],
        "hdu_types": [row["type"] for row in rows],
        "hdu_shapes": {row["name"]: row["shape"] for row in rows},
        "hdu_dtypes": {row["name"]: row["dtype"] for row in rows},
    }


def _check_image_planes(hdul: fits.HDUList) -> dict[str, Any]:
    errors: list[str] = []
    shapes: dict[str, list[int]] = {}
    reference_shape = None
    names = [hdu.name.upper() for hdu in hdul]
    for name in ["IMAGE", "FLAGS", "VARIANCE"]:
        data = hdul[name].data
        if data is None:
            errors.append(f"{name} data is missing")
            continue
        if data.ndim != 2:
            errors.append(f"{name} data must be 2-dimensional")
            continue
        if not np.issubdtype(data.dtype, np.number):
            errors.append(f"{name} data must be numeric")
        shape = list(data.shape)
        shapes[name] = shape
        if reference_shape is None:
            reference_shape = shape
        elif shape != reference_shape:
            errors.append(f"{name} shape {shape} does not match IMAGE shape {reference_shape}")
    if "ZODI" in names:
        data = hdul["ZODI"].data
        if data is None:
            errors.append("ZODI data is missing")
        elif data.ndim != 2:
            errors.append("ZODI data must be 2-dimensional")
        else:
            if not np.issubdtype(data.dtype, np.number):
                errors.append("ZODI data must be numeric")
            shape = list(data.shape)
            shapes["ZODI"] = shape
            if reference_shape is not None and shape != reference_shape:
                errors.append(f"ZODI shape {shape} does not match IMAGE shape {reference_shape}")
    return {"errors": errors, "shapes": shapes}


def check_spatial_wcs(image_header: fits.Header, image_data: Any) -> dict[str, Any]:
    try:
        wcs = WCS(image_header)
        if not wcs.has_celestial:
            return {"spatial_wcs_valid": False, "reason": "IMAGE WCS has no celestial component"}
        shape = image_data.shape if image_data is not None else (1, 1)
        x = (shape[-1] - 1) / 2.0
        y = (shape[-2] - 1) / 2.0
        world = wcs.pixel_to_world_values(x, y)
        ra = float(world[0])
        dec = float(world[1])
        if not (math.isfinite(ra) and math.isfinite(dec)):
            return {"spatial_wcs_valid": False, "reason": "IMAGE WCS produced non-finite coordinates"}
        return {
            "spatial_wcs_valid": True,
            "ctype1": image_header.get("CTYPE1"),
            "ctype2": image_header.get("CTYPE2"),
            "crval1": image_header.get("CRVAL1"),
            "crval2": image_header.get("CRVAL2"),
            "crpix1": image_header.get("CRPIX1"),
            "crpix2": image_header.get("CRPIX2"),
            "cd_or_pc_keywords_present": any(
                key.startswith(("CD", "PC")) for key in image_header.keys()
            ),
        }
    except Exception as exc:  # noqa: BLE001 - summary result
        return {"spatial_wcs_valid": False, "reason": str(exc)}


def check_spectral_wcs(hdul: fits.HDUList) -> dict[str, Any]:
    wcwave_summary = _wcwave_summary(hdul["WCS-WAVE"])
    if not wcwave_summary:
        return {
            "spectral_wcs_valid": False,
            "reason": "WCS-WAVE HDU has no readable structure",
            "wcwave_summary": {},
        }
    try:
        WCS(hdul["IMAGE"].header, fobj=hdul, key="W")
        return {
            "spectral_wcs_valid": True,
            "spectral_wcs_instantiated": True,
            "wcwave_summary": wcwave_summary,
        }
    except Exception as exc:  # noqa: BLE001 - warning if WCS-WAVE exists
        return {
            "spectral_wcs_valid": False,
            "spectral_wcs_instantiated": False,
            "reason": str(exc),
            "wcwave_summary": wcwave_summary,
        }


def _wcwave_summary(hdu: fits.hdu.base.ExtensionHDU) -> dict[str, Any]:
    data = hdu.data
    if data is None:
        return {}
    columns = []
    if hasattr(hdu, "columns"):
        columns = list(hdu.columns.names)
    return {
        "wcwave_hdu_type": hdu.__class__.__name__,
        "wcwave_column_names": columns,
        "wcwave_n_rows": len(data) if hasattr(data, "__len__") else None,
        "wcwave_array_shapes": {
            name: list(np.asarray(data[name]).shape) for name in columns
        }
        if columns
        else {"data": list(np.asarray(data).shape)},
    }


def extract_psf_metadata(hdul: fits.HDUList) -> dict[str, Any]:
    try:
        hdu = hdul["PSF"]
    except Exception:
        return {"psf_hdu_present": False}
    data = hdu.data
    header_text = hdu.header.tostring(sep="\n", endcard=True, padding=False)
    version_keys = {
        key: hdu.header[key]
        for key in ["VERSION", "PROCVER", "PROCDATE", "PSFVER"]
        if key in hdu.header
    }
    xctr_count = sum(1 for key in hdu.header.keys() if key.upper().startswith("XCTR"))
    yctr_count = sum(1 for key in hdu.header.keys() if key.upper().startswith("YCTR"))
    issue = "warn" if hdu.header.get("PSFHDRER") or hdu.header.get("PSFHDRERR") else "none"
    return {
        "psf_hdu_present": True,
        "psf_shape": list(data.shape) if data is not None else None,
        "psf_dtype": str(data.dtype) if data is not None else None,
        "psf_header_hash": hashlib.sha256(header_text.encode("utf-8")).hexdigest(),
        "xctr_keyword_count": xctr_count,
        "yctr_keyword_count": yctr_count,
        "version_keyword_values": version_keys,
        "known_psf_header_issue_status": issue,
    }


def extract_header_metadata(hdul: fits.HDUList, config: Config) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for hdu_name in ["PRIMARY", "IMAGE", "PSF"]:
        hdu = hdul[0] if hdu_name == "PRIMARY" else hdul[hdu_name]
        prefix = hdu_name.lower()
        for key in config.validation.record_header_cards:
            if key in hdu.header:
                value = hdu.header[key]
                try:
                    json.dumps(value)
                except TypeError:
                    value = str(value)
                metadata[f"{prefix}.{key}"] = value
    return metadata
