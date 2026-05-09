"""Solid-angle calibration sampling and flux-unit conversion."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from astropy.io import fits

from .constants import ARCSEC2_TO_SR, MJY_SR_TO_UJY_SR


def load_solid_angle_sr(path: Path, detector_x_grid: np.ndarray, detector_y_grid: np.ndarray) -> np.ndarray:
    path = Path(path)
    data = _load_first_2d_array(str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
    if data is None:
        raise ValueError("solid-angle calibration has no 2D image")
    sampled = sample_detector_map(data, detector_x_grid, detector_y_grid)
    sr = sampled * ARCSEC2_TO_SR
    if not np.isfinite(sr).any() or np.nanmedian(sr) <= 0:
        raise ValueError("solid-angle calibration produced invalid steradian values")
    return sr


@lru_cache(maxsize=12)
def _load_first_2d_array(path: str, mtime_ns: int, size_bytes: int) -> np.ndarray | None:
    del mtime_ns, size_bytes
    with fits.open(path, memmap=False, lazy_load_hdus=False) as hdul:
        for hdu in hdul:
            candidate = getattr(hdu, "data", None)
            if candidate is not None and np.asarray(candidate).ndim == 2:
                return np.asarray(candidate, dtype=float).copy()
    return None


def image_to_microjy_per_pixel(
    image_mjy_sr: np.ndarray,
    variance_mjy_sr2: np.ndarray,
    solid_angle_sr: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    factor = solid_angle_sr * MJY_SR_TO_UJY_SR
    return image_mjy_sr * factor, variance_mjy_sr2 * factor * factor


def sample_detector_map(data: np.ndarray, detector_x_grid: np.ndarray, detector_y_grid: np.ndarray) -> np.ndarray:
    detector_x_grid = np.asarray(detector_x_grid, dtype=float)
    detector_y_grid = np.asarray(detector_y_grid, dtype=float)
    if detector_x_grid.shape != detector_y_grid.shape:
        raise ValueError("detector coordinate grids must have matching shape")
    shape = detector_x_grid.shape
    if data.shape == shape and _grid_matches_data_indices(detector_x_grid, detector_y_grid):
        return np.asarray(data, dtype=float)
    x_index = np.rint(detector_x_grid).astype(int)
    y_index = np.rint(detector_y_grid).astype(int)
    x_index = np.clip(x_index, 0, data.shape[1] - 1)
    y_index = np.clip(y_index, 0, data.shape[0] - 1)
    return np.asarray(data, dtype=float)[y_index, x_index]


def _grid_matches_data_indices(detector_x_grid: np.ndarray, detector_y_grid: np.ndarray) -> bool:
    yy, xx = np.indices(detector_x_grid.shape, dtype=float)
    return bool(np.allclose(detector_x_grid, xx, atol=1.0e-6) and np.allclose(detector_y_grid, yy, atol=1.0e-6))
