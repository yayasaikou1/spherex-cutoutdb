"""Coordinate transforms for cutout photometry."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .fits_io import CutoutData


@dataclass(slots=True)
class SourcePixels:
    cutout_x: float
    cutout_y: float
    detector_x: float
    detector_y: float
    inside: bool
    detector_coordinate_method: str
    detector_coordinate_warnings: list[str]


def source_pixels(cutout: CutoutData, ra_deg: float, dec_deg: float) -> SourcePixels:
    x, y = cutout.spatial_wcs.world_to_pixel_values(float(ra_deg), float(dec_deg))
    image_shape = cutout.image_mjy_sr.shape
    det_x, det_y, method, warnings = cutout_to_detector_pixels(cutout, x, y)
    return SourcePixels(
        cutout_x=float(x),
        cutout_y=float(y),
        detector_x=float(det_x),
        detector_y=float(det_y),
        inside=(0 <= x < image_shape[1] and 0 <= y < image_shape[0]),
        detector_coordinate_method=method,
        detector_coordinate_warnings=warnings,
    )


def cutout_to_detector_pixels(cutout: CutoutData, x: float, y: float) -> tuple[float, float, str, list[str]]:
    header = cutout.image_header
    if "CRPIX1A" in header and "CRPIX2A" in header:
        return (
            1.0 + float(x) - float(header["CRPIX1A"]),
            1.0 + float(y) - float(header["CRPIX2A"]),
            "crpix_a_original_detector",
            [],
        )
    return float(x), float(y), "cutout_pixel_fallback", ["CRPIX1A/CRPIX2A missing; using cutout pixels as detector pixels"]


def cutout_detector_pixel_grid(cutout: CutoutData) -> tuple[np.ndarray, np.ndarray, str, list[str]]:
    yy, xx = np.indices(cutout.image_mjy_sr.shape, dtype=float)
    header = cutout.image_header
    if "CRPIX1A" in header and "CRPIX2A" in header:
        return (
            1.0 + xx - float(header["CRPIX1A"]),
            1.0 + yy - float(header["CRPIX2A"]),
            "crpix_a_original_detector",
            [],
        )
    return xx, yy, "cutout_pixel_fallback", ["CRPIX1A/CRPIX2A missing; using cutout pixel grid as detector grid"]
