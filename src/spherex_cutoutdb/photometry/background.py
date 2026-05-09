"""Source-masked local background estimation."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .constants import (
    FLAG_BACKGROUND_2D_UNSTABLE,
    FLAG_BACKGROUND_IMAGE_CLIPPED_FALLBACK,
)


@dataclass(slots=True)
class BackgroundResult:
    value_uJy_per_pixel: float
    uncertainty_uJy_per_pixel: float
    n_pixels: int
    mask_fraction: float
    clipped_fraction: float
    ok: bool
    reason: str | None = None
    method: str = "source_masked_constant"
    model: str = "constant"
    b0_uJy_per_pixel: float = 0.0
    bx_uJy_per_pixel: float = 0.0
    by_uJy_per_pixel: float = 0.0
    rms_uJy_per_pixel: float = float("nan")
    condition_number: float = float("nan")
    engine: str = "numpy"
    photutils_used: bool = False
    photutils_version: str | None = None
    photutils_box_size: int | None = None
    photutils_background_median: float | None = None
    photutils_rms_median: float | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    fallback_base_pixels: int = 0
    fallback_source_mask_pixels: int = 0
    fallback_valid_pixels: int = 0
    flags: list[str] = field(default_factory=list)
    background_image_uJy: np.ndarray | None = None
    mask_used: np.ndarray | None = None
    fallback_source_mask: np.ndarray | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "value_uJy_per_pixel": self.value_uJy_per_pixel,
            "uncertainty_uJy_per_pixel": self.uncertainty_uJy_per_pixel,
            "n_pixels": self.n_pixels,
            "mask_fraction": self.mask_fraction,
            "clipped_fraction": self.clipped_fraction,
            "ok": self.ok,
            "reason": self.reason,
            "method": self.method,
            "model": self.model,
            "b0_uJy_per_pixel": self.b0_uJy_per_pixel,
            "bx_uJy_per_pixel": self.bx_uJy_per_pixel,
            "by_uJy_per_pixel": self.by_uJy_per_pixel,
            "rms_uJy_per_pixel": self.rms_uJy_per_pixel,
            "condition_number": self.condition_number,
            "engine": self.engine,
            "photutils_used": self.photutils_used,
            "photutils_version": self.photutils_version,
            "photutils_box_size": self.photutils_box_size,
            "photutils_background_median": self.photutils_background_median,
            "photutils_rms_median": self.photutils_rms_median,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "fallback_base_pixels": self.fallback_base_pixels,
            "fallback_source_mask_pixels": self.fallback_source_mask_pixels,
            "fallback_valid_pixels": self.fallback_valid_pixels,
            "flags": list(self.flags),
        }


def estimate_2d_background(
    data_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    background_mask: np.ndarray,
    *,
    fallback_mask: np.ndarray | None = None,
    fallback_protection_mask: np.ndarray | None = None,
    model: str = "plane",
    min_pixels: int = 10,
    min_plane_pixels: int = 12,
    condition_number_max: float = 1.0e8,
    sigma_clip: float = 3.0,
    sigma_clip_iterations: int = 2,
    engine: str = "photutils",
    photutils_box_size: int = 16,
    photutils_filter_size: int = 3,
    photutils_exclude_percentile: float = 80.0,
    center_x: float | None = None,
    center_y: float | None = None,
) -> BackgroundResult:
    """Fit a source-masked local background image.

    The preferred V6 model is a robust clipped plane. If the nominal mask is
    too restrictive or the plane is unstable, the function rebuilds an
    image-clipped fallback mask and fits a smooth 2D background. If the
    fallback is still unconstrained, it returns ``ok=False`` instead of a
    zero-background science fallback.
    """

    data = np.asarray(data_uJy, dtype=float)
    variance = np.asarray(variance_uJy2, dtype=float)
    mask = np.asarray(background_mask, dtype=bool) & np.isfinite(data) & np.isfinite(variance) & (variance > 0)
    if data.size == 0:
        return _failed_background(data, "empty image")

    if str(model).lower() not in {"plane", "2d_plane", "source_masked_plane"}:
        return _constant_result(data, mask, min_pixels, sigma_clip, sigma_clip_iterations, "constant background requested")

    if int(np.count_nonzero(mask)) < int(min_plane_pixels):
        return _image_clipped_2d_fallback(
            data,
            variance,
            nominal_mask=mask,
            fallback_mask=fallback_mask,
            fallback_protection_mask=fallback_protection_mask,
            min_pixels=min_pixels,
            min_plane_pixels=min_plane_pixels,
            condition_number_max=condition_number_max,
            sigma_clip=sigma_clip,
            sigma_clip_iterations=sigma_clip_iterations,
            engine=engine,
            photutils_box_size=photutils_box_size,
            photutils_filter_size=photutils_filter_size,
            photutils_exclude_percentile=photutils_exclude_percentile,
            center_x=center_x,
            center_y=center_y,
            reason="few pixels for plane background",
        )

    yy, xx = np.indices(data.shape, dtype=float)
    x0 = float(center_x) if center_x is not None else (data.shape[1] - 1) / 2.0
    y0 = float(center_y) if center_y is not None else (data.shape[0] - 1) / 2.0
    xc = xx - x0
    yc = yy - y0
    fit_mask = mask.copy()
    photutils_probe = _photutils_probe(
        data,
        variance,
        fit_mask,
        sigma_clip=sigma_clip,
        sigma_clip_iterations=sigma_clip_iterations,
        engine=engine,
        box_size=photutils_box_size,
        filter_size=photutils_filter_size,
        exclude_percentile=photutils_exclude_percentile,
    )
    if photutils_probe is not None:
        fit_mask = _apply_photutils_prefilter(
            data,
            variance,
            fit_mask,
            photutils_probe,
            min_plane_pixels=min_plane_pixels,
            sigma_clip=sigma_clip,
        )
    original_count = int(np.count_nonzero(fit_mask))
    clipped_fraction = 0.0
    coeff = np.zeros(3, dtype=float)
    normal = np.zeros((3, 3), dtype=float)
    cond = float("inf")
    reason: str | None = None
    rms = float("nan")

    for _ in range(max(int(sigma_clip_iterations), 0) + 1):
        count = int(np.count_nonzero(fit_mask))
        if count < int(min_plane_pixels):
            reason = "few pixels after clipping"
            break
        coeff, normal, cond, solve_ok = _solve_weighted_plane(data, variance, fit_mask, xc, yc)
        if not solve_ok:
            reason = "plane background solve failed"
            break
        if not np.isfinite(cond) or cond > float(condition_number_max):
            reason = "ill-conditioned plane background"
            break

        residual = data[fit_mask] - (coeff[0] + coeff[1] * xc[fit_mask] + coeff[2] * yc[fit_mask])
        rms = _robust_sigma(residual)
        noise_floor = _noise_floor(variance[fit_mask])
        clip_sigma = max(rms if np.isfinite(rms) else 0.0, noise_floor if np.isfinite(noise_floor) else 0.0)
        if not (np.isfinite(clip_sigma) and clip_sigma > 0):
            break
        keep_values = np.abs(residual) <= float(sigma_clip) * clip_sigma
        clipped_fraction = 1.0 - float(np.count_nonzero(keep_values)) / float(max(count, 1))
        if keep_values.all():
            break
        next_mask = np.zeros_like(fit_mask, dtype=bool)
        current_indices = np.flatnonzero(fit_mask.ravel())
        next_mask.ravel()[current_indices[keep_values]] = True
        if np.array_equal(next_mask, fit_mask):
            break
        fit_mask = next_mask

    if reason is not None or int(np.count_nonzero(fit_mask)) < int(min_plane_pixels):
        return _image_clipped_2d_fallback(
            data,
            variance,
            nominal_mask=mask,
            fallback_mask=fallback_mask,
            fallback_protection_mask=fallback_protection_mask,
            min_pixels=min_pixels,
            min_plane_pixels=min_plane_pixels,
            condition_number_max=condition_number_max,
            sigma_clip=sigma_clip,
            sigma_clip_iterations=sigma_clip_iterations,
            engine=engine,
            photutils_box_size=photutils_box_size,
            photutils_filter_size=photutils_filter_size,
            photutils_exclude_percentile=photutils_exclude_percentile,
            center_x=center_x,
            center_y=center_y,
            reason=reason or "plane background unstable",
        )

    image = coeff[0] + coeff[1] * xc + coeff[2] * yc
    final_residual = data[fit_mask] - image[fit_mask]
    rms = max(_robust_sigma(final_residual), _noise_floor(variance[fit_mask]))
    n_final = int(np.count_nonzero(fit_mask))
    clipped_total = 1.0 - float(n_final) / float(max(original_count, 1))
    unc = float(rms / np.sqrt(max(n_final, 1))) if np.isfinite(rms) else float("inf")
    ok = n_final >= int(min_pixels)
    return BackgroundResult(
        value_uJy_per_pixel=float(coeff[0]),
        uncertainty_uJy_per_pixel=unc,
        n_pixels=n_final,
        mask_fraction=1.0 - float(np.count_nonzero(mask)) / float(mask.size),
        clipped_fraction=max(float(clipped_fraction), float(clipped_total)),
        ok=ok,
        reason=None if ok else "few background pixels",
        method="source_masked_plane",
        model="plane",
        b0_uJy_per_pixel=float(coeff[0]),
        bx_uJy_per_pixel=float(coeff[1]),
        by_uJy_per_pixel=float(coeff[2]),
        rms_uJy_per_pixel=float(rms),
        condition_number=float(cond),
        engine=_background_engine(engine, photutils_probe),
        photutils_used=photutils_probe is not None,
        photutils_version=photutils_probe.get("version") if photutils_probe else None,
        photutils_box_size=photutils_probe.get("box_size") if photutils_probe else None,
        photutils_background_median=photutils_probe.get("background_median") if photutils_probe else None,
        photutils_rms_median=photutils_probe.get("rms_median") if photutils_probe else None,
        flags=[] if ok else [FLAG_BACKGROUND_2D_UNSTABLE],
        background_image_uJy=np.asarray(image, dtype=float),
        mask_used=fit_mask.copy(),
    )


def estimate_background(
    data_uJy: np.ndarray,
    background_mask: np.ndarray,
    *,
    min_pixels: int = 10,
    sigma_clip: float = 3.0,
    sigma_clip_iterations: int = 2,
) -> BackgroundResult:
    """Compatibility wrapper for older callers that need a constant background."""

    data = np.asarray(data_uJy, dtype=float)
    mask = np.asarray(background_mask, dtype=bool) & np.isfinite(data)
    return _constant_result(data, mask, min_pixels, sigma_clip, sigma_clip_iterations, None)


def _image_clipped_2d_fallback(
    data: np.ndarray,
    variance: np.ndarray,
    *,
    nominal_mask: np.ndarray,
    fallback_mask: np.ndarray | None,
    fallback_protection_mask: np.ndarray | None,
    min_pixels: int,
    min_plane_pixels: int,
    condition_number_max: float,
    sigma_clip: float,
    sigma_clip_iterations: int,
    engine: str,
    photutils_box_size: int,
    photutils_filter_size: int,
    photutils_exclude_percentile: float,
    center_x: float | None,
    center_y: float | None,
    reason: str,
    extra_flags: list[str] | None = None,
) -> BackgroundResult:
    base = np.isfinite(data) & np.isfinite(variance) & (variance > 0)
    if fallback_mask is not None:
        candidate = np.asarray(fallback_mask, dtype=bool)
        if candidate.shape == data.shape:
            base &= candidate
    if fallback_protection_mask is not None:
        protected = np.asarray(fallback_protection_mask, dtype=bool)
        if protected.shape == data.shape:
            base &= ~protected

    base_pixels = int(np.count_nonzero(base))
    flags = _dedupe_flags([*(extra_flags or []), FLAG_BACKGROUND_IMAGE_CLIPPED_FALLBACK])
    if base_pixels < int(min_pixels):
        return _failed_background(
            data,
            reason=f"{reason}; image-clipped fallback has too few base pixels",
            flags=[*flags, FLAG_BACKGROUND_2D_UNSTABLE],
            fallback_base_pixels=base_pixels,
        )

    source_mask = _iterative_data_source_mask(
        data,
        base,
        sigma_clip=sigma_clip,
        sigma_clip_iterations=sigma_clip_iterations,
    )
    fit_mask = base & ~source_mask
    valid_pixels = int(np.count_nonzero(fit_mask))
    if valid_pixels < int(min_pixels):
        return _failed_background(
            data,
            reason=f"{reason}; image-clipped fallback has too few valid pixels",
            flags=[*flags, FLAG_BACKGROUND_2D_UNSTABLE],
            fallback_base_pixels=base_pixels,
            fallback_source_mask=source_mask,
            fallback_valid_pixels=valid_pixels,
        )

    photutils_result = _fallback_photutils_background(
        data,
        variance,
        fit_mask,
        source_mask,
        reason=reason,
        flags=flags,
        min_pixels=min_pixels,
        base_pixels=base_pixels,
        engine=engine,
        box_size=photutils_box_size,
        filter_size=photutils_filter_size,
        exclude_percentile=photutils_exclude_percentile,
        sigma_clip=sigma_clip,
        sigma_clip_iterations=sigma_clip_iterations,
        center_x=center_x,
        center_y=center_y,
    )
    if photutils_result is not None:
        return photutils_result

    if valid_pixels < int(min_plane_pixels):
        return _failed_background(
            data,
            reason=f"{reason}; image-clipped plane fallback has too few valid pixels",
            flags=[*flags, FLAG_BACKGROUND_2D_UNSTABLE],
            fallback_base_pixels=base_pixels,
            fallback_source_mask=source_mask,
            fallback_valid_pixels=valid_pixels,
        )

    yy, xx = np.indices(data.shape, dtype=float)
    x0 = float(center_x) if center_x is not None else (data.shape[1] - 1) / 2.0
    y0 = float(center_y) if center_y is not None else (data.shape[0] - 1) / 2.0
    xc = xx - x0
    yc = yy - y0
    plane_mask = fit_mask.copy()
    original_count = int(np.count_nonzero(plane_mask))
    coeff = np.zeros(3, dtype=float)
    cond = float("inf")
    failure_reason: str | None = None
    clipped_fraction = 0.0
    for _ in range(max(int(sigma_clip_iterations), 0) + 1):
        count = int(np.count_nonzero(plane_mask))
        if count < int(min_plane_pixels):
            failure_reason = "few pixels after image-clipped fallback plane clipping"
            break
        coeff, _, cond, solve_ok = _solve_weighted_plane(data, variance, plane_mask, xc, yc)
        if not solve_ok or not np.isfinite(cond) or cond > float(condition_number_max):
            failure_reason = "image-clipped fallback plane solve failed"
            break
        residual = data[plane_mask] - (coeff[0] + coeff[1] * xc[plane_mask] + coeff[2] * yc[plane_mask])
        rms = _robust_sigma(residual)
        noise_floor = _noise_floor(variance[plane_mask])
        clip_sigma = max(rms if np.isfinite(rms) else 0.0, noise_floor if np.isfinite(noise_floor) else 0.0)
        if not (np.isfinite(clip_sigma) and clip_sigma > 0):
            break
        keep_values = np.abs(residual) <= float(sigma_clip) * clip_sigma
        clipped_fraction = 1.0 - float(np.count_nonzero(keep_values)) / float(max(count, 1))
        if keep_values.all():
            break
        next_mask = np.zeros_like(plane_mask, dtype=bool)
        current_indices = np.flatnonzero(plane_mask.ravel())
        next_mask.ravel()[current_indices[keep_values]] = True
        plane_mask = next_mask

    if failure_reason is not None:
        return _failed_background(
            data,
            reason=f"{reason}; {failure_reason}",
            flags=[*flags, FLAG_BACKGROUND_2D_UNSTABLE],
            fallback_base_pixels=base_pixels,
            fallback_source_mask=source_mask,
            fallback_valid_pixels=int(np.count_nonzero(plane_mask)),
        )

    image = coeff[0] + coeff[1] * xc + coeff[2] * yc
    residual = data[plane_mask] - image[plane_mask]
    rms = max(_robust_sigma(residual), _noise_floor(variance[plane_mask]))
    n_final = int(np.count_nonzero(plane_mask))
    unc = float(rms / np.sqrt(max(n_final, 1))) if np.isfinite(rms) else float("inf")
    clipped_total = 1.0 - float(n_final) / float(max(original_count, 1))
    return BackgroundResult(
        value_uJy_per_pixel=float(coeff[0]),
        uncertainty_uJy_per_pixel=unc,
        n_pixels=n_final,
        mask_fraction=1.0 - float(np.count_nonzero(plane_mask)) / float(plane_mask.size),
        clipped_fraction=max(float(clipped_fraction), float(clipped_total)),
        ok=True,
        reason=None,
        method="image_clipped_plane_fallback",
        model="plane",
        b0_uJy_per_pixel=float(coeff[0]),
        bx_uJy_per_pixel=float(coeff[1]),
        by_uJy_per_pixel=float(coeff[2]),
        rms_uJy_per_pixel=float(rms),
        condition_number=float(cond),
        engine="numpy_plane",
        fallback_used=True,
        fallback_reason=reason,
        fallback_base_pixels=base_pixels,
        fallback_source_mask_pixels=int(np.count_nonzero(source_mask)),
        fallback_valid_pixels=n_final,
        flags=flags,
        background_image_uJy=np.asarray(image, dtype=float),
        mask_used=plane_mask.copy(),
        fallback_source_mask=source_mask.copy(),
    )


def _fallback_photutils_background(
    data: np.ndarray,
    variance: np.ndarray,
    fit_mask: np.ndarray,
    source_mask: np.ndarray,
    *,
    reason: str,
    flags: list[str],
    min_pixels: int,
    base_pixels: int,
    engine: str,
    box_size: int,
    filter_size: int,
    exclude_percentile: float,
    sigma_clip: float,
    sigma_clip_iterations: int,
    center_x: float | None,
    center_y: float | None,
) -> BackgroundResult | None:
    if str(engine).lower() not in {"auto", "photutils", "photutils_plane", "photutils_background2d"}:
        return None
    try:
        import photutils
        from astropy.stats import SigmaClip
        from photutils.background import Background2D, MADStdBackgroundRMS, MedianBackground
    except Exception:
        return None
    if int(np.count_nonzero(fit_mask)) < int(min_pixels):
        return None
    use_box = _photutils_box_size(data.shape, box_size)
    use_filter = _photutils_filter_size(filter_size)
    bad_mask = ~np.asarray(fit_mask, dtype=bool) | ~np.isfinite(data) | ~np.isfinite(variance) | (variance <= 0)
    kwargs = {
        "mask": bad_mask,
        "exclude_percentile": float(exclude_percentile),
        "filter_size": use_filter,
        "sigma_clip": SigmaClip(sigma=float(sigma_clip), maxiters=max(int(sigma_clip_iterations), 1)),
        "bkg_estimator": MedianBackground(),
        "bkg_rms_estimator": MADStdBackgroundRMS(),
    }
    try:
        try:
            bkg = Background2D(np.ascontiguousarray(data, dtype=float), use_box, **kwargs)
        except TypeError:
            kwargs["bkgrms_estimator"] = kwargs.pop("bkg_rms_estimator")
            bkg = Background2D(np.ascontiguousarray(data, dtype=float), use_box, **kwargs)
    except Exception:
        return None
    image = np.asarray(bkg.background, dtype=float)
    rms_map = np.asarray(bkg.background_rms, dtype=float)
    residual = data[fit_mask] - image[fit_mask]
    rms = _finite_median(rms_map[fit_mask])
    if rms is None or not np.isfinite(rms) or rms <= 0:
        rms = max(_robust_sigma(residual), _noise_floor(variance[fit_mask]))
    n_pix = int(np.count_nonzero(fit_mask))
    unc = float(rms / np.sqrt(max(n_pix, 1))) if np.isfinite(rms) else float("inf")
    x0 = int(round(float(center_x))) if center_x is not None else data.shape[1] // 2
    y0 = int(round(float(center_y))) if center_y is not None else data.shape[0] // 2
    x0 = min(max(x0, 0), data.shape[1] - 1)
    y0 = min(max(y0, 0), data.shape[0] - 1)
    center_value = float(image[y0, x0]) if np.isfinite(image[y0, x0]) else _finite_median(image[fit_mask])
    if center_value is None:
        center_value = float("nan")
    return BackgroundResult(
        value_uJy_per_pixel=float(center_value),
        uncertainty_uJy_per_pixel=unc,
        n_pixels=n_pix,
        mask_fraction=1.0 - float(np.count_nonzero(fit_mask)) / float(fit_mask.size),
        clipped_fraction=float(np.count_nonzero(source_mask)) / float(max(base_pixels, 1)),
        ok=True,
        reason=None,
        method="image_clipped_2d_fallback",
        model="background2d",
        b0_uJy_per_pixel=float(center_value),
        bx_uJy_per_pixel=0.0,
        by_uJy_per_pixel=0.0,
        rms_uJy_per_pixel=float(rms),
        condition_number=float("nan"),
        engine="photutils_background2d",
        photutils_used=True,
        photutils_version=getattr(photutils, "__version__", None),
        photutils_box_size=use_box[0] if use_box[0] == use_box[1] else list(use_box),
        photutils_background_median=_finite_median(image[fit_mask]),
        photutils_rms_median=_finite_median(rms_map[fit_mask]),
        fallback_used=True,
        fallback_reason=reason,
        fallback_base_pixels=base_pixels,
        fallback_source_mask_pixels=int(np.count_nonzero(source_mask)),
        fallback_valid_pixels=n_pix,
        flags=_dedupe_flags(flags),
        background_image_uJy=image,
        mask_used=fit_mask.copy(),
        fallback_source_mask=source_mask.copy(),
    )


def _iterative_data_source_mask(
    data: np.ndarray,
    base_mask: np.ndarray,
    *,
    sigma_clip: float,
    sigma_clip_iterations: int,
) -> np.ndarray:
    source_mask = np.zeros_like(base_mask, dtype=bool)
    fit_mask = np.asarray(base_mask, dtype=bool).copy()
    for _ in range(max(int(sigma_clip_iterations), 1)):
        values = data[fit_mask]
        if values.size == 0:
            break
        med = float(np.nanmedian(values))
        sigma = _robust_sigma(values)
        if not (np.isfinite(med) and np.isfinite(sigma) and sigma > 0):
            break
        next_source = np.asarray(base_mask, dtype=bool) & (np.abs(data - med) > float(sigma_clip) * sigma)
        next_fit = np.asarray(base_mask, dtype=bool) & ~next_source
        if np.array_equal(next_source, source_mask):
            break
        source_mask = next_source
        fit_mask = next_fit
    return source_mask


def _failed_background(
    data: np.ndarray,
    reason: str,
    *,
    flags: list[str] | None = None,
    fallback_base_pixels: int = 0,
    fallback_source_mask: np.ndarray | None = None,
    fallback_valid_pixels: int = 0,
) -> BackgroundResult:
    out_flags = _dedupe_flags([*(flags or []), FLAG_BACKGROUND_2D_UNSTABLE])
    source_mask_count = int(np.count_nonzero(fallback_source_mask)) if fallback_source_mask is not None else 0
    return BackgroundResult(
        value_uJy_per_pixel=float("nan"),
        uncertainty_uJy_per_pixel=float("inf"),
        n_pixels=int(fallback_valid_pixels),
        mask_fraction=1.0,
        clipped_fraction=1.0,
        ok=False,
        reason=reason,
        method="failed_background",
        model="none",
        rms_uJy_per_pixel=float("nan"),
        condition_number=float("inf"),
        fallback_used=FLAG_BACKGROUND_IMAGE_CLIPPED_FALLBACK in out_flags
        or bool(fallback_base_pixels or source_mask_count or fallback_valid_pixels),
        fallback_reason=reason,
        fallback_base_pixels=int(fallback_base_pixels),
        fallback_source_mask_pixels=source_mask_count,
        fallback_valid_pixels=int(fallback_valid_pixels),
        flags=out_flags,
        background_image_uJy=np.full_like(data, np.nan, dtype=float),
        mask_used=np.zeros_like(data, dtype=bool),
        fallback_source_mask=np.zeros_like(data, dtype=bool) if fallback_source_mask is None else fallback_source_mask.copy(),
    )


def _dedupe_flags(flags: list[str]) -> list[str]:
    return list(dict.fromkeys(flags))


def _solve_weighted_plane(
    data: np.ndarray,
    variance: np.ndarray,
    mask: np.ndarray,
    xc: np.ndarray,
    yc: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, bool]:
    values = data[mask]
    weights = 1.0 / variance[mask]
    x_values = xc[mask]
    y_values = yc[mask]
    sw = float(np.sum(weights))
    swx = float(np.sum(weights * x_values))
    swy = float(np.sum(weights * y_values))
    swxx = float(np.sum(weights * x_values * x_values))
    swxy = float(np.sum(weights * x_values * y_values))
    swyy = float(np.sum(weights * y_values * y_values))
    swz = float(np.sum(weights * values))
    swxz = float(np.sum(weights * x_values * values))
    swyz = float(np.sum(weights * y_values * values))
    normal = np.asarray(
        [
            [sw, swx, swy],
            [swx, swxx, swxy],
            [swy, swxy, swyy],
        ],
        dtype=float,
    )
    rhs = np.asarray([swz, swxz, swyz], dtype=float)
    try:
        cond = float(np.linalg.cond(normal))
        coeff = np.linalg.solve(normal, rhs)
    except np.linalg.LinAlgError:
        return np.zeros(3, dtype=float), normal, float("inf"), False
    return np.asarray(coeff, dtype=float), normal, cond, True


def _photutils_probe(
    data: np.ndarray,
    variance: np.ndarray,
    mask: np.ndarray,
    *,
    sigma_clip: float,
    sigma_clip_iterations: int,
    engine: str,
    box_size: int,
    filter_size: int,
    exclude_percentile: float,
) -> dict[str, object] | None:
    if str(engine).lower() not in {"auto", "photutils", "photutils_plane"}:
        return None
    try:
        import photutils
        from astropy.stats import SigmaClip
        from photutils.background import Background2D, MADStdBackgroundRMS, MedianBackground
    except Exception:
        return None
    bad_mask = ~np.asarray(mask, dtype=bool) | ~np.isfinite(data) | ~np.isfinite(variance) | (variance <= 0)
    if np.count_nonzero(~bad_mask) == 0:
        return None
    use_box = _photutils_box_size(data.shape, box_size)
    use_filter = _photutils_filter_size(filter_size)
    kwargs = {
        "mask": bad_mask,
        "exclude_percentile": float(exclude_percentile),
        "filter_size": use_filter,
        "sigma_clip": SigmaClip(sigma=float(sigma_clip), maxiters=max(int(sigma_clip_iterations), 1)),
        "bkg_estimator": MedianBackground(),
        "bkg_rms_estimator": MADStdBackgroundRMS(),
    }
    try:
        try:
            bkg = Background2D(
                np.ascontiguousarray(data, dtype=float),
                use_box,
                **kwargs,
            )
        except TypeError:
            kwargs["bkgrms_estimator"] = kwargs.pop("bkg_rms_estimator")
            bkg = Background2D(
                np.ascontiguousarray(data, dtype=float),
                use_box,
                **kwargs,
            )
    except Exception:
        return None
    background = np.asarray(bkg.background, dtype=float)
    rms = np.asarray(bkg.background_rms, dtype=float)
    return {
        "background": background,
        "rms": rms,
        "version": getattr(photutils, "__version__", None),
        "box_size": use_box[0] if use_box[0] == use_box[1] else list(use_box),
        "background_median": _finite_median(background[mask]),
        "rms_median": _finite_median(rms[mask]),
    }


def _apply_photutils_prefilter(
    data: np.ndarray,
    variance: np.ndarray,
    mask: np.ndarray,
    probe: dict[str, object],
    *,
    min_plane_pixels: int,
    sigma_clip: float,
) -> np.ndarray:
    background = np.asarray(probe["background"], dtype=float)
    rms_map = np.asarray(probe["rms"], dtype=float)
    residual = data[mask] - background[mask]
    rms_values = rms_map[mask]
    noise_floor = _noise_floor(variance[mask])
    clip_sigma = np.where(np.isfinite(rms_values) & (rms_values > 0), rms_values, noise_floor)
    keep_values = np.isfinite(residual) & np.isfinite(clip_sigma) & (np.abs(residual) <= float(sigma_clip) * clip_sigma)
    if int(np.count_nonzero(keep_values)) < int(min_plane_pixels):
        return mask
    if keep_values.all():
        return mask
    out = np.zeros_like(mask, dtype=bool)
    current_indices = np.flatnonzero(mask.ravel())
    out.ravel()[current_indices[keep_values]] = True
    return out


def _photutils_box_size(shape: tuple[int, int], requested: int) -> tuple[int, int]:
    value = max(int(requested), 2)
    return (min(value, int(shape[0])), min(value, int(shape[1])))


def _photutils_filter_size(requested: int) -> tuple[int, int]:
    value = max(int(requested), 1)
    if value % 2 == 0:
        value += 1
    return (value, value)


def _background_engine(engine: str, probe: dict[str, object] | None) -> str:
    if probe is not None:
        return "photutils_plane"
    return "numpy_plane" if str(engine).lower() in {"auto", "photutils", "photutils_plane"} else str(engine)


def _constant_result(
    data: np.ndarray,
    mask: np.ndarray,
    min_pixels: int,
    sigma_clip: float,
    sigma_clip_iterations: int,
    reason: str | None,
    *,
    extra_flags: list[str] | None = None,
) -> BackgroundResult:
    values = data[mask]
    flags = list(extra_flags or [])
    if values.size == 0:
        image = np.full_like(data, np.nan, dtype=float)
        return BackgroundResult(
            value_uJy_per_pixel=float("nan"),
            uncertainty_uJy_per_pixel=float("inf"),
            n_pixels=0,
            mask_fraction=1.0,
            clipped_fraction=1.0,
            ok=False,
            reason=reason or "no valid background pixels",
            method="failed_background",
            flags=flags or [FLAG_BACKGROUND_2D_UNSTABLE],
            background_image_uJy=image,
            mask_used=np.zeros_like(data, dtype=bool),
        )

    original_size = int(values.size)
    clipped = _sigma_clip_values(values, sigma_clip, sigma_clip_iterations, min_pixels)
    med = float(np.nanmedian(clipped))
    sigma = _robust_sigma(clipped)
    unc = float(sigma / np.sqrt(max(clipped.size, 1))) if np.isfinite(sigma) else float("inf")
    ok = clipped.size >= min_pixels
    if not ok and FLAG_BACKGROUND_2D_UNSTABLE not in flags:
        flags.append(FLAG_BACKGROUND_2D_UNSTABLE)
    image = np.full_like(data, med, dtype=float)
    return BackgroundResult(
        value_uJy_per_pixel=med,
        uncertainty_uJy_per_pixel=unc,
        n_pixels=int(clipped.size),
        mask_fraction=1.0 - float(np.count_nonzero(mask)) / float(mask.size),
        clipped_fraction=1.0 - float(clipped.size) / float(max(original_size, 1)),
        ok=ok,
        reason=None if ok and reason is None else reason or "few background pixels",
        method="source_masked_constant",
        model="constant",
        b0_uJy_per_pixel=med,
        bx_uJy_per_pixel=0.0,
        by_uJy_per_pixel=0.0,
        rms_uJy_per_pixel=float(sigma),
        condition_number=float("nan"),
        flags=flags,
        background_image_uJy=image,
        mask_used=mask.copy(),
    )


def _sigma_clip_values(values: np.ndarray, sigma_clip: float, iterations: int, min_pixels: int) -> np.ndarray:
    clipped = np.asarray(values, dtype=float)
    for _ in range(max(int(iterations), 0)):
        if clipped.size < min_pixels:
            break
        med = float(np.nanmedian(clipped))
        sigma = _robust_sigma(clipped)
        if not (np.isfinite(sigma) and sigma > 0):
            break
        keep = np.abs(clipped - med) <= float(sigma_clip) * sigma
        if keep.all() or not keep.any():
            break
        clipped = clipped[keep]
    return clipped


def _robust_sigma(values: np.ndarray) -> float:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return float("nan")
    med = float(np.nanmedian(finite))
    mad = float(np.nanmedian(np.abs(finite - med)))
    sigma = 1.4826 * mad if mad > 0 else float(np.nanstd(finite))
    return float(sigma)


def _noise_floor(variance_values: np.ndarray) -> float:
    finite = np.asarray(variance_values, dtype=float)
    finite = finite[np.isfinite(finite) & (finite > 0)]
    if finite.size == 0:
        return float("nan")
    return float(np.nanmedian(np.sqrt(finite)))


def _finite_median(values: np.ndarray) -> float | None:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return None
    return float(np.nanmedian(finite))
