"""Pixel flag interpretation and masks for forced photometry."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from spherex_cutoutdb.config import Config

from .constants import SPHEREX_IMAGE_FLAG_BITS


@dataclass(slots=True)
class MaskSet:
    finite_mask: np.ndarray
    fit_mask: np.ndarray
    background_mask: np.ndarray
    source_finding_mask: np.ndarray
    central_aperture_mask: np.ndarray
    target_footprint_mask: np.ndarray
    target_protection_mask: np.ndarray
    neighbor_footprint_mask: np.ndarray
    edge_mask: np.ndarray
    fit_region_mask: np.ndarray
    background_region_mask: np.ndarray
    fallback_background_mask: np.ndarray
    invalid_variance_mask: np.ndarray
    fit_exclude_mask: np.ndarray
    background_exclude_mask: np.ndarray
    source_finding_exclude_mask: np.ndarray
    science_blocker_mask: np.ndarray
    flag_union_all: int
    flag_union_fit_stamp: int
    flag_union_central: int
    flag_union_target_footprint: int
    masked_template_fraction: float
    template_fraction_unmasked: float
    fit_mask_fraction: float
    background_mask_fraction: float
    source_finding_mask_fraction: float
    target_protection_fraction: float
    central_fit_mask_fraction: float
    fit_exclude_pixel_count: int
    background_pixel_count: int
    science_blocker_pixel_count: int
    flag_any_fraction_core: float
    flag_any_fraction_fit: float
    flag_any_fraction_background: float
    flag_hard_bad_fraction_core: float
    flag_hard_bad_fraction_fit: float
    flag_hard_bad_fraction_background: float
    flag_source_fraction_core: float
    flag_source_fraction_fit: float
    flag_source_fraction_background: float
    flag_science_reject_fraction_core: float
    flag_science_reject_fraction_fit: float
    flag_science_reject_fraction_background: float
    invalid_variance_fraction_core: float
    invalid_variance_fraction_fit: float
    invalid_variance_fraction_background: float
    psf_weighted_hard_bad_fraction: float
    psf_weighted_science_reject_fraction: float
    psf_weighted_invalid_variance_fraction: float
    flag_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "flag_union_all": self.flag_union_all,
            "flag_union_fit_stamp": self.flag_union_fit_stamp,
            "flag_union_central": self.flag_union_central,
            "flag_union_target_footprint": self.flag_union_target_footprint,
            "masked_template_fraction": self.masked_template_fraction,
            "template_fraction_unmasked": self.template_fraction_unmasked,
            "fit_mask_fraction": self.fit_mask_fraction,
            "background_mask_fraction": self.background_mask_fraction,
            "source_finding_mask_fraction": self.source_finding_mask_fraction,
            "target_protection_fraction": self.target_protection_fraction,
            "central_fit_mask_fraction": self.central_fit_mask_fraction,
            "fit_exclude_pixel_count": self.fit_exclude_pixel_count,
            "background_pixel_count": self.background_pixel_count,
            "science_blocker_pixel_count": self.science_blocker_pixel_count,
            "flag_any_fraction_core": self.flag_any_fraction_core,
            "flag_any_fraction_fit": self.flag_any_fraction_fit,
            "flag_any_fraction_background": self.flag_any_fraction_background,
            "flag_hard_bad_fraction_core": self.flag_hard_bad_fraction_core,
            "flag_hard_bad_fraction_fit": self.flag_hard_bad_fraction_fit,
            "flag_hard_bad_fraction_background": self.flag_hard_bad_fraction_background,
            "flag_source_fraction_core": self.flag_source_fraction_core,
            "flag_source_fraction_fit": self.flag_source_fraction_fit,
            "flag_source_fraction_background": self.flag_source_fraction_background,
            "flag_science_reject_fraction_core": self.flag_science_reject_fraction_core,
            "flag_science_reject_fraction_fit": self.flag_science_reject_fraction_fit,
            "flag_science_reject_fraction_background": self.flag_science_reject_fraction_background,
            "invalid_variance_fraction_core": self.invalid_variance_fraction_core,
            "invalid_variance_fraction_fit": self.invalid_variance_fraction_fit,
            "invalid_variance_fraction_background": self.invalid_variance_fraction_background,
            "psf_weighted_hard_bad_fraction": self.psf_weighted_hard_bad_fraction,
            "psf_weighted_science_reject_fraction": self.psf_weighted_science_reject_fraction,
            "psf_weighted_invalid_variance_fraction": self.psf_weighted_invalid_variance_fraction,
            "flag_counts": dict(self.flag_counts),
        }


def build_photometry_masks(
    *,
    flags: np.ndarray,
    data_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    target_template: np.ndarray,
    neighbor_templates: list[np.ndarray] | None,
    target_x: float,
    target_y: float,
    config: Config,
) -> MaskSet:
    flag_values = np.asarray(flags, dtype=np.int64)
    valid_data_mask = np.isfinite(data_uJy)
    valid_variance_mask = np.isfinite(variance_uJy2) & (variance_uJy2 > 0)
    invalid_variance_mask = ~valid_variance_mask
    finite_mask = valid_data_mask & valid_variance_mask
    target_template = np.nan_to_num(np.asarray(target_template, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)

    yy, xx = np.indices(data_uJy.shape)
    central_aperture_mask = (xx - float(target_x)) ** 2 + (yy - float(target_y)) ** 2 <= config.photometry.fit.central_radius_pixels**2
    target_footprint_mask = _template_footprint(target_template, config.photometry.masks.footprint_threshold)
    target_protection_footprint = _template_footprint(
        target_template,
        config.photometry.masks.target_protection_footprint_threshold,
    )
    target_protection_mask = (
        (xx - float(target_x)) ** 2 + (yy - float(target_y)) ** 2
        <= config.photometry.masks.target_protection_radius_pixels**2
    )
    target_protection_mask |= _dilate_mask(target_protection_footprint, config.photometry.masks.target_protection_dilate_pixels)
    edge_mask = _edge_mask(finite_mask.shape, config.photometry.masks.source_finding_edge_buffer_pixels)

    neighbor_footprint_mask = np.zeros_like(finite_mask, dtype=bool)
    for template in neighbor_templates or []:
        neighbor_footprint_mask |= _template_footprint(template, config.photometry.masks.footprint_threshold)

    fit_bits = _bit_value(config.photometry.masks.fit_exclude_bits)
    background_bits = _bit_value(config.photometry.masks.background_exclude_bits)
    science_bits = _bit_value(config.photometry.masks.science_blocker_bits)

    fit_exclude_mask = _has_any_bit(flag_values, fit_bits)
    background_exclude_mask = _has_any_bit(flag_values, background_bits)
    science_blocker_mask = _has_any_bit(flag_values, science_bits)
    source_flag_mask = _has_any_bit(flag_values, SPHEREX_IMAGE_FLAG_BITS["SOURCE"])
    any_flag_mask = flag_values != 0

    fit_mask = finite_mask & ~fit_exclude_mask
    fit_region_mask = target_footprint_mask | central_aperture_mask
    background_region_mask = ~target_protection_mask & ~neighbor_footprint_mask
    fallback_background_mask = finite_mask & ~fit_exclude_mask
    background_mask = finite_mask & ~background_exclude_mask & background_region_mask
    source_finding_exclude_mask = target_protection_mask | edge_mask
    source_finding_mask = fit_mask & ~source_finding_exclude_mask

    fit_stamp = fit_region_mask
    template_sum = float(np.nansum(np.clip(target_template, 0.0, None)))
    template_unmasked = float(np.nansum(np.where(fit_mask, np.clip(target_template, 0.0, None), 0.0)))
    template_fraction_unmasked = template_unmasked / template_sum if template_sum > 0 else 0.0

    central_count = int(np.count_nonzero(central_aperture_mask))
    central_fit_count = int(np.count_nonzero(fit_mask & central_aperture_mask))

    return MaskSet(
        finite_mask=finite_mask,
        fit_mask=fit_mask,
        background_mask=background_mask,
        source_finding_mask=source_finding_mask,
        central_aperture_mask=central_aperture_mask,
        target_footprint_mask=target_footprint_mask,
        target_protection_mask=target_protection_mask,
        neighbor_footprint_mask=neighbor_footprint_mask,
        edge_mask=edge_mask,
        fit_region_mask=fit_region_mask,
        background_region_mask=background_region_mask,
        fallback_background_mask=fallback_background_mask,
        invalid_variance_mask=invalid_variance_mask,
        fit_exclude_mask=fit_exclude_mask,
        background_exclude_mask=background_exclude_mask,
        source_finding_exclude_mask=source_finding_exclude_mask,
        science_blocker_mask=science_blocker_mask,
        flag_union_all=_flag_union(flag_values, np.ones_like(finite_mask, dtype=bool)),
        flag_union_fit_stamp=_flag_union(flag_values, fit_stamp),
        flag_union_central=_flag_union(flag_values, central_aperture_mask),
        flag_union_target_footprint=_flag_union(flag_values, target_footprint_mask),
        masked_template_fraction=1.0 - template_fraction_unmasked,
        template_fraction_unmasked=template_fraction_unmasked,
        fit_mask_fraction=float(np.count_nonzero(fit_mask)) / float(fit_mask.size),
        background_mask_fraction=float(np.count_nonzero(background_mask)) / float(background_mask.size),
        source_finding_mask_fraction=float(np.count_nonzero(source_finding_mask)) / float(source_finding_mask.size),
        target_protection_fraction=float(np.count_nonzero(target_protection_mask)) / float(target_protection_mask.size),
        central_fit_mask_fraction=float(central_fit_count) / float(max(central_count, 1)),
        fit_exclude_pixel_count=int(np.count_nonzero(fit_exclude_mask)),
        background_pixel_count=int(np.count_nonzero(background_mask)),
        science_blocker_pixel_count=int(np.count_nonzero(science_blocker_mask & fit_stamp)),
        flag_any_fraction_core=_fraction(any_flag_mask, central_aperture_mask),
        flag_any_fraction_fit=_fraction(any_flag_mask, fit_region_mask),
        flag_any_fraction_background=_fraction(any_flag_mask, background_region_mask),
        flag_hard_bad_fraction_core=_fraction(fit_exclude_mask, central_aperture_mask),
        flag_hard_bad_fraction_fit=_fraction(fit_exclude_mask, fit_region_mask),
        flag_hard_bad_fraction_background=_fraction(fit_exclude_mask, background_region_mask),
        flag_source_fraction_core=_fraction(source_flag_mask, central_aperture_mask),
        flag_source_fraction_fit=_fraction(source_flag_mask, fit_region_mask),
        flag_source_fraction_background=_fraction(source_flag_mask, background_region_mask),
        flag_science_reject_fraction_core=_fraction(science_blocker_mask, central_aperture_mask),
        flag_science_reject_fraction_fit=_fraction(science_blocker_mask, fit_region_mask),
        flag_science_reject_fraction_background=_fraction(science_blocker_mask, background_region_mask),
        invalid_variance_fraction_core=_fraction(invalid_variance_mask, central_aperture_mask),
        invalid_variance_fraction_fit=_fraction(invalid_variance_mask, fit_region_mask),
        invalid_variance_fraction_background=_fraction(invalid_variance_mask, background_region_mask),
        psf_weighted_hard_bad_fraction=_weighted_fraction(target_template, fit_exclude_mask),
        psf_weighted_science_reject_fraction=_weighted_fraction(target_template, science_blocker_mask),
        psf_weighted_invalid_variance_fraction=_weighted_fraction(target_template, invalid_variance_mask),
        flag_counts=_flag_counts(flag_values),
    )


def decode_flag_names(flag_value: int) -> list[str]:
    return [name for name, bit in SPHEREX_IMAGE_FLAG_BITS.items() if int(flag_value) & bit]


def _fraction(mask: np.ndarray, region: np.ndarray) -> float:
    selected = np.asarray(region, dtype=bool)
    denom = int(np.count_nonzero(selected))
    if denom <= 0:
        return 0.0
    return float(np.count_nonzero(np.asarray(mask, dtype=bool) & selected)) / float(denom)


def _weighted_fraction(template: np.ndarray, mask: np.ndarray) -> float:
    weights = np.clip(np.nan_to_num(np.asarray(template, dtype=float), nan=0.0, posinf=0.0, neginf=0.0), 0.0, None)
    denom = float(np.sum(weights))
    if denom <= 0:
        return 0.0
    return float(np.sum(np.where(np.asarray(mask, dtype=bool), weights, 0.0))) / denom


def _template_footprint(template: np.ndarray, threshold: float) -> np.ndarray:
    clean = np.nan_to_num(np.asarray(template, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    peak = float(np.nanmax(clean)) if clean.size else 0.0
    if peak <= 0:
        return np.zeros_like(clean, dtype=bool)
    return clean >= peak * float(threshold)


def _dilate_mask(mask: np.ndarray, pixels: int) -> np.ndarray:
    radius = max(int(pixels), 0)
    base = np.asarray(mask, dtype=bool)
    if radius <= 0 or not base.any():
        return base.copy()
    padded = np.pad(base, radius, mode="constant", constant_values=False)
    out = np.zeros_like(base, dtype=bool)
    for dy in range(2 * radius + 1):
        for dx in range(2 * radius + 1):
            out |= padded[dy : dy + base.shape[0], dx : dx + base.shape[1]]
    return out


def _edge_mask(shape: tuple[int, int], buffer_pixels: int) -> np.ndarray:
    buffer = max(int(buffer_pixels), 0)
    out = np.zeros(shape, dtype=bool)
    if buffer <= 0:
        return out
    out[:buffer, :] = True
    out[-buffer:, :] = True
    out[:, :buffer] = True
    out[:, -buffer:] = True
    return out


def _bit_value(bits: list[str | int]) -> int:
    value = 0
    for bit in bits:
        if isinstance(bit, int):
            value |= int(bit)
            continue
        name = str(bit).upper()
        if name not in SPHEREX_IMAGE_FLAG_BITS:
            raise ValueError(f"unknown SPHEREx flag bit name: {bit}")
        value |= SPHEREX_IMAGE_FLAG_BITS[name]
    return value


def _has_any_bit(flags: np.ndarray, bit_value: int) -> np.ndarray:
    if bit_value == 0:
        return np.zeros_like(flags, dtype=bool)
    return (np.asarray(flags, dtype=np.int64) & int(bit_value)) != 0


def _flag_union(flags: np.ndarray, mask: np.ndarray) -> int:
    selected = np.asarray(flags, dtype=np.int64)[mask]
    if selected.size == 0:
        return 0
    return int(np.bitwise_or.reduce(selected.ravel()))


def _flag_counts(flags: np.ndarray) -> dict[str, int]:
    out: dict[str, int] = {}
    values = np.asarray(flags, dtype=np.int64)
    for name, bit in SPHEREX_IMAGE_FLAG_BITS.items():
        count = int(np.count_nonzero((values & bit) != 0))
        if count:
            out[name] = count
    return out
