from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from spherex_cutoutdb.config import Config
from spherex_cutoutdb.photometry.constants import (
    FLAG_BACKGROUND_IMAGE_CLIPPED_FALLBACK,
    FLAG_BACKGROUND_POOR,
    FLAG_CENTRAL_POSITIVE_RESIDUAL_AFTER_NEGATIVE_MODEL,
    FLAG_CONTAMINATION_RISK,
    FLAG_DEBLEND_UNSTABLE,
    FLAG_TARGET_POSSIBLY_EXTENDED,
    FLAG_TARGET_PSF_MISMATCH,
    FLAG_TARGET_SPLIT_PROTECTED,
    SPHEREX_IMAGE_FLAG_BITS,
)
from spherex_cutoutdb.photometry.background import estimate_2d_background
from spherex_cutoutdb.photometry.coordinates import cutout_detector_pixel_grid, cutout_to_detector_pixels
from spherex_cutoutdb.photometry.fits_io import CutoutData, build_spatial_header, load_cutout
from spherex_cutoutdb.photometry.linear_fit import weighted_linear_fit
from spherex_cutoutdb.photometry.masks import build_photometry_masks
from spherex_cutoutdb.photometry.measure import MeasurementResult, measure_cutout
from spherex_cutoutdb.photometry.outputs import source_output_paths, validate_full_qa_outputs, validate_source_outputs, write_source_outputs
from spherex_cutoutdb.photometry.psf import render_point_template, select_psf_plane
from spherex_cutoutdb.photometry.solid_angle import ARCSEC2_TO_SR, sample_detector_map
from spherex_cutoutdb.photometry.spectral_wcs import sample_wavelength


def test_source_flag_in_target_core_excluded_from_background_not_fit():
    cfg = Config()
    shape = (9, 9)
    data = np.ones(shape)
    variance = np.ones(shape)
    yy, xx = np.indices(shape)
    template = np.exp(-0.5 * (((xx - 4) / 1.0) ** 2 + ((yy - 4) / 1.0) ** 2))
    template /= template.sum()
    flags = np.zeros(shape, dtype=np.int64)
    flags[4, 4] = SPHEREX_IMAGE_FLAG_BITS["SOURCE"]

    masks = build_photometry_masks(
        flags=flags,
        data_uJy=data,
        variance_uJy2=variance,
        target_template=template,
        neighbor_templates=[],
        target_x=4,
        target_y=4,
        config=cfg,
    )

    assert masks.fit_mask[4, 4]
    assert not masks.background_mask[4, 4]
    assert masks.flag_union_central & SPHEREX_IMAGE_FLAG_BITS["SOURCE"]


def test_severe_bad_pixel_flag_excluded_from_fit():
    cfg = Config()
    shape = (9, 9)
    data = np.ones(shape)
    variance = np.ones(shape)
    template = np.zeros(shape)
    template[4, 4] = 1.0
    flags = np.zeros(shape, dtype=np.int64)
    flags[4, 4] = SPHEREX_IMAGE_FLAG_BITS["HOT"]

    masks = build_photometry_masks(
        flags=flags,
        data_uJy=data,
        variance_uJy2=variance,
        target_template=template,
        neighbor_templates=[],
        target_x=4,
        target_y=4,
        config=cfg,
    )

    assert not masks.fit_mask[4, 4]
    assert masks.science_blocker_pixel_count == 1


def test_source_masked_2d_background_recovers_plane():
    shape = (15, 15)
    yy, xx = np.indices(shape, dtype=float)
    data = 10.0 + 0.2 * (xx - 7.0) - 0.1 * (yy - 7.0)
    data[7, 7] += 100.0
    variance = np.ones(shape)
    mask = np.ones(shape, dtype=bool)
    mask[(xx - 7.0) ** 2 + (yy - 7.0) ** 2 <= 4.0] = False

    result = estimate_2d_background(
        data,
        variance,
        mask,
        model="plane",
        min_pixels=10,
        min_plane_pixels=12,
        center_x=7.0,
        center_y=7.0,
    )

    assert result.ok
    assert result.model == "plane"
    assert result.engine in {"photutils_plane", "numpy_plane"}
    assert result.b0_uJy_per_pixel == pytest.approx(10.0, abs=1.0e-8)
    assert result.bx_uJy_per_pixel == pytest.approx(0.2, abs=1.0e-8)
    assert result.by_uJy_per_pixel == pytest.approx(-0.1, abs=1.0e-8)
    assert np.allclose(result.background_image_uJy, 10.0 + 0.2 * (xx - 7.0) - 0.1 * (yy - 7.0))


def test_photutils_background_engine_records_provenance():
    pytest.importorskip("photutils")
    shape = (32, 32)
    yy, xx = np.indices(shape, dtype=float)
    data = 5.0 + 0.03 * (xx - 15.5) + 0.02 * (yy - 15.5)
    variance = np.ones(shape)
    mask = np.ones(shape, dtype=bool)

    result = estimate_2d_background(
        data,
        variance,
        mask,
        engine="photutils",
        photutils_box_size=16,
        center_x=15.5,
        center_y=15.5,
    )

    assert result.ok
    assert result.model == "plane"
    assert result.engine == "photutils_plane"
    assert result.photutils_used
    assert result.photutils_version
    assert result.photutils_box_size == 16


def test_full_sip_wcs_target_position_preserved():
    header = fits.Header()
    header["NAXIS"] = 2
    header["NAXIS1"] = 100
    header["NAXIS2"] = 100
    header["CTYPE1"] = "RA---TAN-SIP"
    header["CTYPE2"] = "DEC--TAN-SIP"
    header["CRVAL1"] = 10.0
    header["CRVAL2"] = 20.0
    header["CRPIX1"] = 50.0
    header["CRPIX2"] = 50.0
    header["CD1_1"] = -2.7e-4
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = 2.7e-4
    header["A_ORDER"] = 2
    header["B_ORDER"] = 2
    header["A_0_2"] = 1.0e-5
    header["B_2_0"] = -2.0e-5
    full_header = header.copy()
    header["CRPIX1A"] = -220.0
    header["CTYPE1W"] = "WAVE-TAB"
    clean, warnings = build_spatial_header(header)
    full = WCS(full_header, naxis=2)
    built = WCS(clean, naxis=2)
    ra, dec = full.pixel_to_world_values(42.3, 47.8)

    assert warnings
    assert np.allclose(built.world_to_pixel_values(ra, dec), full.world_to_pixel_values(ra, dec), atol=1.0e-5)


def test_official_cutout_to_detector_coordinate_formula():
    cutout = _cutout_with_headers(crpix1a=-220.0, crpix2a=-310.0)

    x_orig, y_orig, method, warnings = cutout_to_detector_pixels(cutout, 2.42, 3.5)
    x_grid, y_grid, grid_method, grid_warnings = cutout_detector_pixel_grid(cutout)

    assert method == "crpix_a_original_detector"
    assert warnings == []
    assert x_orig == pytest.approx(223.42)
    assert y_orig == pytest.approx(314.5)
    assert grid_method == "crpix_a_original_detector"
    assert grid_warnings == []
    assert x_grid[3, 2] == pytest.approx(223.0)
    assert y_grid[3, 2] == pytest.approx(314.0)


def test_psf_header_center_variants_select_nearest_plane():
    cube = np.stack([np.full((5, 5), 1.0), np.eye(5)], axis=0)
    header = fits.Header()
    header["XCTR_1"] = 10.0
    header["YCTR_1"] = 10.0
    header["XCTR02"] = 100.0
    header["YCTR02"] = 100.0
    cutout = _cutout_with_headers(psf=cube, psf_header=header)

    first = select_psf_plane(cutout, 11.0, 9.0)
    second = select_psf_plane(cutout, 90.0, 95.0)

    assert first.index == 0
    assert second.index == 1
    assert np.allclose(first.plane, cube[0])
    assert np.allclose(second.plane, cube[1])


def test_missing_psf_centers_do_not_silently_use_plane_zero():
    cube = np.stack([np.full((5, 5), 1.0), np.eye(5)], axis=0)
    cutout = _cutout_with_headers(psf=cube, psf_header=fits.Header())

    with pytest.raises(ValueError, match="missing plane center"):
        select_psf_plane(cutout, 10.0, 10.0)


def test_oversampled_psf_integration_normalizes_and_shifts_centroid():
    cfg = Config()
    plane = _oversampled_gaussian(size=101, oversampling=10, sigma_native=1.1)
    psf_header = fits.Header()
    psf_header["OVERSAMP"] = 10
    cutout = _cutout_with_headers(shape=(31, 31), psf=plane, psf_header=psf_header)

    rendered = render_point_template(
        cutout,
        15.3,
        14.7,
        15.3,
        14.7,
        radius_pixels=cfg.photometry.psf_template_radius_pixels,
        oversampling_factor=cfg.photometry.psf.oversampling_factor,
    )
    yy, xx = np.indices(rendered.image.shape)

    assert rendered.image.sum() == pytest.approx(1.0, abs=1.0e-8)
    assert float(np.sum(rendered.image * xx)) == pytest.approx(15.3, abs=0.08)
    assert float(np.sum(rendered.image * yy)) == pytest.approx(14.7, abs=0.08)


def test_sapm_detector_grid_sampling():
    data = np.arange(10000, dtype=float).reshape(100, 100)
    x_grid = np.array([[10.2, 11.4], [12.6, 13.1]])
    y_grid = np.array([[5.2, 6.4], [7.6, 8.1]])

    sampled = sample_detector_map(data, x_grid, y_grid)

    assert np.allclose(sampled, data[np.rint(y_grid).astype(int), np.rint(x_grid).astype(int)])


def test_cwave_cband_detector_grid_sampling(tmp_path: Path):
    path = tmp_path / "spectral_wcs.fits"
    yy, xx = np.indices((100, 100))
    fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(1.0 + xx.astype("f4") * 0.01, name="CWAVE"),
            fits.ImageHDU(0.1 + yy.astype("f4") * 0.001, name="CBAND"),
        ]
    ).writeto(path)
    x_grid = np.array([[10.0, 11.0], [12.0, 13.0]])
    y_grid = np.array([[20.0, 21.0], [22.0, 23.0]])
    weights = np.array([[0.0, 0.0], [0.0, 1.0]])

    sample = sample_wavelength(path, x_grid, y_grid, response_weights=weights)

    assert sample.wavelength_um == pytest.approx(1.13)
    assert sample.bandwidth_um == pytest.approx(0.123)
    assert sample.method == "spectral_wcs_psf_weighted"


def test_weighted_linear_fit_preserves_negative_flux_and_reports_diagnostics():
    data = np.array([[0.0, 0.0], [0.0, -2.0]])
    variance = np.ones_like(data)
    template = np.array([[0.0, 0.0], [0.0, 1.0]])
    fit = weighted_linear_fit(data, variance, [template], np.ones_like(data, dtype=bool))

    assert fit.target_flux_uJy == pytest.approx(-2.0)
    assert fit.metadata["matched_filter_numerator"] < 0
    assert fit.metadata["matched_filter_denominator"] > 0
    assert fit.covariance[0, 0] == pytest.approx(1.0)


def test_background_uncertainty_increases_flux_uncertainty():
    data = np.array([[0.0, 0.0], [0.0, 2.0]])
    variance = np.ones_like(data)
    template = np.array([[0.0, 0.0], [0.0, 1.0]])
    fit_image_only = weighted_linear_fit(data, variance, [template], np.ones_like(data, dtype=bool))
    fit_with_background = weighted_linear_fit(
        data,
        variance,
        [template],
        np.ones_like(data, dtype=bool),
        background_uncertainty_uJy_per_pixel=0.5,
    )

    assert fit_with_background.target_flux_err_uJy > fit_image_only.target_flux_err_uJy


def test_measurement_records_negative_central_residual_qa_flag(tmp_path: Path):
    cfg = Config()
    cutout_path = tmp_path / "bad_negative.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=0.0, central_spike_uJy=60.0, negative_ring_uJy=-1000.0)
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["selected_flux_uJy"] < 0
    assert FLAG_CENTRAL_POSITIVE_RESIDUAL_AFTER_NEGATIVE_MODEL in result.row["photometry_flags"]
    assert not result.row["science_recommended"]


def test_slightly_resolved_target_is_not_split_into_neighbors(tmp_path: Path):
    cfg = Config()
    cfg.photometry.deblending.enabled = True
    cfg.photometry.deblending.residual_snr_threshold = 1.0
    cfg.photometry.deblending.material_overlap_threshold = 0.0
    cutout_path = tmp_path / "resolved_target.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=250.0, target_sigma_native=1.8)
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["n_neighbors"] == 0
    assert FLAG_TARGET_SPLIT_PROTECTED in result.row["photometry_flags"]
    assert FLAG_TARGET_POSSIBLY_EXTENDED in result.row["photometry_flags"]
    assert FLAG_TARGET_PSF_MISMATCH in result.row["photometry_flags"]
    assert not result.row["science_recommended"]


def test_unstable_deblend_keeps_point_measurement_and_flags(tmp_path: Path):
    cfg = Config()
    cfg.photometry.deblending.enabled = True
    cfg.photometry.deblending.residual_snr_threshold = 5.0
    cfg.photometry.deblending.material_overlap_threshold = 0.0
    cfg.photometry.fit.target_neighbor_correlation_max = 0.01
    cutout_path = tmp_path / "neighbor.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=30.0, neighbor_flux_uJy=100.0, neighbor_xy=(16, 12))
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["deblend_status"] == "needed_unstable"
    assert FLAG_DEBLEND_UNSTABLE in result.row["photometry_flags"]
    assert FLAG_CONTAMINATION_RISK in result.row["photometry_flags"]
    assert not result.row["science_recommended"]


def test_stable_two_source_joint_fit_improves_residuals_and_records_covariance(tmp_path: Path):
    cfg = Config()
    cfg.photometry.deblending.enabled = True
    cfg.photometry.deblending.residual_snr_threshold = 5.0
    cfg.photometry.deblending.material_overlap_threshold = 0.0
    cfg.photometry.fit.target_neighbor_correlation_max = 0.99
    cfg.photometry.detection_snr_threshold = 3.0
    cutout_path = tmp_path / "stable_neighbor.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=30.0, neighbor_flux_uJy=100.0, neighbor_xy=(16, 12))
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["deblend_status"] == "joint_stable"
    assert result.row["n_neighbors"] == 1
    assert result.row["science_recommended"]
    assert FLAG_TARGET_PSF_MISMATCH not in result.row["photometry_flags"]
    assert result.provenance["neighbor_search"]["candidates"][0]["area_pixels"] == 1
    assert result.provenance["joint_fit"]["chi2_reduced"] < result.provenance["point_fit"]["chi2_reduced"]
    assert len(result.provenance["joint_fit"]["covariance"]) >= 2


def test_official_fit_quality_metric_matches_manual_calculation(tmp_path: Path):
    cfg = Config()
    cutout_path = tmp_path / "metric.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=40.0, central_spike_uJy=4.0)
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    yy, xx = np.indices(result.qa_arrays["residual_sigma"].shape)
    radius = (xx - result.row["x_cutout"]) ** 2 + (yy - result.row["y_cutout"]) ** 2 <= 2.5**2
    mask = radius & result.qa_arrays["fit_mask"].astype(bool)
    manual = float(np.nanmean(np.abs(result.qa_arrays["residual_sigma"][mask])))
    assert result.row["fit_ql_mean_abs_2p5pix"] == pytest.approx(manual)


def test_full_qa_plot_and_new_schema_fields_written(tmp_path: Path):
    cfg = Config()
    cfg.photometry.output_root = tmp_path / "results"
    cutout_path = tmp_path / "positive.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=40.0)
    spectral_path, sapm_path = _write_calibrations(tmp_path)
    source = {"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0}
    result = measure_cutout(
        cutout_path=cutout_path,
        source=source,
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["selected_flux_uJy"] > 0
    assert result.row["detection_status"] == "detected"
    assert result.row["science_recommended"]
    assert result.row["mjd"] == pytest.approx(60000.0)
    assert result.row["mjd_avg"] == pytest.approx(60000.5)
    assert "background_2d" in result.qa_arrays
    assert result.row["background_engine"] in {"photutils_plane", "numpy_plane"}
    assert result.row["background_photutils_used"] in {True, False}
    assert "point_residual_sigma" in result.qa_arrays
    assert "joint_residual_sigma" in result.qa_arrays
    assert "mask_source_map" in result.qa_arrays
    second = MeasurementResult(
        row={**result.row, "measurement_id": "m2"},
        provenance=deepcopy(result.provenance),
        qa_arrays={key: np.array(value, copy=True) for key, value in result.qa_arrays.items()},
    )
    cfg.photometry.qa.full_plot_workers = 2
    qa_events = []
    paths = write_source_outputs(
        config=cfg,
        source=source,
        measurements=[result, second],
        failures=[],
        qa_level="full",
        progress_callback=qa_events.append,
    )

    qa_plot = source_output_paths(cfg, source)["qa_dir"] / "m_qa.png"
    qa_plot_2 = source_output_paths(cfg, source)["qa_dir"] / "m2_qa.png"
    csv_text = paths["csv"].read_text(encoding="utf-8")
    assert qa_plot.exists() and qa_plot.stat().st_size > 0
    assert qa_plot_2.exists() and qa_plot_2.stat().st_size > 0
    assert validate_full_qa_outputs(config=cfg, source=source, measurements=[result, second])
    assert "measurement_status" in csv_text
    assert "science_reject_reason" in csv_text
    assert "photometry_native_v6" in csv_text
    assert "photometry_psf_wls_v6_nozodi_failclosed_bkgfallback" in csv_text
    assert validate_source_outputs(paths, config=cfg, source=source, measurements=[result, second])
    paths["csv"].write_text(csv_text + "\n", encoding="utf-8")
    assert not validate_source_outputs(paths, config=cfg, source=source, measurements=[result, second])
    assert qa_events[0] == {"phase": "qa_start", "qa_written": 0, "qa_total": 2}
    assert qa_events[-1] == {"phase": "qa_plot", "qa_written": 2, "qa_total": 2}


def test_zodi_extension_is_ignored_by_measurement(tmp_path: Path):
    cfg = Config()
    cfg.photometry.deblending.enabled = False
    spectral_path, sapm_path = _write_calibrations(tmp_path)
    source = {"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0}
    base_path = tmp_path / "zodi_zero.fits"
    zodi_path = tmp_path / "zodi_bright.fits"
    _write_synthetic_cutout(base_path, flux_uJy=45.0, zodi_flux_uJy=0.0)
    _write_synthetic_cutout(zodi_path, flux_uJy=45.0, zodi_flux_uJy=500.0)

    base = measure_cutout(
        cutout_path=base_path,
        source=source,
        cutout_row={"cutout_key": "base", "detector_id": 3, "observation_id": "obs", "sha256": "base"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m_base",
        work_item_id="w_base",
    )
    with_zodi = measure_cutout(
        cutout_path=zodi_path,
        source=source,
        cutout_row={"cutout_key": "zodi", "detector_id": 3, "observation_id": "obs", "sha256": "zodi"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m_zodi",
        work_item_id="w_zodi",
    )

    assert with_zodi.row["input_image_extension"] == "IMAGE"
    assert with_zodi.row["zodi_used"] is False
    assert with_zodi.provenance["zodi_used"] is False
    assert with_zodi.row["selected_flux_uJy"] == pytest.approx(base.row["selected_flux_uJy"], abs=1.0e-8)


def test_core_bad_flags_report_weighted_fractions_and_reject_science(tmp_path: Path):
    cfg = Config()
    cutout_path = tmp_path / "hot_core.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=60.0, core_bad_flag="HOT")
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["flag_hard_bad_fraction_core"] > 0
    assert result.row["flag_science_reject_fraction_core"] > 0
    assert result.row["psf_weighted_hard_bad_fraction"] > 0
    assert not result.row["science_recommended"]


def test_invalid_variance_pixels_are_masked_and_reported(tmp_path: Path):
    cfg = Config()
    cutout_path = tmp_path / "invalid_variance.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=60.0, variance_mode="invalid_core")
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["invalid_variance_fraction_core"] > 0
    assert result.row["invalid_variance_fraction_fit"] > 0
    assert result.row["psf_weighted_invalid_variance_fraction"] > 0
    assert not result.qa_arrays["fit_mask"][12, 12]


def test_source_dominated_nominal_background_uses_image_clipped_fallback(tmp_path: Path):
    cfg = Config()
    cutout_path = tmp_path / "source_flags_all.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=45.0, source_flags_all=True)
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["background_ok"]
    assert result.row["background_fallback_used"]
    assert result.row["background_method"] in {"image_clipped_2d_fallback", "image_clipped_plane_fallback"}
    assert FLAG_BACKGROUND_IMAGE_CLIPPED_FALLBACK in result.row["background_flags"]
    assert np.isfinite(result.row["selected_flux_uJy"])


def test_background_failure_does_not_emit_science_flux(tmp_path: Path):
    cfg = Config()
    cutout_path = tmp_path / "background_failed.fits"
    _write_synthetic_cutout(cutout_path, flux_uJy=45.0, variance_mode="all_zero")
    spectral_path, sapm_path = _write_calibrations(tmp_path)

    result = measure_cutout(
        cutout_path=cutout_path,
        source={"source_id": "src", "source_name": "src", "ra_deg": 10.0, "dec_deg": 20.0},
        cutout_row={"cutout_key": "cut", "detector_id": 3, "observation_id": "obs", "sha256": "sha"},
        calibration_resolution=_calibration_resolution(spectral_path, sapm_path),
        config=cfg,
        measurement_id="m",
        work_item_id="w",
    )

    assert result.row["measurement_status"] == "failed_background"
    assert not result.row["measurement_ok"]
    assert not result.row["science_recommended"]
    assert result.row["science_reject_reason"] == FLAG_BACKGROUND_POOR
    assert not np.isfinite(result.row["selected_flux_uJy"])
    assert result.row["background_method"] == "failed_background"


def _cutout_with_headers(
    *,
    shape: tuple[int, int] = (9, 9),
    crpix1a: float = 1.0,
    crpix2a: float = 1.0,
    psf: np.ndarray | None = None,
    psf_header: fits.Header | None = None,
) -> CutoutData:
    header = fits.Header()
    header["CRPIX1A"] = crpix1a
    header["CRPIX2A"] = crpix2a
    return CutoutData(
        path=Path("dummy.fits"),
        image_mjy_sr=np.zeros(shape),
        variance_mjy_sr2=np.ones(shape),
        flags=np.zeros(shape, dtype=np.int64),
        psf=np.ones((5, 5)) if psf is None else psf,
        image_header=header,
        primary_header=fits.Header(),
        psf_header=fits.Header() if psf_header is None else psf_header,
        spatial_wcs=WCS(naxis=2),
        spatial_wcs_warnings=[],
        header_metadata={},
    )


def _oversampled_gaussian(*, size: int, oversampling: int, sigma_native: float) -> np.ndarray:
    yy, xx = np.indices((size, size), dtype=float)
    center = (size - 1) / 2.0
    sigma = sigma_native * oversampling
    plane = np.exp(-0.5 * (((xx - center) / sigma) ** 2 + ((yy - center) / sigma) ** 2))
    return plane / plane.sum()


def _write_synthetic_cutout(
    path: Path,
    *,
    flux_uJy: float,
    central_spike_uJy: float = 0.0,
    negative_ring_uJy: float = 0.0,
    neighbor_flux_uJy: float = 0.0,
    neighbor_xy: tuple[float, float] = (14, 12),
    target_sigma_native: float = 1.1,
    zodi_flux_uJy: float = 0.0,
    source_flags_all: bool = False,
    core_bad_flag: str | None = None,
    variance_mode: str | None = None,
) -> None:
    shape = (25, 25)
    center = (12, 12)
    solid_factor = ARCSEC2_TO_SR * 1e12
    yy, xx = np.indices(shape)
    template = np.exp(-0.5 * (((xx - center[1]) / target_sigma_native) ** 2 + ((yy - center[0]) / target_sigma_native) ** 2))
    template /= template.sum()
    image_uJy = flux_uJy * template
    if central_spike_uJy:
        image_uJy[center] += central_spike_uJy
    if negative_ring_uJy:
        ring = ((xx - center[1]) ** 2 + (yy - center[0]) ** 2 >= 3) & ((xx - center[1]) ** 2 + (yy - center[0]) ** 2 <= 9)
        image_uJy[ring] += negative_ring_uJy / max(int(np.count_nonzero(ring)), 1)
    if neighbor_flux_uJy:
        nx, ny = neighbor_xy
        neighbor = np.exp(-0.5 * (((xx - nx) / 1.1) ** 2 + ((yy - ny) / 1.1) ** 2))
        neighbor /= neighbor.sum()
        image_uJy += neighbor_flux_uJy * neighbor
    image = np.full(shape, 1.0, dtype="f4") + (image_uJy / solid_factor).astype("f4")
    variance = np.full(shape, (2.0 / solid_factor) ** 2, dtype="f4")
    flags = np.zeros(shape, dtype="i4")
    if source_flags_all:
        flags[:, :] |= SPHEREX_IMAGE_FLAG_BITS["SOURCE"]
    if core_bad_flag is not None:
        bit = SPHEREX_IMAGE_FLAG_BITS[str(core_bad_flag).upper()]
        flags[center] |= bit
    if variance_mode == "invalid_core":
        core = (np.abs(xx - center[1]) <= 1) & (np.abs(yy - center[0]) <= 1)
        variance[core] = 0.0
        variance[center[0] - 1, center[1]] = -1.0
        variance[center[0], center[1] - 1] = np.nan
    elif variance_mode == "all_zero":
        variance[:, :] = 0.0
    zodi = (zodi_flux_uJy * template / solid_factor).astype("f4")
    header = fits.Header()
    header["NAXIS"] = 2
    header["NAXIS1"] = shape[1]
    header["NAXIS2"] = shape[0]
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRVAL1"] = 10.0
    header["CRVAL2"] = 20.0
    header["CRPIX1"] = center[1] + 1
    header["CRPIX2"] = center[0] + 1
    header["CRPIX1A"] = 1.0
    header["CRPIX2A"] = 1.0
    header["CD1_1"] = -0.0002777778
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = 0.0002777778
    header["MJD"] = 60000.0
    header["MJD-AVG"] = 60000.5
    psf = _oversampled_gaussian(size=101, oversampling=10, sigma_native=1.1).astype("f4")
    psf_header = fits.Header()
    psf_header["OVERSAMP"] = 10
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(image, header=header, name="IMAGE"),
            fits.ImageHDU(flags, name="FLAGS"),
            fits.ImageHDU(variance, name="VARIANCE"),
            fits.ImageHDU(zodi, name="ZODI"),
            fits.ImageHDU(psf, header=psf_header, name="PSF"),
            fits.BinTableHDU.from_columns([fits.Column(name="WAVELENGTH", array=np.array([2.0], dtype="f4"), format="E")], name="WCS-WAVE"),
        ]
    ).writeto(path, overwrite=True)
    # Ensure the loader path itself sees the target at the intended sky position.
    loaded = load_cutout(path)
    assert np.allclose(loaded.spatial_wcs.world_to_pixel_values(10.0, 20.0), (12.0, 12.0), atol=1.0e-5)


def _write_calibrations(tmp_path: Path) -> tuple[Path, Path]:
    spectral_path = tmp_path / "spectral.fits"
    sapm_path = tmp_path / "sapm.fits"
    shape = (25, 25)
    fits.HDUList(
        [
            fits.PrimaryHDU(),
            fits.ImageHDU(np.full(shape, 2.0, dtype="f4"), name="CWAVE"),
            fits.ImageHDU(np.full(shape, 0.1, dtype="f4"), name="CBAND"),
        ]
    ).writeto(spectral_path)
    fits.HDUList([fits.PrimaryHDU(np.ones(shape, dtype="f4"))]).writeto(sapm_path)
    return spectral_path, sapm_path


def _calibration_resolution(spectral_path: Path, sapm_path: Path):
    products = {
        "spectral_wcs": {"calibration_id": "spec", "path": str(spectral_path)},
        "solid_angle_pixel_map": {"calibration_id": "sapm", "path": str(sapm_path)},
    }
    return SimpleNamespace(
        products=products,
        exact_match=True,
        path_for=lambda _config, product: Path(products[product]["path"]),
    )
