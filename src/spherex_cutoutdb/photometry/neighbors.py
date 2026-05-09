"""Target-protected connected-component neighbor selection."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from spherex_cutoutdb.config import Config

from .masks import MaskSet


@dataclass(slots=True)
class NeighborCandidate:
    x: float
    y: float
    residual_snr: float
    overlap: float
    component_id: int = 0
    area_pixels: int = 0
    peak_x: float | None = None
    peak_y: float | None = None
    centroid_x: float | None = None
    centroid_y: float | None = None
    peak_snr: float | None = None
    integrated_snr: float | None = None
    distance_from_target_pixels: float | None = None
    touches_target_protection: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "x": self.x,
            "y": self.y,
            "residual_snr": self.residual_snr,
            "overlap": self.overlap,
            "component_id": self.component_id,
            "area_pixels": self.area_pixels,
            "peak_x": self.peak_x,
            "peak_y": self.peak_y,
            "centroid_x": self.centroid_x,
            "centroid_y": self.centroid_y,
            "peak_snr": self.peak_snr,
            "integrated_snr": self.integrated_snr,
            "distance_from_target_pixels": self.distance_from_target_pixels,
            "touches_target_protection": self.touches_target_protection,
        }


@dataclass(slots=True)
class NeighborSearchResult:
    candidates: list[NeighborCandidate] = field(default_factory=list)
    rejected_components: list[dict[str, object]] = field(default_factory=list)
    source_map: np.ndarray | None = None
    target_overlap_component_count: int = 0
    target_residual_peak_sigma: float = float("nan")
    threshold_sigma: float = float("nan")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "rejected_components": list(self.rejected_components),
            "target_overlap_component_count": self.target_overlap_component_count,
            "target_residual_peak_sigma": self.target_residual_peak_sigma,
            "threshold_sigma": self.threshold_sigma,
        }


def find_neighbor_components(
    residual_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    target_template: np.ndarray,
    masks: MaskSet,
    target_x: float,
    target_y: float,
    config: Config,
) -> NeighborSearchResult:
    if not config.photometry.deblending.enabled:
        return NeighborSearchResult(threshold_sigma=float(config.photometry.deblending.residual_snr_threshold))

    sigma = np.sqrt(np.where(variance_uJy2 > 0, variance_uJy2, np.nan))
    snr = np.where(np.isfinite(sigma) & (sigma > 0), residual_uJy / sigma, 0.0)
    threshold = float(config.photometry.deblending.residual_snr_threshold)
    yy, xx = np.indices(residual_uJy.shape)
    influence = (xx - float(target_x)) ** 2 + (yy - float(target_y)) ** 2 <= (config.photometry.fit_box_pixels / 2.0) ** 2
    source_finding = np.asarray(masks.source_finding_mask, dtype=bool)
    target_guard_radius = float(config.photometry.masks.target_protection_radius_pixels) + 1.5
    target_guard_mask = (
        np.asarray(masks.target_protection_mask, dtype=bool)
        | (((xx - float(target_x)) ** 2 + (yy - float(target_y)) ** 2) <= target_guard_radius**2)
    )
    positive = (snr >= threshold) & source_finding & influence & ~target_guard_mask & np.isfinite(snr)
    source_map = np.zeros_like(positive, dtype=int)
    target_positive = (
        (snr >= threshold)
        & np.asarray(masks.fit_mask, dtype=bool)
        & target_guard_mask
        & np.isfinite(snr)
    )
    target_peak_values = snr[target_positive]
    target_peak = float(np.nanmax(target_peak_values)) if target_peak_values.size else float("nan")
    target_overlap_count = len(_connected_components(target_positive))

    candidates: list[NeighborCandidate] = []
    rejected: list[dict[str, object]] = []
    accepted_centroids: list[tuple[float, float]] = []
    component_id = 0
    for pixels in _connected_components(positive):
        component_id += 1
        ys = np.asarray([pix[0] for pix in pixels], dtype=int)
        xs = np.asarray([pix[1] for pix in pixels], dtype=int)
        source_map[ys, xs] = component_id
        touches_target = bool(np.any(masks.target_protection_mask[ys, xs]))
        component = _component_summary(
            xs=xs,
            ys=ys,
            residual_uJy=residual_uJy,
            variance_uJy2=variance_uJy2,
            snr=snr,
            target_template=target_template,
            target_x=target_x,
            target_y=target_y,
            component_id=component_id,
        )
        peak_inside = _mask_value_at(masks.target_protection_mask, float(component["peak_x"]), float(component["peak_y"]))
        centroid_inside = _mask_value_at(masks.target_protection_mask, float(component["centroid_x"]), float(component["centroid_y"]))
        if (touches_target or peak_inside or centroid_inside) and config.photometry.deblending.reject_components_touching_target_protection:
            component["reason"] = "touches_target_protection"
            component["touches_target_protection"] = True
            rejected.append(component)
            continue
        if int(component["area_pixels"]) < int(config.photometry.deblending.min_component_pixels):
            component["reason"] = "small_component"
            rejected.append(component)
            continue
        if float(component["distance_from_target_pixels"]) <= target_guard_radius:
            component["reason"] = "near_target_protected_residual"
            component["touches_target_protection"] = True
            rejected.append(component)
            continue
        if float(component["distance_from_target_pixels"]) < float(config.photometry.deblending.min_distance_from_target_pixels):
            component["reason"] = "inside_minimum_target_distance"
            rejected.append(component)
            continue
        if not _is_material(component, config):
            component["reason"] = "low_target_influence"
            rejected.append(component)
            continue
        cx = float(component["centroid_x"])
        cy = float(component["centroid_y"])
        if _near_existing_component(cx, cy, accepted_centroids, config.photometry.deblending.merge_radius_pixels):
            component["reason"] = "merged_with_nearby_component"
            rejected.append(component)
            continue
        accepted_centroids.append((cx, cy))
        candidates.append(
            NeighborCandidate(
                x=cx,
                y=cy,
                residual_snr=float(component["peak_snr"]),
                overlap=float(component["overlap"]),
                component_id=component_id,
                area_pixels=int(component["area_pixels"]),
                peak_x=float(component["peak_x"]),
                peak_y=float(component["peak_y"]),
                centroid_x=cx,
                centroid_y=cy,
                peak_snr=float(component["peak_snr"]),
                integrated_snr=float(component["integrated_snr"]),
                distance_from_target_pixels=float(component["distance_from_target_pixels"]),
                touches_target_protection=False,
            )
        )

    candidates.sort(key=lambda candidate: float(candidate.peak_snr or candidate.residual_snr), reverse=True)
    max_neighbors = int(config.photometry.deblending.max_neighbors)
    if len(candidates) > max_neighbors:
        for candidate in candidates[max_neighbors:]:
            rejected.append({**candidate.as_dict(), "reason": "max_neighbors_exceeded"})
        candidates = candidates[:max_neighbors]

    return NeighborSearchResult(
        candidates=candidates,
        rejected_components=rejected,
        source_map=source_map,
        target_overlap_component_count=target_overlap_count + sum(1 for item in rejected if item.get("touches_target_protection")),
        target_residual_peak_sigma=target_peak,
        threshold_sigma=threshold,
    )


def find_material_neighbors(
    residual_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    target_template: np.ndarray,
    fit_mask: np.ndarray,
    target_x: float,
    target_y: float,
    config: Config,
) -> list[NeighborCandidate]:
    """Compatibility wrapper for older tests/callers."""

    yy, xx = np.indices(residual_uJy.shape)
    target_protection = (xx - float(target_x)) ** 2 + (yy - float(target_y)) ** 2 <= config.photometry.masks.target_protection_radius_pixels**2
    dummy_masks = MaskSet(
        finite_mask=np.asarray(fit_mask, dtype=bool),
        fit_mask=np.asarray(fit_mask, dtype=bool),
        background_mask=np.asarray(fit_mask, dtype=bool),
        source_finding_mask=np.asarray(fit_mask, dtype=bool) & ~target_protection,
        central_aperture_mask=target_protection,
        target_footprint_mask=target_protection,
        target_protection_mask=target_protection,
        neighbor_footprint_mask=np.zeros_like(fit_mask, dtype=bool),
        edge_mask=np.zeros_like(fit_mask, dtype=bool),
        fit_region_mask=target_protection,
        background_region_mask=np.asarray(fit_mask, dtype=bool),
        fallback_background_mask=np.asarray(fit_mask, dtype=bool),
        invalid_variance_mask=np.zeros_like(fit_mask, dtype=bool),
        fit_exclude_mask=np.zeros_like(fit_mask, dtype=bool),
        background_exclude_mask=np.zeros_like(fit_mask, dtype=bool),
        source_finding_exclude_mask=target_protection,
        science_blocker_mask=np.zeros_like(fit_mask, dtype=bool),
        flag_union_all=0,
        flag_union_fit_stamp=0,
        flag_union_central=0,
        flag_union_target_footprint=0,
        masked_template_fraction=0.0,
        template_fraction_unmasked=1.0,
        fit_mask_fraction=float(np.count_nonzero(fit_mask)) / float(fit_mask.size),
        background_mask_fraction=float(np.count_nonzero(fit_mask)) / float(fit_mask.size),
        source_finding_mask_fraction=float(np.count_nonzero(fit_mask & ~target_protection)) / float(fit_mask.size),
        target_protection_fraction=float(np.count_nonzero(target_protection)) / float(fit_mask.size),
        central_fit_mask_fraction=1.0,
        fit_exclude_pixel_count=0,
        background_pixel_count=int(np.count_nonzero(fit_mask)),
        science_blocker_pixel_count=0,
        flag_any_fraction_core=0.0,
        flag_any_fraction_fit=0.0,
        flag_any_fraction_background=0.0,
        flag_hard_bad_fraction_core=0.0,
        flag_hard_bad_fraction_fit=0.0,
        flag_hard_bad_fraction_background=0.0,
        flag_source_fraction_core=0.0,
        flag_source_fraction_fit=0.0,
        flag_source_fraction_background=0.0,
        flag_science_reject_fraction_core=0.0,
        flag_science_reject_fraction_fit=0.0,
        flag_science_reject_fraction_background=0.0,
        invalid_variance_fraction_core=0.0,
        invalid_variance_fraction_fit=0.0,
        invalid_variance_fraction_background=0.0,
        psf_weighted_hard_bad_fraction=0.0,
        psf_weighted_science_reject_fraction=0.0,
        psf_weighted_invalid_variance_fraction=0.0,
    )
    return find_neighbor_components(
        residual_uJy,
        variance_uJy2,
        target_template,
        dummy_masks,
        target_x,
        target_y,
        config,
    ).candidates


def _connected_components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    use = np.asarray(mask, dtype=bool)
    visited = np.zeros_like(use, dtype=bool)
    components: list[list[tuple[int, int]]] = []
    height, width = use.shape
    for y in range(height):
        for x in range(width):
            if not use[y, x] or visited[y, x]:
                continue
            stack = [(y, x)]
            visited[y, x] = True
            pixels: list[tuple[int, int]] = []
            while stack:
                cy, cx = stack.pop()
                pixels.append((cy, cx))
                for ny in range(max(0, cy - 1), min(height, cy + 2)):
                    for nx in range(max(0, cx - 1), min(width, cx + 2)):
                        if visited[ny, nx] or not use[ny, nx]:
                            continue
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            components.append(pixels)
    return components


def _component_summary(
    *,
    xs: np.ndarray,
    ys: np.ndarray,
    residual_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    snr: np.ndarray,
    target_template: np.ndarray,
    target_x: float,
    target_y: float,
    component_id: int,
) -> dict[str, object]:
    values = snr[ys, xs]
    peak_index = int(np.nanargmax(values))
    peak_x = float(xs[peak_index])
    peak_y = float(ys[peak_index])
    weights = np.clip(values, 0.0, None)
    if float(np.sum(weights)) > 0:
        centroid_x = float(np.sum(xs * weights) / np.sum(weights))
        centroid_y = float(np.sum(ys * weights) / np.sum(weights))
    else:
        centroid_x = float(np.mean(xs))
        centroid_y = float(np.mean(ys))
    variance = variance_uJy2[ys, xs]
    residual = residual_uJy[ys, xs]
    integrated_snr = float(np.nansum(residual) / np.sqrt(np.nansum(variance))) if np.nansum(variance) > 0 else float("nan")
    approx = _gaussian_like(target_template, centroid_x, centroid_y)
    distance = float(np.hypot(centroid_x - float(target_x), centroid_y - float(target_y)))
    return {
        "component_id": component_id,
        "area_pixels": int(xs.size),
        "peak_x": peak_x,
        "peak_y": peak_y,
        "centroid_x": centroid_x,
        "centroid_y": centroid_y,
        "peak_snr": float(values[peak_index]),
        "integrated_snr": integrated_snr,
        "distance_from_target_pixels": distance,
        "overlap": _template_overlap(target_template, approx),
    }


def _is_material(component: dict[str, object], config: Config) -> bool:
    peak_snr = abs(float(component["peak_snr"]))
    integrated_snr = abs(float(component["integrated_snr"]))
    snr_ok = (
        peak_snr >= float(config.photometry.deblending.residual_snr_threshold)
        or integrated_snr >= float(config.photometry.deblending.neighbor_flux_snr_threshold)
    )
    if not snr_ok:
        return False
    overlap = float(component["overlap"])
    distance = float(component["distance_from_target_pixels"])
    if overlap >= float(config.photometry.deblending.material_overlap_threshold):
        return True
    return distance <= float(config.photometry.fit_box_pixels) / 2.0 and integrated_snr >= float(config.photometry.deblending.neighbor_flux_snr_threshold)


def _mask_value_at(mask: np.ndarray, x: float, y: float) -> bool:
    ix = int(round(x))
    iy = int(round(y))
    if iy < 0 or iy >= mask.shape[0] or ix < 0 or ix >= mask.shape[1]:
        return False
    return bool(mask[iy, ix])


def _near_existing_component(x: float, y: float, existing: list[tuple[float, float]], merge_radius: float) -> bool:
    for ox, oy in existing:
        if float(np.hypot(x - ox, y - oy)) <= float(merge_radius):
            return True
    return False


def _gaussian_like(reference: np.ndarray, x: float, y: float) -> np.ndarray:
    yy, xx = np.indices(reference.shape)
    sigma = 1.2
    image = np.exp(-0.5 * (((xx - x) / sigma) ** 2 + ((yy - y) / sigma) ** 2))
    total = float(np.sum(image))
    return image / total if total > 0 else image


def _template_overlap(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    return float(np.sum(a * b) / denom) if denom > 0 else 0.0
