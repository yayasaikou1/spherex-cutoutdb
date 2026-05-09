"""Spectral WCS calibration sampling."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from astropy.io import fits

from .solid_angle import sample_detector_map


@dataclass(slots=True)
class WavelengthSample:
    wavelength_um: float
    bandwidth_um: float
    method: str
    wavelength_center_um: float
    bandwidth_center_um: float


def sample_wavelength(
    path: Path,
    detector_x_grid: np.ndarray,
    detector_y_grid: np.ndarray,
    response_weights: np.ndarray | None = None,
) -> WavelengthSample:
    path = Path(path)
    cwave, cband = _load_cwave_cband(str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
    cwave_cut = sample_detector_map(cwave, detector_x_grid, detector_y_grid)
    cband_cut = sample_detector_map(cband, detector_x_grid, detector_y_grid)
    center_grid_y, center_grid_x = _center_grid_index(cwave_cut.shape, response_weights)
    center_det_x = float(np.asarray(detector_x_grid)[center_grid_y, center_grid_x])
    center_det_y = float(np.asarray(detector_y_grid)[center_grid_y, center_grid_x])
    center_y = min(max(int(round(center_det_y)), 0), cwave.shape[0] - 1)
    center_x = min(max(int(round(center_det_x)), 0), cwave.shape[1] - 1)
    center_wave = float(cwave[center_y, center_x])
    center_band = float(cband[center_y, center_x])
    if response_weights is not None and np.isfinite(response_weights).any() and float(np.nansum(response_weights)) > 0:
        weights = np.asarray(response_weights, dtype=float)
        good = np.isfinite(weights) & np.isfinite(cwave_cut) & np.isfinite(cband_cut) & (weights > 0)
        if good.any():
            total = float(np.sum(weights[good]))
            return WavelengthSample(
                wavelength_um=float(np.sum(weights[good] * cwave_cut[good]) / total),
                bandwidth_um=float(np.sum(weights[good] * cband_cut[good]) / total),
                method="spectral_wcs_psf_weighted",
                wavelength_center_um=center_wave,
                bandwidth_center_um=center_band,
            )
    return WavelengthSample(
        wavelength_um=center_wave,
        bandwidth_um=center_band,
        method="spectral_wcs_center",
        wavelength_center_um=center_wave,
        bandwidth_center_um=center_band,
    )


@lru_cache(maxsize=12)
def _load_cwave_cband(path: str, mtime_ns: int, size_bytes: int) -> tuple[np.ndarray, np.ndarray]:
    del mtime_ns, size_bytes
    with fits.open(path, memmap=False, lazy_load_hdus=False) as hdul:
        cwave = np.asarray(hdul["CWAVE"].data, dtype=float).copy()
        cband = np.asarray(hdul["CBAND"].data, dtype=float).copy()
    return cwave, cband


def _center_grid_index(shape: tuple[int, int], response_weights: np.ndarray | None) -> tuple[int, int]:
    if response_weights is not None:
        weights = np.asarray(response_weights, dtype=float)
        if weights.shape == shape and np.isfinite(weights).any() and float(np.nanmax(weights)) > 0:
            return tuple(int(v) for v in np.unravel_index(int(np.nanargmax(weights)), shape))
    return shape[0] // 2, shape[1] // 2
