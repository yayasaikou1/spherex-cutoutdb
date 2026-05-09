"""Durable photometry CSV, plot, QA, and provenance outputs."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from spherex_cutoutdb.config import Config
from spherex_cutoutdb.filenames import safe_slug


_PLOT_LOCK = threading.Lock()
_QA_POOL_LOCK = threading.Lock()
_QA_POOL: ProcessPoolExecutor | None = None
_QA_POOL_WORKERS: int | None = None


def source_output_paths(config: Config, source: dict[str, Any]) -> dict[str, Path]:
    base = safe_slug(source.get("source_name") or source["source_id"])
    root = config.photometry.output_root
    return {
        "csv": root / "spectra" / f"{base}.csv",
        "sed": root / "plots" / f"{base}_sed.png",
        "qa": root / "qa" / base / f"{base}_qa_summary.png",
        "provenance": root / "provenance" / f"{base}_provenance.json",
        "index": root / "provenance" / f"{base}_measurement_index.json",
        "manifest": root / "provenance" / f"{base}_output_manifest.json",
        "qa_dir": root / "qa" / base / "measurements",
    }


def write_source_outputs(
    *,
    config: Config,
    source: dict[str, Any],
    measurements: list,
    failures: list[dict[str, Any]],
    qa_level: str | None = None,
    progress_callback=None,
    write_full_qa: bool = True,
) -> dict[str, Path]:
    paths = source_output_paths(config, source)
    for key, path in paths.items():
        if key == "qa_dir":
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
    rows = [measurement.row for measurement in measurements]
    df = pd.DataFrame(rows)
    if not df.empty and "wavelength_um" in df.columns:
        df = df.sort_values(
            ["wavelength_um", "detector_id", "observation_id", "measurement_id"],
            na_position="last",
        )
    _atomic_text(paths["csv"], df.to_csv(index=False))
    with _PLOT_LOCK:
        _write_sed_plot(paths["sed"], source, df)
        _write_qa_summary(paths["qa"], source, df)
    measurement_index = _measurement_index(df, paths, config)
    provenance = {
        "source": source,
        "output_schema_version": config.photometry.output_schema_version,
        "photometry_code_version": config.photometry.code_version,
        "measurements": [measurement.provenance for measurement in measurements],
        "failures": failures,
        "measurement_index_path": _rel_or_str(paths["index"], config),
    }
    _atomic_text(paths["index"], json.dumps(measurement_index, indent=2, sort_keys=True, default=str))
    _atomic_text(paths["provenance"], json.dumps(provenance, indent=2, sort_keys=True, default=str))
    manifest = _build_output_manifest(config, source, measurements, paths)
    _atomic_text(paths["manifest"], json.dumps(manifest, indent=2, sort_keys=True, default=str))
    if write_full_qa and (qa_level or config.photometry.qa_level) == "full":
        write_full_measurement_qa_outputs(
            config=config,
            source=source,
            measurements=measurements,
            progress_callback=progress_callback,
        )
    return paths


def write_full_measurement_qa_outputs(
    *,
    config: Config,
    source: dict[str, Any],
    measurements: list,
    progress_callback=None,
) -> list[Path]:
    written = write_full_measurement_qa_batch(
        config=config,
        source_measurements=[(source, measurements)],
        progress_callback=progress_callback,
    )
    return written.get(str(source.get("source_id")), [])


def write_full_measurement_qa_batch(
    *,
    config: Config,
    source_measurements: list[tuple[dict[str, Any], list]],
    progress_callback=None,
) -> dict[str, list[Path]]:
    """Write full per-measurement QA PNGs for many sources with one worker pool."""
    payloads: list[dict[str, Any]] = []
    by_source: dict[str, tuple[dict[str, Any], list, list[Path]]] = {}
    for source, measurements in source_measurements:
        source_id = str(source.get("source_id"))
        if not measurements:
            by_source[source_id] = (source, measurements, [])
            continue
        paths = source_output_paths(config, source)
        paths["qa_dir"].mkdir(parents=True, exist_ok=True)
        source_payloads = _full_qa_payloads(
            paths["qa_dir"],
            source,
            measurements,
            dpi=config.photometry.qa.measurement_plot_dpi,
            show_colorbars=config.photometry.qa.measurement_plot_colorbars,
        )
        payloads.extend(source_payloads)
        by_source[source_id] = (
            source,
            measurements,
            [Path(payload["path"]) for payload in source_payloads],
        )
    _write_full_measurement_qa_payloads(
        payloads,
        workers=config.photometry.qa.full_plot_workers,
        progress_callback=progress_callback,
    )
    for source, measurements, _ in by_source.values():
        if measurements:
            write_full_qa_manifest(config=config, source=source, measurements=measurements)
    return {source_id: paths for source_id, (_, _, paths) in by_source.items()}


def full_qa_measurement_path(config: Config, source: dict[str, Any], measurement_id: str) -> Path:
    paths = source_output_paths(config, source)
    return paths["qa_dir"] / f"{measurement_id}_qa.png"


def full_qa_files_exist(*, config: Config, source: dict[str, Any], measurements: list) -> bool:
    for measurement in measurements:
        measurement_id = str(getattr(measurement, "row", {}).get("measurement_id") or "")
        if not measurement_id:
            return False
        path = full_qa_measurement_path(config, source, measurement_id)
        if not path.exists() or path.stat().st_size <= 0:
            return False
    return True


def validate_full_qa_measurement(*, config: Config, source: dict[str, Any], measurement_id: str) -> bool:
    paths = source_output_paths(config, source)
    try:
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    full_qa = manifest.get("full_qa") or {}
    plot_info = (full_qa.get("plots") or {}).get(str(measurement_id))
    if not plot_info:
        return False
    path = full_qa_measurement_path(config, source, str(measurement_id))
    if not path.exists() or path.stat().st_size <= 0:
        return False
    if int(plot_info.get("size_bytes") or -1) != path.stat().st_size:
        return False
    return plot_info.get("sha256") == _sha256_file(path)


def validate_full_qa_outputs(
    *,
    config: Config,
    source: dict[str, Any],
    measurements: list,
) -> bool:
    paths = source_output_paths(config, source)
    if not validate_source_outputs(paths, config=config, source=source, measurements=measurements):
        return False
    try:
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    full_qa = manifest.get("full_qa") or {}
    expected_ids = sorted(
        str(getattr(measurement, "row", {}).get("measurement_id"))
        for measurement in measurements
        if getattr(measurement, "row", {}).get("measurement_id")
    )
    if full_qa.get("measurement_ids") != expected_ids:
        return False
    if int(full_qa.get("measurement_plot_count") or -1) != len(expected_ids):
        return False
    if not full_qa.get("complete"):
        return False
    plots = full_qa.get("plots") or {}
    for measurement_id in expected_ids:
        info = plots.get(measurement_id)
        if not info:
            return False
        path = full_qa_measurement_path(config, source, measurement_id)
        if not path.exists() or path.stat().st_size <= 0:
            return False
        if int(info.get("size_bytes") or -1) != path.stat().st_size:
            return False
        if info.get("sha256") != _sha256_file(path):
            return False
    return True


def write_full_qa_manifest(*, config: Config, source: dict[str, Any], measurements: list) -> dict[str, Any]:
    paths = source_output_paths(config, source)
    if paths["manifest"].exists():
        try:
            manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = _build_output_manifest(config, source, measurements, paths)
    else:
        manifest = _build_output_manifest(config, source, measurements, paths)
    manifest["full_qa"] = _full_qa_manifest_section(config, source, measurements)
    manifest.pop("manifest_hash", None)
    manifest["manifest_hash"] = _hash_json(manifest)
    _atomic_text(paths["manifest"], json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return manifest


def validate_source_outputs(
    paths: dict[str, Path],
    *,
    config: Config | None = None,
    source: dict[str, Any] | None = None,
    measurements: list | None = None,
) -> bool:
    required = ["csv", "sed", "qa", "provenance", "index", "manifest"]
    if not all(paths[key].exists() and paths[key].stat().st_size > 0 for key in required):
        return False
    try:
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if config is not None:
        if manifest.get("output_schema_version") != config.photometry.output_schema_version:
            return False
        if manifest.get("photometry_code_version") != config.photometry.code_version:
            return False
        if manifest.get("config_hash") != _config_hash(config):
            return False
    if source is not None and manifest.get("source_id") != source.get("source_id"):
        return False
    if measurements is not None:
        expected = _measurement_manifest_section(measurements)
        for key, value in expected.items():
            if manifest.get(key) != value:
                return False
    for key, info in (manifest.get("files") or {}).items():
        if key == "manifest":
            continue
        path = paths.get(key)
        if path is None or not path.exists():
            return False
        if int(info.get("size_bytes") or -1) != path.stat().st_size:
            return False
        if info.get("sha256") != _sha256_file(path):
            return False
    return True


def _write_sed_plot(path: Path, source: dict[str, Any], df: pd.DataFrame) -> None:
    _ensure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tmp = path.with_suffix(path.suffix + ".tmp")
    fig, ax = plt.subplots(figsize=(7, 4))
    if not df.empty:
        good = df["science_recommended"].astype(bool) if "science_recommended" in df else np.zeros(len(df), dtype=bool)
        ax.errorbar(
            df.loc[~good, "wavelength_um"],
            df.loc[~good, "selected_flux_uJy"],
            yerr=df.loc[~good, "selected_flux_err_uJy"],
            fmt="o",
            ms=4,
            alpha=0.5,
            color="tab:gray",
            label="measured",
        )
        ax.errorbar(
            df.loc[good, "wavelength_um"],
            df.loc[good, "selected_flux_uJy"],
            yerr=df.loc[good, "selected_flux_err_uJy"],
            fmt="o",
            ms=4,
            color="tab:blue",
            label="science recommended",
        )
    ax.axhline(0, color="black", lw=0.8, alpha=0.4)
    ax.set_xlabel("Wavelength (um)")
    ax.set_ylabel("Flux density (uJy)")
    ax.set_title(f"{source.get('source_name') or source['source_id']} | N={len(df)}")
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(handles, labels, loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(tmp, dpi=150, format="png")
    plt.close(fig)
    tmp.replace(path)


def _write_qa_summary(path: Path, source: dict[str, Any], df: pd.DataFrame) -> None:
    _ensure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tmp = path.with_suffix(path.suffix + ".tmp")
    fig, axes = plt.subplots(3, 1, figsize=(7, 7), sharex=True)
    if not df.empty:
        axes[0].errorbar(df["wavelength_um"], df["selected_flux_uJy"], yerr=df["selected_flux_err_uJy"], fmt="o", ms=3)
        axes[1].scatter(df["wavelength_um"], df["selected_snr"], s=14)
        axes[2].scatter(df["wavelength_um"], df["fit_quality"], s=14)
    axes[0].axhline(0, color="black", lw=0.8, alpha=0.4)
    axes[0].set_ylabel("uJy")
    axes[1].set_ylabel("S/N")
    axes[2].set_ylabel("fit ql")
    axes[2].set_xlabel("Wavelength (um)")
    fig.suptitle(f"{source.get('source_name') or source['source_id']} QA | N={len(df)}")
    fig.tight_layout()
    fig.savefig(tmp, dpi=150, format="png")
    plt.close(fig)
    tmp.replace(path)


def _write_full_measurement_qa(
    qa_dir: Path,
    measurements: list,
    *,
    dpi: int,
    show_colorbars: bool,
    workers: int,
    progress_callback=None,
) -> None:
    payloads = _full_qa_payloads(
        qa_dir,
        {},
        measurements,
        dpi=dpi,
        show_colorbars=show_colorbars,
    )
    _write_full_measurement_qa_payloads(
        payloads,
        workers=workers,
        progress_callback=progress_callback,
    )


def _full_qa_payloads(
    qa_dir: Path,
    source: dict[str, Any],
    measurements: list,
    *,
    dpi: int,
    show_colorbars: bool,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for measurement in measurements:
        row = getattr(measurement, "row", {})
        measurement_id = row.get("measurement_id")
        if not measurement_id:
            raise ValueError("cannot write full QA without measurement_id")
        qa_arrays = getattr(measurement, "qa_arrays", {}) or {}
        if not _has_full_qa_arrays(qa_arrays):
            raise ValueError(f"cannot write full QA for {measurement_id}: in-memory QA arrays are unavailable")
        payloads.append(
            {
                "path": str(qa_dir / f"{measurement_id}_qa.png"),
                "row": row,
                "provenance": getattr(measurement, "provenance", {}) or {},
                "qa_arrays": qa_arrays,
                "dpi": int(dpi),
                "show_colorbars": bool(show_colorbars),
                "source_id": source.get("source_id"),
                "source_name": source.get("source_name"),
                "measurement_id": measurement_id,
            }
        )
    return payloads


def _write_full_measurement_qa_payloads(
    payloads: list[dict[str, Any]],
    *,
    workers: int,
    progress_callback=None,
) -> None:
    total = len(payloads)
    if total <= 0:
        return
    if progress_callback is not None:
        progress_callback({"phase": "qa_start", "qa_written": 0, "qa_total": total})
    if workers > 1 and total > 1:
        try:
            pool = _qa_process_pool(workers)
            payload_iter = iter(payloads)
            pending = set()
            max_inflight = min(total, max(1, int(workers)) * 2)
            for _ in range(max_inflight):
                pending.add(pool.submit(_write_measurement_qa_payload, next(payload_iter)))
            written = 0
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                    written += 1
                    if progress_callback is not None:
                        progress_callback({"phase": "qa_plot", "qa_written": written, "qa_total": total})
                    try:
                        pending.add(pool.submit(_write_measurement_qa_payload, next(payload_iter)))
                    except StopIteration:
                        pass
            return
        except (BrokenProcessPool, NotImplementedError, OSError):
            _shutdown_qa_process_pool()
            if progress_callback is not None:
                progress_callback({"phase": "qa_parallel_unavailable", "qa_written": 0, "qa_total": total})
    with _PLOT_LOCK:
        for written, payload in enumerate(payloads, start=1):
            _write_measurement_qa_payload(payload)
            if progress_callback is not None:
                progress_callback({"phase": "qa_plot", "qa_written": written, "qa_total": total})


def _has_full_qa_arrays(arrays: dict[str, Any]) -> bool:
    if not arrays:
        return False
    return any(np.asarray(value).ndim == 2 for value in arrays.values())


def _qa_process_pool(workers: int) -> ProcessPoolExecutor:
    global _QA_POOL, _QA_POOL_WORKERS
    wanted = max(1, int(workers))
    with _QA_POOL_LOCK:
        if _QA_POOL is not None and _QA_POOL_WORKERS != wanted:
            pool = _QA_POOL
            _QA_POOL = None
            _QA_POOL_WORKERS = None
            pool.shutdown(wait=False, cancel_futures=True)
        if _QA_POOL is None:
            _QA_POOL = ProcessPoolExecutor(max_workers=wanted)
            _QA_POOL_WORKERS = wanted
        return _QA_POOL


def _shutdown_qa_process_pool() -> None:
    global _QA_POOL, _QA_POOL_WORKERS
    with _QA_POOL_LOCK:
        pool = _QA_POOL
        _QA_POOL = None
        _QA_POOL_WORKERS = None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def _write_measurement_qa_payload(payload: dict[str, Any]) -> None:
    _write_measurement_qa_from_parts(
        Path(payload["path"]),
        payload["row"],
        payload.get("provenance") or {},
        payload.get("qa_arrays") or {},
        dpi=int(payload.get("dpi") or 110),
        show_colorbars=bool(payload.get("show_colorbars")),
    )


def _write_measurement_qa(path: Path, measurement, *, dpi: int = 110, show_colorbars: bool = False) -> None:
    _write_measurement_qa_from_parts(
        path,
        measurement.row,
        measurement.provenance,
        measurement.qa_arrays,
        dpi=dpi,
        show_colorbars=show_colorbars,
    )


def _write_measurement_qa_from_parts(
    path: Path,
    row: dict[str, Any],
    provenance: dict[str, Any],
    arrays: dict[str, np.ndarray],
    *,
    dpi: int,
    show_colorbars: bool,
) -> None:
    _ensure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tmp = path.with_suffix(path.suffix + ".tmp")
    fig, axes = plt.subplots(2, 4, figsize=(12.5, 6.2))
    axes = axes.ravel()
    x = row.get("x_cutout")
    y = row.get("y_cutout")
    neighbors = provenance.get("neighbors", []) if provenance else []
    panels = [
        ("raw_image", "raw image", "RdBu_r"),
        ("background_2d", "2D background", "viridis"),
        ("data", "data-bg", "RdBu_r"),
        ("mask_source_map", "mask/source/bg", "tab20"),
        ("point_model", "point model", "RdBu_r"),
        ("point_residual_sigma", "point residual/sigma", "RdBu_r"),
        ("joint_model", "joint model", "RdBu_r"),
        ("joint_residual_sigma", "joint residual/sigma", "RdBu_r"),
    ]
    symmetric = {"raw_image", "data", "point_model", "joint_model", "point_residual_sigma", "joint_residual_sigma"}
    shape = _first_array_shape(arrays)
    for ax, (key, title, cmap) in zip(axes, panels, strict=True):
        image = _array_or_zeros(arrays, key, shape)
        kwargs = {}
        if key in symmetric:
            scale = float(np.nanpercentile(np.abs(image), 98)) if np.isfinite(image).any() else 1.0
            scale = scale if scale > 0 else 1.0
            kwargs = {"vmin": -scale, "vmax": scale}
        im = ax.imshow(image, origin="lower", cmap=cmap, interpolation="nearest", **kwargs)
        if key == "mask_source_map" and "flag_mask" in arrays:
            masked = np.ma.masked_where(arrays["flag_mask"] <= 0, arrays["flag_mask"])
            ax.imshow(masked, origin="lower", cmap="autumn", alpha=0.45)
        if x is not None and y is not None:
            ax.plot(float(x), float(y), marker="+", color="black", ms=9, mew=1.5)
            radius = row.get("target_protection_radius_pixels")
            if radius is not None:
                circle = plt.Circle((float(x), float(y)), float(radius), color="black", fill=False, lw=0.8, alpha=0.8)
                ax.add_patch(circle)
        for neighbor in neighbors:
            ax.plot(float(neighbor["x"]), float(neighbor["y"]), marker="x", color="lime", ms=6, mew=1.2)
        ax.set_title(title, fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        if show_colorbars:
            fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        else:
            _add_scale_text(ax, image, kwargs)
    flags = row.get("key_photometry_flags") or row.get("photometry_flags") or "-"
    if len(str(flags)) > 64:
        flags = str(flags)[:61] + "..."
    sci = "Y" if row.get("science_recommended") else "N"
    background_text = row.get("background_method") or row.get("background_model") or "-"
    background_flags = row.get("background_flags") or "-"
    if len(str(background_flags)) > 36:
        background_flags = str(background_flags)[:33] + "..."
    mjd_value = _finite_float(row.get("mjd_avg"))
    if mjd_value is None:
        mjd_value = _finite_float(row.get("mjd"))
    mjd_text = "-" if mjd_value is None else f"{mjd_value:.5f}"
    fig.suptitle(
        f"{row.get('source_name') or row['source_id']} obs={row.get('observation_id')} D{row.get('detector_id')} "
        f"MJD={mjd_text} {row['wavelength_um']:.3g}/{row.get('bandwidth_um', np.nan):.3g}um "
        f"{row['selected_flux_uJy']:.3g}+/-{row['selected_flux_err_uJy']:.3g}uJy "
        f"S/N={row['selected_snr']:.2f} mode={row['science_mode']} det={row['detection_status']} "
        f"bkg={background_text} bkg_flags={background_flags} sci={sci} flags={flags}",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.02, right=0.99, bottom=0.03, top=0.84, wspace=0.04, hspace=0.18)
    fig.savefig(tmp, dpi=dpi, format="png")
    plt.close(fig)
    tmp.replace(path)


def _add_scale_text(ax, image: np.ndarray, kwargs: dict[str, float]) -> None:
    if "vmin" in kwargs and "vmax" in kwargs:
        text = f"{kwargs['vmin']:.2g}..{kwargs['vmax']:.2g}"
    else:
        finite = np.asarray(image)[np.isfinite(image)]
        if finite.size:
            text = f"{float(np.nanmin(finite)):.2g}..{float(np.nanmax(finite)):.2g}"
        else:
            text = "no finite data"
    ax.text(
        0.02,
        0.03,
        text,
        transform=ax.transAxes,
        fontsize=6,
        color="white",
        ha="left",
        va="bottom",
        bbox={"facecolor": "black", "alpha": 0.45, "pad": 1.2, "linewidth": 0},
    )


def _first_array_shape(arrays: dict[str, np.ndarray]) -> tuple[int, int]:
    for value in arrays.values():
        arr = np.asarray(value)
        if arr.ndim == 2:
            return arr.shape
    return (1, 1)


def _array_or_zeros(arrays: dict[str, np.ndarray], key: str, shape: tuple[int, int]) -> np.ndarray:
    if key in arrays:
        return np.asarray(arrays[key], dtype=float)
    fallback = {
        "raw_image": "data",
        "background_2d": "background",
        "point_model": "model",
        "joint_model": "model",
        "point_residual_sigma": "residual_sigma",
        "joint_residual_sigma": "residual_sigma",
        "mask_source_map": "fit_mask",
    }.get(key)
    if fallback and fallback in arrays:
        return np.asarray(arrays[fallback], dtype=float)
    return np.zeros(shape, dtype=float)


def _finite_float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def _ensure_matplotlib_cache() -> None:
    if os.environ.get("MPLCONFIGDIR"):
        return
    cache_dir = Path(tempfile.gettempdir()) / "spherex_cutoutdb_matplotlib"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(cache_dir)


def _measurement_index(df: pd.DataFrame, paths: dict[str, Path], config: Config) -> dict[str, Any]:
    rows = {}
    for index, row in df.reset_index(drop=True).iterrows():
        measurement_id = row["measurement_id"]
        rows[measurement_id] = {
            "csv_row": int(index),
            "qa_plot_path": _rel_or_str(paths["qa_dir"] / f"{measurement_id}_qa.png", config),
            "cutout_key": row.get("cutout_key"),
            "spectral_wcs_calibration_id": row.get("spectral_wcs_calibration_id"),
            "solid_angle_calibration_id": row.get("solid_angle_calibration_id"),
        }
    return {
        "csv_path": _rel_or_str(paths["csv"], config),
        "measurements": rows,
    }


def _build_output_manifest(
    config: Config,
    source: dict[str, Any],
    measurements: list,
    paths: dict[str, Path],
) -> dict[str, Any]:
    payload = {
        "source_id": source.get("source_id"),
        "source_name": source.get("source_name"),
        "output_schema_version": config.photometry.output_schema_version,
        "photometry_code_version": config.photometry.code_version,
        "config_hash": _config_hash(config),
        **_measurement_manifest_section(measurements),
        "files": _file_manifest(paths),
    }
    payload["manifest_hash"] = _hash_json({key: value for key, value in payload.items() if key != "manifest_hash"})
    return payload


def _measurement_manifest_section(measurements: list) -> dict[str, Any]:
    rows = [getattr(measurement, "row", {}) for measurement in measurements]
    ids = sorted(str(row.get("measurement_id")) for row in rows if row.get("measurement_id"))
    row_hashes = {
        str(row.get("measurement_id")): _hash_json(row)
        for row in rows
        if row.get("measurement_id")
    }
    return {
        "measurement_ids": ids,
        "measurement_row_count": len(ids),
        "measurement_rows_hash": _hash_json(row_hashes),
    }


def _full_qa_manifest_section(config: Config, source: dict[str, Any], measurements: list) -> dict[str, Any]:
    plots: dict[str, dict[str, Any]] = {}
    ids: list[str] = []
    complete = True
    for measurement in measurements:
        row = getattr(measurement, "row", {})
        measurement_id = row.get("measurement_id")
        if not measurement_id:
            complete = False
            continue
        measurement_id = str(measurement_id)
        ids.append(measurement_id)
        path = full_qa_measurement_path(config, source, measurement_id)
        exists = path.exists() and path.stat().st_size > 0
        complete = complete and exists
        plots[measurement_id] = {
            "path": _rel_or_str(path, config),
            "exists": exists,
            "size_bytes": path.stat().st_size if exists else None,
            "sha256": _sha256_file(path) if exists else None,
        }
    return {
        "complete": bool(complete),
        "measurement_ids": sorted(ids),
        "measurement_plot_count": len(ids),
        "dpi": int(config.photometry.qa.measurement_plot_dpi),
        "colorbars": bool(config.photometry.qa.measurement_plot_colorbars),
        "plots": plots,
    }


def _file_manifest(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in ["csv", "sed", "qa", "provenance", "index"]:
        path = paths[key]
        out[key] = {
            "path": str(path),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "sha256": _sha256_file(path) if path.exists() else None,
        }
    return out


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _config_hash(config: Config) -> str:
    return _hash_json(config.model_dump(mode="json"))


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def _rel_or_str(path: Path, config: Config) -> str:
    try:
        return str(Path(path).resolve().relative_to(config.project.root))
    except ValueError:
        return str(path)
