"""Detection status and conservative science selection."""

from __future__ import annotations

import math

from spherex_cutoutdb.config import Config

from .constants import (
    FLAG_BACKGROUND_2D_FALLBACK_CONSTANT,
    FLAG_BACKGROUND_2D_UNSTABLE,
    FLAG_BACKGROUND_POOR,
    FLAG_CALIBRATION_VERSION_MISMATCH,
    FLAG_CENTRAL_POSITIVE_RESIDUAL_AFTER_NEGATIVE_MODEL,
    FLAG_CONTAMINATION_RISK,
    FLAG_DEBLEND_UNSTABLE,
    FLAG_DETECTOR_COORD_FALLBACK,
    FLAG_FIT_ERROR,
    FLAG_FIT_ILL_CONDITIONED,
    FLAG_LOW_TEMPLATE_SUPPORT,
    FLAG_NEGATIVE_FORCED_FLUX,
    FLAG_NON_DETECTION,
    FLAG_PSF_COORDINATE_UNCERTAIN,
    FLAG_PSF_RENDERING_INVALID,
    FLAG_PSF_TRUNCATED,
    FLAG_SEVERE_IMAGE_FLAG,
    FLAG_TARGET_CORE_MASKED,
    FLAG_TARGET_POSSIBLY_EXTENDED,
    FLAG_TARGET_PSF_MISMATCH,
    FLAG_TARGET_SPLIT_PROTECTED,
)


def detection_status(flux_uJy: float, flux_err_uJy: float, config: Config) -> tuple[str, list[str], float]:
    if not math.isfinite(flux_uJy) or not math.isfinite(flux_err_uJy) or flux_err_uJy <= 0:
        return "invalid_fit", [FLAG_FIT_ERROR], float("nan")
    snr = flux_uJy / flux_err_uJy
    flags: list[str] = []
    if flux_uJy < 0:
        flags.append(FLAG_NEGATIVE_FORCED_FLUX)
    if abs(snr) < config.photometry.detection_snr_threshold:
        flags.append(FLAG_NON_DETECTION)
        return ("negative_flux" if flux_uJy < 0 else "non_detection"), flags, snr
    if flux_uJy < 0:
        return "negative_flux", flags, snr
    return "detected", flags, snr


def science_recommended(
    *,
    detection: str,
    flags: list[str],
    calibration_exact_match: bool,
    calibration_ok_for_science: bool | None = None,
    fit_ok: bool,
    background_ok: bool,
) -> tuple[bool, list[str], str | None, str]:
    out = list(dict.fromkeys(flags))
    calibration_ok = calibration_exact_match if calibration_ok_for_science is None else calibration_ok_for_science
    if not calibration_ok and FLAG_CALIBRATION_VERSION_MISMATCH not in out:
        out.append(FLAG_CALIBRATION_VERSION_MISMATCH)
    if not fit_ok and FLAG_FIT_ILL_CONDITIONED not in out:
        out.append(FLAG_FIT_ILL_CONDITIONED)
    if not background_ok and FLAG_BACKGROUND_POOR not in out:
        out.append(FLAG_BACKGROUND_POOR)
    blockers = {
        FLAG_CALIBRATION_VERSION_MISMATCH,
        FLAG_CENTRAL_POSITIVE_RESIDUAL_AFTER_NEGATIVE_MODEL,
        FLAG_CONTAMINATION_RISK,
        FLAG_DEBLEND_UNSTABLE,
        FLAG_DETECTOR_COORD_FALLBACK,
        FLAG_FIT_ERROR,
        FLAG_FIT_ILL_CONDITIONED,
        FLAG_LOW_TEMPLATE_SUPPORT,
        FLAG_BACKGROUND_2D_FALLBACK_CONSTANT,
        FLAG_BACKGROUND_2D_UNSTABLE,
        FLAG_BACKGROUND_POOR,
        FLAG_SEVERE_IMAGE_FLAG,
        FLAG_TARGET_CORE_MASKED,
        FLAG_TARGET_POSSIBLY_EXTENDED,
        FLAG_TARGET_PSF_MISMATCH,
        FLAG_TARGET_SPLIT_PROTECTED,
        FLAG_PSF_COORDINATE_UNCERTAIN,
        FLAG_PSF_RENDERING_INVALID,
        FLAG_PSF_TRUNCATED,
        FLAG_NEGATIVE_FORCED_FLUX,
        FLAG_NON_DETECTION,
    }
    active_blockers = [flag for flag in out if flag in blockers]
    recommended = detection == "detected" and not active_blockers
    if recommended:
        return True, out, None, "A"
    if detection in {"negative_flux", "non_detection"} and not [flag for flag in active_blockers if flag not in {FLAG_NEGATIVE_FORCED_FLUX, FLAG_NON_DETECTION}]:
        return False, out, detection, "non_detection"
    reason = ";".join(active_blockers) if active_blockers else detection
    return False, out, reason, "reject"
