"""Variance-weighted linear fits for forced photometry."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class FitResult:
    fluxes_uJy: np.ndarray
    covariance: np.ndarray
    model_uJy: np.ndarray
    residual_uJy: np.ndarray
    chi2_reduced: float
    condition_number: float
    n_valid_pixels: int
    ok: bool
    reason: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def target_flux_uJy(self) -> float:
        return float(self.fluxes_uJy[0]) if self.fluxes_uJy.size else float("nan")

    @property
    def target_flux_err_uJy(self) -> float:
        if self.covariance.size == 0:
            return float("inf")
        value = float(self.covariance[0, 0])
        return value**0.5 if value >= 0 else float("inf")


def weighted_linear_fit(
    data_uJy: np.ndarray,
    variance_uJy2: np.ndarray,
    templates: list[np.ndarray],
    fit_mask: np.ndarray,
    *,
    background_uncertainty_uJy_per_pixel: float = 0.0,
    condition_number_max: float = 1.0e8,
) -> FitResult:
    if not templates:
        raise ValueError("at least one template is required")
    mask = fit_mask & np.isfinite(data_uJy) & np.isfinite(variance_uJy2) & (variance_uJy2 > 0)
    for template in templates:
        mask &= np.isfinite(template)
    n_pix = int(np.count_nonzero(mask))
    n_comp = len(templates)
    if n_pix <= n_comp:
        return _failed(data_uJy, n_pix, "insufficient valid pixels")
    y = data_uJy[mask].reshape(-1, 1)
    a = np.vstack([template[mask] for template in templates]).T
    w = 1.0 / variance_uJy2[mask]
    aw = a * w[:, None] ** 0.5
    yw = y[:, 0] * w**0.5
    normal = aw.T @ aw
    rhs = a.T @ (w * y[:, 0])
    try:
        cond = float(np.linalg.cond(normal))
        cov = np.linalg.inv(normal)
    except np.linalg.LinAlgError as exc:
        try:
            cond = float(np.linalg.cond(normal))
            cov = np.linalg.pinv(normal)
        except np.linalg.LinAlgError:
            return _failed(data_uJy, n_pix, f"linear fit failed: {exc}")
    fluxes = cov @ rhs
    image_cov = np.asarray(cov, dtype=float)
    background_cov = np.zeros_like(image_cov)
    if background_uncertainty_uJy_per_pixel > 0 and np.isfinite(background_uncertainty_uJy_per_pixel):
        coupling = cov @ (a.T @ w)
        background_cov = float(background_uncertainty_uJy_per_pixel) ** 2 * np.outer(coupling, coupling)
        cov = image_cov + background_cov
    model = np.zeros_like(data_uJy, dtype=float)
    for flux, template in zip(fluxes, templates, strict=True):
        model += float(flux) * template
    residual = data_uJy - model
    chi2 = float(np.sum((residual[mask] ** 2) / variance_uJy2[mask]))
    dof = max(n_pix - n_comp, 1)
    return FitResult(
        fluxes_uJy=np.asarray(fluxes, dtype=float),
        covariance=np.asarray(cov, dtype=float),
        model_uJy=model,
        residual_uJy=residual,
        chi2_reduced=chi2 / dof,
        condition_number=cond,
        n_valid_pixels=n_pix,
        ok=np.isfinite(cond) and cond < float(condition_number_max),
        reason=None if np.isfinite(cond) and cond < float(condition_number_max) else "ill-conditioned fit",
        metadata={
            "solver_name": "weighted_linear_least_squares",
            "dof": dof,
            "matched_filter_numerator": float(rhs[0]) if rhs.size else float("nan"),
            "matched_filter_denominator": float(normal[0, 0]) if normal.size else float("nan"),
            "normal_matrix": np.asarray(normal, dtype=float).tolist(),
            "rhs": np.asarray(rhs, dtype=float).tolist(),
            "image_covariance": image_cov.tolist(),
            "background_covariance": background_cov.tolist(),
            "target_neighbor_correlations": _target_neighbor_correlations(cov),
            "target_neighbor_max_corr": _target_neighbor_max_corr(cov),
            "condition_number_max": float(condition_number_max),
        },
    )


def _failed(data_uJy: np.ndarray, n_pix: int, reason: str) -> FitResult:
    return FitResult(
        fluxes_uJy=np.asarray([], dtype=float),
        covariance=np.zeros((0, 0), dtype=float),
        model_uJy=np.zeros_like(data_uJy, dtype=float),
        residual_uJy=np.asarray(data_uJy, dtype=float),
        chi2_reduced=float("nan"),
        condition_number=float("inf"),
        n_valid_pixels=n_pix,
        ok=False,
        reason=reason,
    )


def _target_neighbor_correlations(cov: np.ndarray) -> list[float]:
    if cov.shape[0] <= 1:
        return []
    out: list[float] = []
    for idx in range(1, cov.shape[0]):
        denom = float(np.sqrt(max(cov[0, 0], 0.0) * max(cov[idx, idx], 0.0)))
        out.append(float(cov[0, idx] / denom) if denom > 0 else float("nan"))
    return out


def _target_neighbor_max_corr(cov: np.ndarray) -> float:
    values = [abs(value) for value in _target_neighbor_correlations(cov) if np.isfinite(value)]
    return float(max(values)) if values else 0.0
