"""PSF plane selection and native template rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .fits_io import CutoutData


@dataclass(slots=True)
class RenderedTemplate:
    image: np.ndarray
    fraction_in_cutout: float
    peak_x: float
    peak_y: float
    truncated: bool
    normalization_method: str
    template_sum_full: float
    template_sum_in_cutout: float
    template_sum_in_fit_mask: float | None
    fraction_unmasked: float | None
    psf_plane_index: int | None
    psf_zone_center_detector_x: float | None
    psf_zone_center_detector_y: float | None
    oversampling_factor: int
    subpixel_dx: float
    subpixel_dy: float
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "fraction_in_cutout": self.fraction_in_cutout,
            "peak_x": self.peak_x,
            "peak_y": self.peak_y,
            "truncated": self.truncated,
            "normalization_method": self.normalization_method,
            "template_sum_full": self.template_sum_full,
            "template_sum_in_cutout": self.template_sum_in_cutout,
            "template_sum_in_fit_mask": self.template_sum_in_fit_mask,
            "fraction_unmasked": self.fraction_unmasked,
            "psf_plane_index": self.psf_plane_index,
            "psf_zone_center_detector_x": self.psf_zone_center_detector_x,
            "psf_zone_center_detector_y": self.psf_zone_center_detector_y,
            "oversampling_factor": self.oversampling_factor,
            "subpixel_dx": self.subpixel_dx,
            "subpixel_dy": self.subpixel_dy,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class PsfPlaneSelection:
    plane: np.ndarray
    index: int | None
    center_x: float | None
    center_y: float | None
    warnings: list[str] = field(default_factory=list)


def select_psf_plane(cutout: CutoutData, detector_x: float, detector_y: float, *, allow_plane0_without_centers: bool = False) -> PsfPlaneSelection:
    psf = np.asarray(cutout.psf, dtype=float)
    if psf.ndim == 2:
        return PsfPlaneSelection(plane=psf, index=None, center_x=None, center_y=None)
    if psf.ndim != 3:
        raise ValueError("PSF HDU must be a 2D plane or 3D cube")
    centers, center_warnings = _psf_centers(cutout, psf.shape[0])
    if not centers:
        if allow_plane0_without_centers:
            return PsfPlaneSelection(plane=psf[0], index=0, center_x=None, center_y=None, warnings=["PSF_CENTER_MISSING_PLANE0_FALLBACK"])
        raise ValueError("3D PSF cube is missing plane center keywords")
    distances = [(x - detector_x) ** 2 + (y - detector_y) ** 2 for x, y in centers]
    index = int(np.argmin(distances))
    center_x, center_y = centers[index]
    return PsfPlaneSelection(plane=psf[index], index=index, center_x=center_x, center_y=center_y, warnings=center_warnings)


def render_point_template(
    cutout: CutoutData,
    cutout_x: float,
    cutout_y: float,
    detector_x: float,
    detector_y: float,
    *,
    radius_pixels: int,
    oversampling_factor: int = 10,
    fit_mask: np.ndarray | None = None,
    allow_plane0_without_centers: bool = False,
) -> RenderedTemplate:
    selection = select_psf_plane(cutout, detector_x, detector_y, allow_plane0_without_centers=allow_plane0_without_centers)
    plane = np.nan_to_num(np.asarray(selection.plane, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    plane = np.clip(plane, 0.0, None)
    total = float(np.sum(plane))
    if plane.size == 0 or total <= 0:
        raise ValueError("PSF plane has no positive flux")
    plane = plane / total
    oversampling = _oversampling(cutout, oversampling_factor)

    image = _bin_oversampled_psf(
        plane,
        shape=cutout.image_mjy_sr.shape,
        cutout_x=float(cutout_x),
        cutout_y=float(cutout_y),
        radius_pixels=int(radius_pixels),
        oversampling_factor=oversampling,
    )
    sum_in_cutout = float(np.sum(image))
    if sum_in_cutout <= 0:
        raise ValueError("rendered PSF template has no support in cutout")
    template_sum_in_fit_mask = None
    fraction_unmasked = None
    if fit_mask is not None:
        template_sum_in_fit_mask = float(np.sum(np.where(fit_mask, image, 0.0)))
        fraction_unmasked = template_sum_in_fit_mask / sum_in_cutout if sum_in_cutout > 0 else None
    return RenderedTemplate(
        image=image,
        fraction_in_cutout=sum_in_cutout,
        peak_x=float(cutout_x),
        peak_y=float(cutout_y),
        truncated=sum_in_cutout < 0.98,
        normalization_method="unit_total_response_oversampled_detector_pixel_binning",
        template_sum_full=1.0,
        template_sum_in_cutout=sum_in_cutout,
        template_sum_in_fit_mask=template_sum_in_fit_mask,
        fraction_unmasked=fraction_unmasked,
        psf_plane_index=selection.index,
        psf_zone_center_detector_x=selection.center_x,
        psf_zone_center_detector_y=selection.center_y,
        oversampling_factor=oversampling,
        subpixel_dx=float(cutout_x) - np.floor(float(cutout_x)),
        subpixel_dy=float(cutout_y) - np.floor(float(cutout_y)),
        warnings=selection.warnings,
    )


def _bin_oversampled_psf(
    plane: np.ndarray,
    *,
    shape: tuple[int, int],
    cutout_x: float,
    cutout_y: float,
    radius_pixels: int,
    oversampling_factor: int,
) -> np.ndarray:
    out = np.zeros(shape, dtype=float)
    yy_os, xx_os = np.indices(plane.shape, dtype=float)
    center_x_os = (plane.shape[1] - 1) / 2.0
    center_y_os = (plane.shape[0] - 1) / 2.0
    native_x = cutout_x + (xx_os - center_x_os) / float(oversampling_factor)
    native_y = cutout_y + (yy_os - center_y_os) / float(oversampling_factor)
    ix = np.floor(native_x + 0.5).astype(int)
    iy = np.floor(native_y + 0.5).astype(int)
    in_image = (ix >= 0) & (ix < shape[1]) & (iy >= 0) & (iy < shape[0])
    in_stamp = (np.abs(ix - cutout_x) <= radius_pixels + 0.5) & (np.abs(iy - cutout_y) <= radius_pixels + 0.5)
    use = in_image & in_stamp
    np.add.at(out, (iy[use], ix[use]), plane[use])
    return out


def _oversampling(cutout: CutoutData, default: int) -> int:
    value = cutout.psf_header.get("OVERSAMP", default)
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = int(default)
    return max(parsed, 1)


def _psf_centers(cutout: CutoutData, n_planes: int) -> tuple[list[tuple[float, float]], list[str]]:
    direct = _read_centers_in_header_order(cutout, n_planes)
    warnings: list[str] = []
    if len(direct) != n_planes:
        return [], [f"PSF_CENTER_COUNT_MISMATCH:{len(direct)}/{n_planes}"]
    corrected = _corrected_centers_from_comments(cutout, n_planes)
    if corrected is not None and _psf_header_fix_needed(cutout):
        warnings.append("PSF_HEADER_CORRECTION_APPLIED")
        return corrected, warnings
    return direct, warnings


def _read_centers_in_header_order(cutout: CutoutData, n_planes: int) -> list[tuple[float, float]]:
    centers: list[tuple[float, float]] = []
    for index in range(1, n_planes + 1):
        x = _header_value(cutout.psf_header, "XCTR", index)
        y = _header_value(cutout.psf_header, "YCTR", index)
        if x is not None and y is not None:
            centers.append((float(x), float(y)))
    return centers


def _header_value(header, prefix: str, index: int):
    for key in (f"{prefix}_{index}", f"{prefix}{index}", f"{prefix}{index:02d}"):
        if key in header:
            return header[key]
    return None


def _corrected_centers_from_comments(cutout: CutoutData, n_planes: int) -> list[tuple[float, float]] | None:
    x_by_ix: dict[int, float] = {}
    y_by_iy: dict[int, float] = {}
    max_ix = -1
    max_iy = -1
    for index in range(1, n_planes + 1):
        key = next((candidate for candidate in (f"XCTR_{index}", f"XCTR{index}", f"XCTR{index:02d}") if candidate in cutout.psf_header), None)
        if key is None:
            return None
        match = re.search(r"\((\d+)\s*,\s*(\d+)\)", str(cutout.psf_header.comments[key]))
        if match is None:
            return None
        ix = int(match.group(1))
        iy = int(match.group(2))
        x = _header_value(cutout.psf_header, "XCTR", index)
        y = _header_value(cutout.psf_header, "YCTR", index)
        if x is None or y is None:
            return None
        x_by_ix[ix] = float(x)
        y_by_iy[iy] = float(y)
        max_ix = max(max_ix, ix)
        max_iy = max(max_iy, iy)
    bins_x = max_ix + 1
    bins_y = max_iy + 1
    if bins_x * bins_y != n_planes:
        return None
    if set(x_by_ix) != set(range(bins_x)) or set(y_by_iy) != set(range(bins_y)):
        return None
    return [(x_by_ix[ix], y_by_iy[iy]) for iy in range(bins_y) for ix in range(bins_x)]


def _psf_header_fix_needed(cutout: CutoutData) -> bool:
    raw = cutout.primary_header.get("VERSION") or cutout.image_header.get("VERSION") or cutout.image_header.get("PROCVER")
    if raw is None:
        return False
    text = str(raw)
    if "psffix1" in text.lower():
        return False
    match = re.search(r"(\d+(?:\.\d+){1,2})", text)
    if match is None:
        return False
    parts = tuple(int(part) for part in match.group(1).split("."))
    while len(parts) < 3:
        parts += (0,)
    return parts <= (6, 5, 5)
