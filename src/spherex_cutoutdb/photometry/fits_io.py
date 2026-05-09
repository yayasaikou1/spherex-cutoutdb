"""FITS loading for SPHEREx photometry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


@dataclass(slots=True)
class CutoutData:
    path: Path
    image_mjy_sr: np.ndarray
    variance_mjy_sr2: np.ndarray
    flags: np.ndarray
    psf: np.ndarray
    image_header: fits.Header
    primary_header: fits.Header
    psf_header: fits.Header
    spatial_wcs: WCS
    spatial_wcs_warnings: list[str]
    header_metadata: dict[str, Any]


def load_cutout(path: Path) -> CutoutData:
    path = Path(path)
    with fits.open(path, memmap=False, lazy_load_hdus=False) as hdul:
        image_hdu = hdul["IMAGE"]
        psf_hdu = hdul["PSF"]
        image = np.asarray(image_hdu.data, dtype=float)
        variance = np.asarray(hdul["VARIANCE"].data, dtype=float)
        flags = np.asarray(hdul["FLAGS"].data)
        psf = np.asarray(psf_hdu.data, dtype=float)
        if image.ndim != 2 or variance.shape != image.shape or flags.shape != image.shape:
            raise ValueError("IMAGE, VARIANCE, and FLAGS must be matching 2D arrays")
        spatial_header, wcs_warnings = build_spatial_header(image_hdu.header)
        return CutoutData(
            path=path,
            image_mjy_sr=image,
            variance_mjy_sr2=variance,
            flags=flags,
            psf=psf,
            image_header=image_hdu.header.copy(),
            primary_header=hdul[0].header.copy(),
            psf_header=psf_hdu.header.copy(),
            spatial_wcs=WCS(spatial_header, naxis=2),
            spatial_wcs_warnings=wcs_warnings,
            header_metadata=_header_metadata(hdul),
        )


def build_spatial_header(header: fits.Header) -> tuple[fits.Header, list[str]]:
    clean = fits.Header()
    dropped: list[str] = []
    for key, value in header.items():
        upper = key.upper()
        if _is_alternate_wcs_card(upper):
            dropped.append(key)
            continue
        if _is_primary_spatial_wcs_card(upper) or _is_sip_card(upper):
            clean[key] = value
    if "NAXIS" in clean:
        clean["NAXIS"] = 2
    clean["WCSAXES"] = 2
    warnings = []
    if dropped:
        warnings.append(f"dropped alternate/non-spatial WCS cards: {','.join(sorted(dropped)[:12])}")
    return clean, warnings


def _is_alternate_wcs_card(key: str) -> bool:
    if len(key) < 2:
        return False
    suffix = key[-1]
    if suffix not in {"A", "W"}:
        return False
    stem = key[:-1]
    prefixes = (
        "WCSAXES",
        "CTYPE",
        "CRVAL",
        "CRPIX",
        "CDELT",
        "CUNIT",
        "CNAME",
        "CD",
        "PC",
        "PV",
        "PS",
        "LONPOLE",
        "LATPOLE",
    )
    return stem.startswith(prefixes)


def _is_primary_spatial_wcs_card(key: str) -> bool:
    if key in {"WCSAXES", "WCSNAME", "RADESYS", "EQUINOX", "LONPOLE", "LATPOLE"}:
        return True
    if key in {"NAXIS", "NAXIS1", "NAXIS2"}:
        return True
    if key in {"CTYPE1", "CTYPE2", "CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CDELT1", "CDELT2", "CUNIT1", "CUNIT2"}:
        return True
    for prefix in ("CD", "PC"):
        if key.startswith(prefix) and any(key == f"{prefix}{i}_{j}" for i in (1, 2) for j in (1, 2)):
            return True
    for prefix in ("PV", "PS"):
        if key.startswith(prefix) and key[2:3] in {"1", "2"}:
            return True
    return False


def _is_sip_card(key: str) -> bool:
    if key in {"A_ORDER", "B_ORDER", "AP_ORDER", "BP_ORDER"}:
        return True
    for prefix in ("A_", "B_", "AP_", "BP_"):
        if key.startswith(prefix):
            parts = key[len(prefix):].split("_")
            return len(parts) == 2 and all(part.isdigit() for part in parts)
    return False


def _header_metadata(hdul: fits.HDUList) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for hdu_name in ["PRIMARY", "IMAGE", "PSF"]:
        hdu = hdul[0] if hdu_name == "PRIMARY" else hdul[hdu_name]
        for key in [
            "OBSID",
            "OBS_ID",
            "DETECTOR",
            "DETID",
            "VERSION",
            "PROCVER",
            "PROCDATE",
            "DATE-OBS",
            "MJD",
            "MJD-AVG",
            "EXPID",
            "BAND",
            "CRPIX1",
            "CRPIX2",
            "CRPIX1A",
            "CRPIX2A",
        ]:
            if key in hdu.header:
                metadata[f"{hdu_name.lower()}.{key}"] = _jsonable(hdu.header[key])
    return metadata


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
