"""Single-cutout SPHEREx forced photometry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from spherex_cutoutdb.config import Config

from .background import BackgroundResult, estimate_2d_background
from .constants import (
    FLAG_BACKGROUND_POOR,
    FLAG_CENTRAL_POSITIVE_RESIDUAL_AFTER_NEGATIVE_MODEL,
    FLAG_CONTAMINATION_RISK,
    FLAG_DEBLEND_UNSTABLE,
    FLAG_DETECTOR_COORD_FALLBACK,
    FLAG_EDGE_OF_CUTOUT,
    FLAG_FIT_ERROR,
    FLAG_FIT_ILL_CONDITIONED,
    FLAG_LOW_TEMPLATE_SUPPORT,
    FLAG_PSF_COORDINATE_UNCERTAIN,
    FLAG_PSF_RENDERING_INVALID,
    FLAG_PSF_TRUNCATED,
    FLAG_SEVERE_IMAGE_FLAG,
    FLAG_TARGET_CORE_MASKED,
    FLAG_TARGET_POSSIBLY_EXTENDED,
    FLAG_TARGET_PSF_MISMATCH,
    FLAG_TARGET_SPLIT_PROTECTED,
)
from .coordinates import cutout_detector_pixel_grid, cutout_to_detector_pixels, source_pixels
from .fits_io import load_cutout
from .linear_fit import FitResult, weighted_linear_fit
from .masks import MaskSet, build_photometry_masks, decode_flag_names
from .neighbors import NeighborSearchResult, find_neighbor_components
from .psf import RenderedTemplate, render_point_template
from .science_selection import detection_status, science_recommended
from .solid_angle import image_to_microjy_per_pixel, load_solid_angle_sr
from .spectral_wcs import sample_wavelength


@dataclass(slots=True)
class MeasurementResult:
    row: dict[str, Any]
    provenance: dict[str, Any]
    qa_arrays: dict[str, np.ndarray] = field(default_factory=dict)


def measure_cutout(
    *,
    cutout_path: Path,
    source: dict[str, Any],
    cutout_row: dict[str, Any],
    calibration_resolution,
    config: Config,
    measurement_id: str,
    work_item_id: str,
) -> MeasurementResult:
    cutout = load_cutout(cutout_path)
    pixels = source_pixels(cutout, float(source["ra_deg"]), float(source["dec_deg"]))
    detector_x_grid, detector_y_grid, grid_method, grid_warnings = cutout_detector_pixel_grid(cutout)
    flags: list[str] = []
    warnings: list[str] = []
    warnings.extend(cutout.spatial_wcs_warnings)
    warnings.extend(pixels.detector_coordinate_warnings)
    warnings.extend(grid_warnings)
    if not pixels.inside:
        flags.append(FLAG_EDGE_OF_CUTOUT)
    if pixels.detector_coordinate_method == "cutout_pixel_fallback" or grid_method == "cutout_pixel_fallback":
        flags.append(FLAG_DETECTOR_COORD_FALLBACK)

    solid_path = calibration_resolution.path_for(config, "solid_angle_pixel_map")
    spectral_path = calibration_resolution.path_for(config, "spectral_wcs")
    if solid_path is None or spectral_path is None:
        raise ValueError("required calibration paths are missing")

    solid_sr = load_solid_angle_sr(solid_path, detector_x_grid, detector_y_grid)
    data_uJy, variance_uJy2 = image_to_microjy_per_pixel(
        cutout.image_mjy_sr,
        cutout.variance_mjy_sr2,
        solid_sr,
    )

    try:
        target_template = render_point_template(
            cutout,
            pixels.cutout_x,
            pixels.cutout_y,
            pixels.detector_x,
            pixels.detector_y,
            radius_pixels=config.photometry.psf_template_radius_pixels,
            oversampling_factor=config.photometry.psf.oversampling_factor,
            allow_plane0_without_centers=config.photometry.psf.allow_plane0_without_centers,
        )
    except ValueError:
        flags.append(FLAG_PSF_RENDERING_INVALID)
        raise
    flags.extend(_template_flags(target_template))

    masks = _build_masks(cutout, data_uJy, variance_uJy2, target_template.image, [], pixels, config)
    flags.extend(_mask_flags(masks, config))

    first_background = _estimate_background(data_uJy, variance_uJy2, masks, pixels, config)
    flags.extend(first_background.flags)
    if first_background.ok:
        first_background_sub = data_uJy - _background_image(first_background, data_uJy.shape)
        initial_point_fit = _fit(first_background_sub, variance_uJy2, [target_template.image], masks, first_background, config)
    else:
        first_background_sub = np.full_like(data_uJy, np.nan, dtype=float)
        initial_point_fit = _failed_fit(data_uJy, first_background.reason or "background failed")
    if not first_background.ok:
        flags.append(FLAG_BACKGROUND_POOR)
    elif not initial_point_fit.ok:
        flags.append(FLAG_FIT_ILL_CONDITIONED if initial_point_fit.fluxes_uJy.size else FLAG_FIT_ERROR)

    neighbor_rows: list[dict[str, Any]] = []
    neighbor_templates: list[np.ndarray] = []
    neighbor_search = NeighborSearchResult()
    deblend_status = "none"
    target_neighbor_max_corr = 0.0
    uncertainty_inflation = 1.0

    if initial_point_fit.ok:
        neighbor_search = find_neighbor_components(
            initial_point_fit.residual_uJy,
            variance_uJy2,
            target_template.image,
            masks,
            pixels.cutout_x,
            pixels.cutout_y,
            config,
        )
        if neighbor_search.target_overlap_component_count:
            flags.extend([FLAG_TARGET_SPLIT_PROTECTED, FLAG_TARGET_POSSIBLY_EXTENDED, FLAG_TARGET_PSF_MISMATCH])

        for candidate in neighbor_search.candidates:
            det_x, det_y, _, candidate_warnings = cutout_to_detector_pixels(cutout, candidate.x, candidate.y)
            warnings.extend(candidate_warnings)
            try:
                rendered = render_point_template(
                    cutout,
                    candidate.x,
                    candidate.y,
                    det_x,
                    det_y,
                    radius_pixels=config.photometry.psf_template_radius_pixels,
                    oversampling_factor=config.photometry.psf.oversampling_factor,
                    allow_plane0_without_centers=config.photometry.psf.allow_plane0_without_centers,
                )
            except ValueError as exc:
                warnings.append(f"neighbor PSF rendering failed at ({candidate.x:.2f},{candidate.y:.2f}): {exc}")
                flags.extend([FLAG_DEBLEND_UNSTABLE, FLAG_CONTAMINATION_RISK])
                continue
            neighbor_templates.append(rendered.image)
            neighbor_rows.append(
                {
                    **candidate.as_dict(),
                    "detector_x": det_x,
                    "detector_y": det_y,
                    "psf_plane_index": rendered.psf_plane_index,
                    "template_fraction_in_cutout": rendered.fraction_in_cutout,
                }
            )

    if neighbor_templates:
        masks = _build_masks(cutout, data_uJy, variance_uJy2, target_template.image, neighbor_templates, pixels, config)
        flags.extend(_mask_flags(masks, config))

    background = _estimate_background(data_uJy, variance_uJy2, masks, pixels, config)
    flags.extend(background.flags)
    background_failed = not background.ok
    if background_failed:
        flags.append(FLAG_BACKGROUND_POOR)
        background_sub = np.full_like(data_uJy, np.nan, dtype=float)
        point_fit = _failed_fit(data_uJy, background.reason or "background failed")
    else:
        background_sub = data_uJy - _background_image(background, data_uJy.shape)
        point_fit = _fit(background_sub, variance_uJy2, [target_template.image], masks, background, config)
    if (not background_failed) and not point_fit.ok:
        flags.append(FLAG_FIT_ILL_CONDITIONED if point_fit.fluxes_uJy.size else FLAG_FIT_ERROR)

    joint_fit = point_fit
    selected_fit = point_fit
    selected_fit_name = "point"
    if neighbor_templates and not background_failed:
        joint_fit = _fit(background_sub, variance_uJy2, [target_template.image, *neighbor_templates], masks, background, config)
        target_neighbor_max_corr = float(joint_fit.metadata.get("target_neighbor_max_corr", 0.0))
        point_err = point_fit.target_flux_err_uJy
        joint_err = joint_fit.target_flux_err_uJy
        uncertainty_inflation = float(joint_err / point_err) if point_err > 0 and np.isfinite(point_err) else float("inf")
        target_protection_problem = any(
            flag in flags for flag in [FLAG_TARGET_SPLIT_PROTECTED, FLAG_TARGET_POSSIBLY_EXTENDED, FLAG_TARGET_PSF_MISMATCH]
        )
        if _joint_fit_is_stable(joint_fit, point_fit, target_neighbor_max_corr, uncertainty_inflation, target_protection_problem, config):
            selected_fit = joint_fit
            selected_fit_name = "joint"
            deblend_status = "joint_stable"
        else:
            selected_fit = point_fit
            selected_fit_name = "point"
            deblend_status = "needed_unstable" if not target_protection_problem else "blocked_target_protection"
            flags.extend([FLAG_DEBLEND_UNSTABLE, FLAG_CONTAMINATION_RISK])

    if (not background_failed) and not selected_fit.ok:
        flags.append(FLAG_FIT_ILL_CONDITIONED if selected_fit.fluxes_uJy.size else FLAG_FIT_ERROR)

    target_template.template_sum_in_fit_mask = float(np.sum(np.where(masks.fit_mask, target_template.image, 0.0)))
    target_template.fraction_unmasked = (
        target_template.template_sum_in_fit_mask / target_template.fraction_in_cutout
        if target_template.fraction_in_cutout > 0
        else 0.0
    )
    point_metrics = (
        _invalid_central_metrics()
        if background_failed
        else _central_residual_metrics(point_fit, variance_uJy2, masks, pixels.cutout_x, pixels.cutout_y)
    )
    joint_metrics = (
        _invalid_central_metrics()
        if background_failed
        else _central_residual_metrics(joint_fit, variance_uJy2, masks, pixels.cutout_x, pixels.cutout_y)
    )
    selected_metrics = point_metrics if selected_fit_name == "point" else joint_metrics
    if (
        not background_failed
        and selected_fit.ok
        and selected_fit.target_flux_uJy < 0
        and selected_metrics["central_residual_peak_sigma"] >= config.photometry.fit.central_positive_residual_sigma
    ):
        flags.append(FLAG_CENTRAL_POSITIVE_RESIDUAL_AFTER_NEGATIVE_MODEL)
    if (
        not background_failed
        and point_fit.ok
        and point_metrics["central_residual_peak_sigma"] >= config.photometry.fit.central_positive_residual_sigma
        and not neighbor_rows
    ):
        flags.extend([FLAG_TARGET_POSSIBLY_EXTENDED, FLAG_TARGET_PSF_MISMATCH])
    if (
        not background_failed
        and selected_fit.ok
        and selected_metrics["central_residual_median_abs_sigma"] >= config.photometry.fit.science_fit_quality_max
    ):
        flags.append(FLAG_TARGET_PSF_MISMATCH)

    selected_flux = float("nan") if background_failed else selected_fit.target_flux_uJy
    selected_err = float("nan") if background_failed else selected_fit.target_flux_err_uJy
    if background_failed:
        detection, detection_flags, snr = "invalid_background", [], float("nan")
    else:
        detection, detection_flags, snr = detection_status(selected_flux, selected_err, config)
    flags.extend(detection_flags)
    recommended, all_flags, science_reject_reason, qa_grade = science_recommended(
        detection=detection,
        flags=flags,
        calibration_exact_match=bool(getattr(calibration_resolution, "exact_match", False)),
        calibration_ok_for_science=bool(getattr(calibration_resolution, "ok", True)),
        fit_ok=bool(selected_fit.ok) if not background_failed else True,
        background_ok=bool(background.ok),
    )
    if background_failed:
        recommended = False
        science_reject_reason = FLAG_BACKGROUND_POOR
        qa_grade = "reject"

    response_weights = _response_weights(target_template.image, variance_uJy2, masks.fit_mask)
    wavelength = sample_wavelength(
        spectral_path,
        detector_x_grid,
        detector_y_grid,
        response_weights=response_weights,
    )
    fit_quality = selected_metrics["central_residual_median_abs_sigma"]
    image_flag_union = masks.flag_union_all
    science_mode = "joint_point_source" if selected_fit_name == "joint" and selected_fit.ok else "point_source_forced"
    if background_failed:
        science_mode = "failed_background"
    elif not selected_fit.ok:
        science_mode = "invalid_fit"
    measurement_status = "failed_background" if background_failed else ("measured" if selected_fit.ok else "invalid_fit")
    detector_flag_names = decode_flag_names(image_flag_union)
    key_flags = _dedupe(all_flags)[:8]
    mjd = _metadata_value(cutout, cutout_row, ["mjd", "MJD", "image.MJD", "primary.MJD"])
    mjd_avg = _metadata_value(cutout, cutout_row, ["mjd_avg", "MJD-AVG", "MJD_AVG", "image.MJD-AVG", "primary.MJD-AVG"])

    row = {
        "source_id": source["source_id"],
        "source_name": source.get("source_name"),
        "ra_deg": float(source["ra_deg"]),
        "dec_deg": float(source["dec_deg"]),
        "measurement_id": measurement_id,
        "work_item_id": work_item_id,
        "cutout_key": cutout_row["cutout_key"],
        "product_id": cutout_row.get("product_id"),
        "parent_filename": cutout_row.get("parent_filename"),
        "observation_id": cutout_row.get("observation_id"),
        "detector_id": cutout_row.get("detector_id"),
        "collection": cutout_row.get("collection"),
        "processing_version": cutout_row.get("processing_version"),
        "processing_date": cutout_row.get("processing_date"),
        "mjd": _float_or_nan(mjd),
        "mjd_avg": _float_or_nan(mjd_avg),
        "cutout_sha256": cutout_row.get("sha256"),
        "input_image_extension": "IMAGE",
        "zodi_used": False,
        "wavelength_um": wavelength.wavelength_um,
        "bandwidth_um": wavelength.bandwidth_um,
        "wavelength_method": wavelength.method,
        "wavelength_center_um": wavelength.wavelength_center_um,
        "bandwidth_center_um": wavelength.bandwidth_center_um,
        "point_flux_uJy": point_fit.target_flux_uJy,
        "point_flux_err_uJy": point_fit.target_flux_err_uJy,
        "joint_flux_uJy": joint_fit.target_flux_uJy if joint_fit.fluxes_uJy.size else point_fit.target_flux_uJy,
        "joint_flux_err_uJy": joint_fit.target_flux_err_uJy if joint_fit.fluxes_uJy.size else point_fit.target_flux_err_uJy,
        "selected_flux_uJy": selected_flux,
        "selected_flux_err_uJy": selected_err,
        "selected_snr": snr,
        "measurement_status": measurement_status,
        "measurement_ok": bool(selected_fit.ok) and not background_failed,
        "science_mode": science_mode,
        "selected_fit": selected_fit_name,
        "science_recommended": bool(recommended),
        "science_reject_reason": science_reject_reason,
        "qa_grade": qa_grade,
        "detection_status": detection,
        "upper_limit_3sigma_uJy": 3.0 * selected_err if np.isfinite(selected_err) else np.nan,
        "photometry_flags": ";".join(_dedupe(all_flags)),
        "key_photometry_flags": ";".join(key_flags),
        "image_flags": image_flag_union,
        "image_flag_names": ";".join(detector_flag_names),
        "severe_flag": bool(masks.science_blocker_pixel_count),
        "fit_quality": fit_quality,
        "fit_ql_mean_abs_2p5pix": selected_metrics["fit_ql_mean_abs_2p5pix"],
        "point_fit_quality": point_metrics["central_residual_median_abs_sigma"],
        "joint_fit_quality": joint_metrics["central_residual_median_abs_sigma"],
        "point_fit_ql_mean_abs_2p5pix": point_metrics["fit_ql_mean_abs_2p5pix"],
        "joint_fit_ql_mean_abs_2p5pix": joint_metrics["fit_ql_mean_abs_2p5pix"],
        "chi2_reduced": selected_fit.chi2_reduced,
        "point_chi2_reduced": point_fit.chi2_reduced,
        "joint_chi2_reduced": joint_fit.chi2_reduced,
        "n_valid_pixels": selected_fit.n_valid_pixels,
        "x_cutout": pixels.cutout_x,
        "y_cutout": pixels.cutout_y,
        "x_detector": pixels.detector_x,
        "y_detector": pixels.detector_y,
        "detector_coordinate_method": pixels.detector_coordinate_method,
        "template_fraction_in_cutout": target_template.fraction_in_cutout,
        "template_fraction_unmasked": masks.template_fraction_unmasked,
        "fit_mask_fraction": masks.fit_mask_fraction,
        "background_mask_fraction": masks.background_mask_fraction,
        "source_finding_mask_fraction": masks.source_finding_mask_fraction,
        "target_protection_fraction": masks.target_protection_fraction,
        "flag_any_fraction_core": masks.flag_any_fraction_core,
        "flag_any_fraction_fit": masks.flag_any_fraction_fit,
        "flag_any_fraction_background": masks.flag_any_fraction_background,
        "flag_hard_bad_fraction_core": masks.flag_hard_bad_fraction_core,
        "flag_hard_bad_fraction_fit": masks.flag_hard_bad_fraction_fit,
        "flag_hard_bad_fraction_background": masks.flag_hard_bad_fraction_background,
        "flag_source_fraction_core": masks.flag_source_fraction_core,
        "flag_source_fraction_fit": masks.flag_source_fraction_fit,
        "flag_source_fraction_background": masks.flag_source_fraction_background,
        "flag_science_reject_fraction_core": masks.flag_science_reject_fraction_core,
        "flag_science_reject_fraction_fit": masks.flag_science_reject_fraction_fit,
        "flag_science_reject_fraction_background": masks.flag_science_reject_fraction_background,
        "invalid_variance_fraction_core": masks.invalid_variance_fraction_core,
        "invalid_variance_fraction_fit": masks.invalid_variance_fraction_fit,
        "invalid_variance_fraction_background": masks.invalid_variance_fraction_background,
        "psf_weighted_hard_bad_fraction": masks.psf_weighted_hard_bad_fraction,
        "psf_weighted_science_reject_fraction": masks.psf_weighted_science_reject_fraction,
        "psf_weighted_invalid_variance_fraction": masks.psf_weighted_invalid_variance_fraction,
        "target_protection_radius_pixels": config.photometry.masks.target_protection_radius_pixels,
        "central_residual_median_abs_sigma": selected_metrics["central_residual_median_abs_sigma"],
        "central_residual_peak_sigma": selected_metrics["central_residual_peak_sigma"],
        "central_residual_mean_sigma": selected_metrics["central_residual_mean_sigma"],
        "point_central_residual_peak_sigma": point_metrics["central_residual_peak_sigma"],
        "joint_central_residual_peak_sigma": joint_metrics["central_residual_peak_sigma"],
        "target_residual_peak_sigma": neighbor_search.target_residual_peak_sigma,
        "target_overlap_component_count": neighbor_search.target_overlap_component_count,
        "background_uJy_per_pixel": background.value_uJy_per_pixel,
        "background_unc_uJy_per_pixel": background.uncertainty_uJy_per_pixel,
        "background_method": background.method,
        "background_model": background.model,
        "background_engine": background.engine,
        "background_b0_uJy_per_pixel": background.b0_uJy_per_pixel,
        "background_bx_uJy_per_pixel": background.bx_uJy_per_pixel,
        "background_by_uJy_per_pixel": background.by_uJy_per_pixel,
        "background_rms_uJy_per_pixel": background.rms_uJy_per_pixel,
        "background_condition_number": background.condition_number,
        "background_photutils_used": bool(background.photutils_used),
        "background_photutils_version": background.photutils_version,
        "background_photutils_box_size": background.photutils_box_size,
        "background_npix": background.n_pixels,
        "background_ok": bool(background.ok),
        "background_clipped_fraction": background.clipped_fraction,
        "background_flags": ";".join(background.flags),
        "background_reason": background.reason,
        "background_fallback_used": bool(background.fallback_used),
        "background_fallback_reason": background.fallback_reason,
        "background_fallback_base_pixels": background.fallback_base_pixels,
        "background_fallback_source_mask_pixels": background.fallback_source_mask_pixels,
        "background_fallback_valid_pixels": background.fallback_valid_pixels,
        "deblend_status": deblend_status,
        "n_neighbors": len(neighbor_rows),
        "n_neighbor_candidates": len(neighbor_search.candidates),
        "n_rejected_neighbor_components": len(neighbor_search.rejected_components),
        "target_neighbor_max_corr": target_neighbor_max_corr,
        "uncertainty_inflation": uncertainty_inflation,
        "fit_condition_number": selected_fit.condition_number,
        "matched_filter_numerator": selected_fit.metadata.get("matched_filter_numerator"),
        "matched_filter_denominator": selected_fit.metadata.get("matched_filter_denominator"),
        "psf_plane_index": target_template.psf_plane_index,
        "psf_zone_center_detector_x": target_template.psf_zone_center_detector_x,
        "psf_zone_center_detector_y": target_template.psf_zone_center_detector_y,
        "psf_oversampling_factor": target_template.oversampling_factor,
        "psf_subpixel_dx": target_template.subpixel_dx,
        "psf_subpixel_dy": target_template.subpixel_dy,
        "calibration_exact_match": bool(getattr(calibration_resolution, "exact_match", False)),
        "calibration_match_quality": getattr(calibration_resolution, "match_quality", "exact_match" if bool(getattr(calibration_resolution, "exact_match", False)) else "unknown"),
        "detector_release_match": bool(getattr(calibration_resolution, "detector_release_match", bool(getattr(calibration_resolution, "exact_match", False)))),
        "header_reference_match": bool(getattr(calibration_resolution, "header_reference_match", False)),
        "spectral_wcs_calibration_id": calibration_resolution.products["spectral_wcs"]["calibration_id"],
        "solid_angle_calibration_id": calibration_resolution.products["solid_angle_pixel_map"]["calibration_id"],
        "output_schema_version": config.photometry.output_schema_version,
        "photometry_code_version": config.photometry.code_version,
    }
    provenance = {
        "measurement_id": measurement_id,
        "input_image_extension": "IMAGE",
        "zodi_used": False,
        "source": source,
        "cutout": cutout_row,
        "pixels": {
            "cutout_x": pixels.cutout_x,
            "cutout_y": pixels.cutout_y,
            "detector_x": pixels.detector_x,
            "detector_y": pixels.detector_y,
            "detector_coordinate_method": pixels.detector_coordinate_method,
            "detector_coordinate_warnings": pixels.detector_coordinate_warnings,
            "detector_grid_method": grid_method,
            "detector_grid_warnings": grid_warnings,
        },
        "spatial_wcs_warnings": cutout.spatial_wcs_warnings,
        "warnings": warnings,
        "calibration": calibration_resolution.products,
        "first_pass_background": first_background.as_dict(),
        "background": background.as_dict(),
        "masks": masks.as_dict(),
        "target_template": target_template.as_dict(),
        "neighbor_search": neighbor_search.as_dict(),
        "neighbors": neighbor_rows,
        "point_fit": _fit_summary(point_fit),
        "joint_fit": _fit_summary(joint_fit),
        "point_central_residual_metrics": point_metrics,
        "joint_central_residual_metrics": joint_metrics,
        "central_residual_metrics": selected_metrics,
        "flags": _dedupe(all_flags),
    }
    qa_arrays = {
        "raw_image": data_uJy,
        "background_2d": _background_image(background, data_uJy.shape),
        "data": background_sub,
        "data_background_subtracted": background_sub,
        "model": selected_fit.model_uJy,
        "residual": selected_fit.residual_uJy,
        "residual_sigma": _residual_sigma(selected_fit, variance_uJy2),
        "point_model": point_fit.model_uJy,
        "point_residual": point_fit.residual_uJy,
        "point_residual_sigma": _residual_sigma(point_fit, variance_uJy2),
        "joint_model": joint_fit.model_uJy,
        "joint_residual": joint_fit.residual_uJy,
        "joint_residual_sigma": _residual_sigma(joint_fit, variance_uJy2),
        "template": target_template.image,
        "fit_mask": masks.fit_mask.astype(float),
        "background_mask": masks.background_mask.astype(float),
        "background_fit_mask": _mask_or_zeros(background.mask_used, data_uJy.shape),
        "background_fallback_source_mask": _mask_or_zeros(background.fallback_source_mask, data_uJy.shape),
        "hard_bad_mask": masks.fit_exclude_mask.astype(float),
        "invalid_variance_mask": masks.invalid_variance_mask.astype(float),
        "source_finding_mask": masks.source_finding_mask.astype(float),
        "target_protection_mask": masks.target_protection_mask.astype(float),
        "flag_mask": (np.asarray(cutout.flags, dtype=np.int64) != 0).astype(float),
        "source_map": neighbor_search.source_map.astype(float) if neighbor_search.source_map is not None else np.zeros_like(data_uJy),
        "mask_source_map": _mask_source_map(masks, cutout.flags, neighbor_search, background),
    }
    return MeasurementResult(row=row, provenance=provenance, qa_arrays=qa_arrays)


def _build_masks(
    cutout,
    data_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    target_template: np.ndarray,
    neighbor_templates: list[np.ndarray],
    pixels,
    config: Config,
) -> MaskSet:
    return build_photometry_masks(
        flags=cutout.flags,
        data_uJy=data_uJy,
        variance_uJy2=variance_uJy2,
        target_template=target_template,
        neighbor_templates=neighbor_templates,
        target_x=pixels.cutout_x,
        target_y=pixels.cutout_y,
        config=config,
    )


def _estimate_background(
    data_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    masks: MaskSet,
    pixels,
    config: Config,
) -> BackgroundResult:
    return estimate_2d_background(
        data_uJy,
        variance_uJy2,
        masks.background_mask,
        fallback_mask=masks.fallback_background_mask,
        fallback_protection_mask=masks.target_protection_mask | masks.neighbor_footprint_mask,
        model=config.photometry.background.model,
        min_pixels=config.photometry.background.min_unmasked_pixels,
        min_plane_pixels=config.photometry.background.min_plane_pixels,
        condition_number_max=config.photometry.background.plane_condition_number_max,
        sigma_clip=config.photometry.background.sigma_clip,
        sigma_clip_iterations=config.photometry.background.sigma_clip_iterations,
        engine=config.photometry.background.engine,
        photutils_box_size=config.photometry.background.photutils_box_size,
        photutils_filter_size=config.photometry.background.photutils_filter_size,
        photutils_exclude_percentile=config.photometry.background.photutils_exclude_percentile,
        center_x=pixels.cutout_x,
        center_y=pixels.cutout_y,
    )


def _background_image(background: BackgroundResult, shape: tuple[int, int]) -> np.ndarray:
    if background.background_image_uJy is not None:
        return np.asarray(background.background_image_uJy, dtype=float)
    return np.full(shape, float(background.value_uJy_per_pixel), dtype=float)


def _fit(
    data_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    templates: list[np.ndarray],
    masks: MaskSet,
    background: BackgroundResult,
    config: Config,
) -> FitResult:
    return weighted_linear_fit(
        data_uJy,
        variance_uJy2,
        templates,
        masks.fit_mask,
        background_uncertainty_uJy_per_pixel=background.uncertainty_uJy_per_pixel,
        condition_number_max=config.photometry.fit.condition_number_max,
    )


def _failed_fit(data_uJy: np.ndarray, reason: str) -> FitResult:
    return FitResult(
        fluxes_uJy=np.asarray([], dtype=float),
        covariance=np.zeros((0, 0), dtype=float),
        model_uJy=np.zeros_like(data_uJy, dtype=float),
        residual_uJy=np.full_like(data_uJy, np.nan, dtype=float),
        chi2_reduced=float("nan"),
        condition_number=float("inf"),
        n_valid_pixels=0,
        ok=False,
        reason=reason,
        metadata={"solver_name": "not_run", "reason": reason},
    )


def _joint_fit_is_stable(
    joint_fit: FitResult,
    point_fit: FitResult,
    target_neighbor_max_corr: float,
    uncertainty_inflation: float,
    target_protection_problem: bool,
    config: Config,
) -> bool:
    if target_protection_problem or not (point_fit.ok and joint_fit.ok):
        return False
    if target_neighbor_max_corr > config.photometry.fit.target_neighbor_correlation_max:
        return False
    if uncertainty_inflation > config.photometry.fit.uncertainty_inflation_max:
        return False
    if np.isfinite(point_fit.chi2_reduced) and np.isfinite(joint_fit.chi2_reduced):
        return joint_fit.chi2_reduced <= point_fit.chi2_reduced * 1.05
    return True


def _template_flags(template: RenderedTemplate) -> list[str]:
    flags: list[str] = []
    if template.truncated:
        flags.append(FLAG_PSF_TRUNCATED)
    if any("PSF_CENTER" in warning for warning in template.warnings):
        flags.append(FLAG_PSF_COORDINATE_UNCERTAIN)
    return flags


def _mask_flags(masks: MaskSet, config: Config) -> list[str]:
    flags: list[str] = []
    if masks.science_blocker_pixel_count:
        flags.append(FLAG_SEVERE_IMAGE_FLAG)
    if masks.central_fit_mask_fraction < config.photometry.fit.min_template_fraction_unmasked:
        flags.append(FLAG_TARGET_CORE_MASKED)
    if masks.template_fraction_unmasked < config.photometry.fit.min_template_fraction_unmasked:
        flags.append(FLAG_LOW_TEMPLATE_SUPPORT)
    return flags


def _central_residual_metrics(
    fit: FitResult,
    variance_uJy2: np.ndarray,
    masks: MaskSet,
    target_x: float,
    target_y: float,
) -> dict[str, float]:
    mask = masks.central_aperture_mask & masks.fit_mask & np.isfinite(variance_uJy2) & (variance_uJy2 > 0)
    fit_ql = _fit_ql_mean_abs_2p5pix(fit, variance_uJy2, masks, target_x, target_y)
    if not mask.any():
        return {
            "central_residual_median_abs_sigma": float("nan"),
            "central_residual_peak_sigma": float("nan"),
            "central_residual_mean_sigma": float("nan"),
            "fit_ql_mean_abs_2p5pix": fit_ql,
        }
    norm = fit.residual_uJy[mask] / np.sqrt(variance_uJy2[mask])
    positive = norm[np.isfinite(norm)]
    peak = float(np.nanmax(positive)) if positive.size else float("nan")
    return {
        "central_residual_median_abs_sigma": float(np.nanmedian(np.abs(norm))),
        "central_residual_peak_sigma": peak,
        "central_residual_mean_sigma": float(np.nanmean(norm)),
        "fit_ql_mean_abs_2p5pix": fit_ql,
    }


def _fit_ql_mean_abs_2p5pix(
    fit: FitResult,
    variance_uJy2: np.ndarray,
    masks: MaskSet,
    target_x: float,
    target_y: float,
) -> float:
    yy, xx = np.indices(fit.residual_uJy.shape)
    aperture = (xx - float(target_x)) ** 2 + (yy - float(target_y)) ** 2 <= 2.5**2
    mask = aperture & masks.fit_mask & np.isfinite(variance_uJy2) & (variance_uJy2 > 0)
    if not mask.any():
        return float("nan")
    norm = fit.residual_uJy[mask] / np.sqrt(variance_uJy2[mask])
    return float(np.nanmean(np.abs(norm)))


def _invalid_central_metrics() -> dict[str, float]:
    return {
        "central_residual_median_abs_sigma": float("nan"),
        "central_residual_peak_sigma": float("nan"),
        "central_residual_mean_sigma": float("nan"),
        "fit_ql_mean_abs_2p5pix": float("nan"),
    }


def _response_weights(template: np.ndarray, variance_uJy2: np.ndarray, fit_mask: np.ndarray) -> np.ndarray:
    weights = np.zeros_like(template, dtype=float)
    good = fit_mask & np.isfinite(template) & np.isfinite(variance_uJy2) & (variance_uJy2 > 0)
    weights[good] = template[good] * template[good] / variance_uJy2[good]
    return weights


def _residual_sigma(fit: FitResult, variance_uJy2: np.ndarray) -> np.ndarray:
    residual_sigma = np.zeros_like(fit.residual_uJy, dtype=float)
    good_sigma = np.isfinite(variance_uJy2) & (variance_uJy2 > 0)
    residual_sigma[good_sigma] = fit.residual_uJy[good_sigma] / np.sqrt(variance_uJy2[good_sigma])
    return residual_sigma


def _mask_or_zeros(mask: np.ndarray | None, shape: tuple[int, int]) -> np.ndarray:
    if mask is None:
        return np.zeros(shape, dtype=float)
    return np.asarray(mask, dtype=float)


def _mask_source_map(masks: MaskSet, flags: np.ndarray, neighbor_search: NeighborSearchResult, background: BackgroundResult) -> np.ndarray:
    image = np.zeros_like(masks.fit_mask, dtype=float)
    image[masks.fit_mask] = 1.0
    image[masks.background_mask] = 2.0
    image[masks.target_protection_mask] = 3.0
    image[np.asarray(flags, dtype=np.int64) != 0] = 5.0
    if background.mask_used is not None:
        fallback_only = np.asarray(background.mask_used, dtype=bool) & ~masks.background_mask
        image[fallback_only] = 6.0
    if background.fallback_source_mask is not None:
        image[np.asarray(background.fallback_source_mask, dtype=bool)] = 7.0
    image[masks.fit_exclude_mask] = 8.0
    image[masks.invalid_variance_mask] = 9.0
    if neighbor_search.source_map is not None:
        image[np.asarray(neighbor_search.source_map) > 0] = 4.0
    return image


def _fit_summary(fit: FitResult) -> dict[str, Any]:
    return {
        "ok": fit.ok,
        "reason": fit.reason,
        "fluxes_uJy": fit.fluxes_uJy.tolist(),
        "covariance": fit.covariance.tolist(),
        "chi2_reduced": fit.chi2_reduced,
        "condition_number": fit.condition_number,
        "n_valid_pixels": fit.n_valid_pixels,
        "metadata": fit.metadata,
    }


def _metadata_value(cutout, cutout_row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in cutout_row and cutout_row[key] is not None:
            return cutout_row[key]
        if key in cutout.header_metadata and cutout.header_metadata[key] is not None:
            return cutout.header_metadata[key]
    return None


def _float_or_nan(value: Any) -> float:
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
