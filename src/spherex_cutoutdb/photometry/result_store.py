"""Photometry planning and SQLite result-store helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from spherex_cutoutdb.config import Config, config_hash
from spherex_cutoutdb.database import canonical_json, stable_hash, utcnow


VALIDATION_OK = {"passed", "passed_with_warnings"}


def photometry_config_hash(config: Config) -> str:
    return stable_hash(config.photometry.model_dump(mode="json"))


def start_photometry_run(conn, config: Config, run_id: str | None = None) -> str:
    payload = {
        "run_id": run_id,
        "config_hash": photometry_config_hash(config),
        "schema": config.photometry.output_schema_version,
        "code": config.photometry.code_version,
        "started_at": utcnow(),
    }
    photometry_run_id = f"phot_{stable_hash(payload)[:16]}"
    conn.execute(
        """
        INSERT OR REPLACE INTO photometry_runs(
          photometry_run_id, run_id, started_at, status, config_hash, code_version, output_schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            photometry_run_id,
            run_id,
            utcnow(),
            "running",
            photometry_config_hash(config),
            config.photometry.code_version,
            config.photometry.output_schema_version,
        ),
    )
    conn.commit()
    return photometry_run_id


def finish_photometry_run(
    conn,
    photometry_run_id: str,
    status: str,
    counts: dict[str, Any] | None = None,
    summary_path: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE photometry_runs
        SET finished_at = ?, status = ?, counts_json = ?, summary_path = ?
        WHERE photometry_run_id = ?
        """,
        (utcnow(), status, canonical_json(counts or {}), summary_path, photometry_run_id),
    )
    conn.commit()


def measurement_id_for(
    *,
    source_id: str,
    cutout_key: str,
    cutout_sha256: str | None,
    spectral_wcs_calibration_id: str | None,
    solid_angle_calibration_id: str | None,
    config: Config,
) -> str:
    return stable_hash(
        {
            "source_id": source_id,
            "cutout_key": cutout_key,
            "cutout_sha256": cutout_sha256,
            "spectral_wcs_calibration_id": spectral_wcs_calibration_id,
            "solid_angle_calibration_id": solid_angle_calibration_id,
            "photometry_config_hash": photometry_config_hash(config),
            "code_version": config.photometry.code_version,
            "output_schema_version": config.photometry.output_schema_version,
        }
    )


def work_item_id_for(measurement_id: str) -> str:
    return stable_hash({"measurement_id": measurement_id})


def valid_measurement_exists(conn, measurement_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM photometry_measurements WHERE measurement_id = ? LIMIT 1",
        (measurement_id,),
    ).fetchone()
    return row is not None


def upsert_work_item(
    conn,
    *,
    photometry_run_id: str | None,
    source: dict[str, Any],
    plan_row: dict[str, Any],
    cutout: dict[str, Any] | None,
    calibration_resolution,
    state: str,
    reason: str | None,
    config: Config,
    commit: bool = True,
) -> dict[str, Any]:
    cutout_sha = cutout.get("sha256") if cutout else None
    spectral_id = calibration_resolution.products.get("spectral_wcs", {}).get("calibration_id")
    solid_id = calibration_resolution.products.get("solid_angle_pixel_map", {}).get("calibration_id")
    measurement_id = measurement_id_for(
        source_id=source["source_id"],
        cutout_key=plan_row["cutout_key"],
        cutout_sha256=cutout_sha,
        spectral_wcs_calibration_id=spectral_id,
        solid_angle_calibration_id=solid_id,
        config=config,
    )
    work_item_id = work_item_id_for(measurement_id)
    key = {
        "source_id": source["source_id"],
        "source_row_hash": source.get("row_hash"),
        "product_id": plan_row.get("product_id"),
        "cutout_key": plan_row.get("cutout_key"),
        "cutout_sha256": cutout_sha,
        "validation_status": cutout.get("validation_status") if cutout else None,
        "spectral_wcs_calibration_id": spectral_id,
        "solid_angle_calibration_id": solid_id,
        "photometry_config_hash": photometry_config_hash(config),
        "code_version": config.photometry.code_version,
        "output_schema_version": config.photometry.output_schema_version,
    }
    now = utcnow()
    conn.execute(
        """
        INSERT INTO photometry_work_items(
          work_item_id, photometry_run_id, source_id, product_id, cutout_key, cutout_id,
          measurement_id, state, reason, work_key_json, source_row_hash, cutout_sha256,
          validation_status, spectral_wcs_calibration_id, solid_angle_calibration_id,
          photometry_config_hash, code_version, output_schema_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(work_item_id) DO UPDATE SET
          photometry_run_id=excluded.photometry_run_id,
          cutout_id=excluded.cutout_id,
          measurement_id=excluded.measurement_id,
          state=excluded.state,
          reason=excluded.reason,
          work_key_json=excluded.work_key_json,
          cutout_sha256=excluded.cutout_sha256,
          validation_status=excluded.validation_status,
          spectral_wcs_calibration_id=excluded.spectral_wcs_calibration_id,
          solid_angle_calibration_id=excluded.solid_angle_calibration_id,
          updated_at=excluded.updated_at
        """,
        (
            work_item_id,
            photometry_run_id,
            source["source_id"],
            plan_row.get("product_id"),
            plan_row["cutout_key"],
            cutout.get("cutout_id") if cutout else None,
            measurement_id,
            state,
            reason,
            canonical_json(key),
            source.get("row_hash"),
            cutout_sha,
            cutout.get("validation_status") if cutout else None,
            spectral_id,
            solid_id,
            photometry_config_hash(config),
            config.photometry.code_version,
            config.photometry.output_schema_version,
            now,
            now,
        ),
    )
    if commit:
        conn.commit()
    return {
        "work_item_id": work_item_id,
        "measurement_id": measurement_id,
        "state": state,
        "reason": reason,
        "plan_row": plan_row,
        "cutout": cutout,
        "source": source,
        "calibration": calibration_resolution,
    }


def mark_work_item_state(
    conn,
    work_item_id: str,
    state: str,
    reason: str | None = None,
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        "UPDATE photometry_work_items SET state = ?, reason = ?, updated_at = ? WHERE work_item_id = ?",
        (state, reason, utcnow(), work_item_id),
    )
    if commit:
        conn.commit()


def record_measurement(
    conn,
    *,
    photometry_run_id: str,
    work_item_id: str,
    cutout_id: int | None,
    result,
    config: Config,
    commit: bool = True,
) -> None:
    row = result.row
    conn.execute(
        """
        INSERT INTO photometry_measurements(
          measurement_id, work_item_id, photometry_run_id, source_id, product_id, cutout_id,
          cutout_key, cutout_sha256, wavelength_um, bandwidth_um, point_flux_uJy,
          point_flux_err_uJy, joint_flux_uJy, joint_flux_err_uJy, selected_flux_uJy,
          selected_flux_err_uJy, selected_snr, science_mode, science_recommended,
          detection_status, photometry_flags, image_flags, fit_quality, chi2_reduced,
          n_valid_pixels, background_uJy_per_pixel, background_unc_uJy_per_pixel,
          deblend_status, n_neighbors, calibration_exact_match, spectral_wcs_calibration_id,
          solid_angle_calibration_id, output_schema_version, photometry_config_hash,
          code_version, row_json, provenance_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(measurement_id) DO UPDATE SET
          work_item_id=excluded.work_item_id,
          photometry_run_id=excluded.photometry_run_id,
          source_id=excluded.source_id,
          product_id=excluded.product_id,
          cutout_id=excluded.cutout_id,
          cutout_key=excluded.cutout_key,
          cutout_sha256=excluded.cutout_sha256,
          wavelength_um=excluded.wavelength_um,
          bandwidth_um=excluded.bandwidth_um,
          point_flux_uJy=excluded.point_flux_uJy,
          point_flux_err_uJy=excluded.point_flux_err_uJy,
          joint_flux_uJy=excluded.joint_flux_uJy,
          joint_flux_err_uJy=excluded.joint_flux_err_uJy,
          selected_flux_uJy=excluded.selected_flux_uJy,
          selected_flux_err_uJy=excluded.selected_flux_err_uJy,
          selected_snr=excluded.selected_snr,
          science_mode=excluded.science_mode,
          science_recommended=excluded.science_recommended,
          detection_status=excluded.detection_status,
          photometry_flags=excluded.photometry_flags,
          image_flags=excluded.image_flags,
          fit_quality=excluded.fit_quality,
          chi2_reduced=excluded.chi2_reduced,
          n_valid_pixels=excluded.n_valid_pixels,
          background_uJy_per_pixel=excluded.background_uJy_per_pixel,
          background_unc_uJy_per_pixel=excluded.background_unc_uJy_per_pixel,
          deblend_status=excluded.deblend_status,
          n_neighbors=excluded.n_neighbors,
          calibration_exact_match=excluded.calibration_exact_match,
          spectral_wcs_calibration_id=excluded.spectral_wcs_calibration_id,
          solid_angle_calibration_id=excluded.solid_angle_calibration_id,
          output_schema_version=excluded.output_schema_version,
          photometry_config_hash=excluded.photometry_config_hash,
          code_version=excluded.code_version,
          row_json=excluded.row_json,
          provenance_json=excluded.provenance_json,
          created_at=excluded.created_at
        """,
        (
            row["measurement_id"],
            work_item_id,
            photometry_run_id,
            row["source_id"],
            row.get("product_id"),
            cutout_id,
            row["cutout_key"],
            row.get("cutout_sha256"),
            _f(row.get("wavelength_um")),
            _f(row.get("bandwidth_um")),
            _f(row.get("point_flux_uJy")),
            _f(row.get("point_flux_err_uJy")),
            _f(row.get("joint_flux_uJy")),
            _f(row.get("joint_flux_err_uJy")),
            _f(row.get("selected_flux_uJy")),
            _f(row.get("selected_flux_err_uJy")),
            _f(row.get("selected_snr")),
            row.get("science_mode"),
            1 if row.get("science_recommended") else 0,
            row.get("detection_status"),
            row.get("photometry_flags"),
            row.get("image_flags"),
            _f(row.get("fit_quality")),
            _f(row.get("chi2_reduced")),
            row.get("n_valid_pixels"),
            _f(row.get("background_uJy_per_pixel")),
            _f(row.get("background_unc_uJy_per_pixel")),
            row.get("deblend_status"),
            row.get("n_neighbors"),
            1 if row.get("calibration_exact_match") else 0,
            row.get("spectral_wcs_calibration_id"),
            row.get("solid_angle_calibration_id"),
            config.photometry.output_schema_version,
            photometry_config_hash(config),
            config.photometry.code_version,
            canonical_json(row),
            canonical_json(result.provenance),
            utcnow(),
        ),
    )
    mark_work_item_state(conn, work_item_id, "persisted", None, commit=False)
    if commit:
        conn.commit()


def record_photometry_failure(
    conn,
    *,
    photometry_run_id: str | None,
    work_item_id: str | None,
    source_id: str | None,
    product_id: int | None,
    cutout_id: int | None,
    failure_type: str,
    reason: str,
    exception_class: str | None = None,
    traceback: str | None = None,
    retryable: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO photometry_failures(
          photometry_run_id, work_item_id, source_id, product_id, cutout_id,
          failure_type, status, reason, exception_class, traceback, retryable, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            photometry_run_id,
            work_item_id,
            source_id,
            product_id,
            cutout_id,
            failure_type,
            "open",
            reason,
            exception_class,
            traceback,
            1 if retryable else 0,
            utcnow(),
        ),
    )
    conn.commit()


def record_output_product(
    conn,
    *,
    photometry_run_id: str,
    source_id: str,
    product_type: str,
    path: Path,
    config: Config,
    measurement_id: str | None = None,
    commit: bool = True,
) -> None:
    rel = _rel_or_str(path, config)
    size = path.stat().st_size if path.exists() else None
    sha256 = _sha256_file(path) if path.exists() else None
    conn.execute(
        """
        INSERT INTO photometry_output_products(
          photometry_run_id, source_id, measurement_id, product_type, path, sha256, file_size_bytes,
          output_schema_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            photometry_run_id,
            source_id,
            measurement_id,
            product_type,
            rel,
            sha256,
            size,
            config.photometry.output_schema_version,
            utcnow(),
        ),
    )
    if commit:
        conn.commit()


def upsert_source_summary(
    conn,
    *,
    photometry_run_id: str,
    source_id: str,
    status: str,
    n_planned: int,
    n_measured: int,
    n_failed: int,
    n_science_recommended: int,
    paths: dict[str, Path],
    config: Config,
    summary: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO photometry_source_summaries(
          source_id, photometry_run_id, source_status, n_planned, n_measured, n_failed,
          n_science_recommended, spectrum_path, sed_plot_path, qa_summary_path,
          provenance_path, measurement_index_path, updated_at, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
          photometry_run_id=excluded.photometry_run_id,
          source_status=excluded.source_status,
          n_planned=excluded.n_planned,
          n_measured=excluded.n_measured,
          n_failed=excluded.n_failed,
          n_science_recommended=excluded.n_science_recommended,
          spectrum_path=excluded.spectrum_path,
          sed_plot_path=excluded.sed_plot_path,
          qa_summary_path=excluded.qa_summary_path,
          provenance_path=excluded.provenance_path,
          measurement_index_path=excluded.measurement_index_path,
          updated_at=excluded.updated_at,
          summary_json=excluded.summary_json
        """,
        (
            source_id,
            photometry_run_id,
            status,
            n_planned,
            n_measured,
            n_failed,
            n_science_recommended,
            _rel_or_str(paths["csv"], config),
            _rel_or_str(paths["sed"], config),
            _rel_or_str(paths["qa"], config),
            _rel_or_str(paths["provenance"], config),
            _rel_or_str(paths["index"], config),
            utcnow(),
            canonical_json(summary or {}),
        ),
    )
    conn.commit()


def latest_cutout_for_key(conn, cutout_key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM cutouts WHERE cutout_key = ? AND active = 1", (cutout_key,)).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def source_by_id(conn, source_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def source_by_name(conn, source_name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sources WHERE source_name = ? AND active = 1", (source_name,)).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def active_sources(conn, source_ids: list[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    sql = "SELECT * FROM sources WHERE active = 1"
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        sql += f" AND source_id IN ({placeholders})"
        params.extend(source_ids)
    sql += " ORDER BY COALESCE(priority, 999999), source_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [{key: row[key] for key in row.keys()} for row in conn.execute(sql, tuple(params)).fetchall()]


def _rel_or_str(path: Path, config: Config) -> str:
    try:
        return str(Path(path).resolve().relative_to(config.project.root))
    except ValueError:
        return str(path)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
