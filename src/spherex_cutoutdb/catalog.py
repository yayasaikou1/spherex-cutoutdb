"""Source catalog reading, normalization, validation, and ingestion."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from astropy.table import Table

from .config import Config, config_hash
from .database import insert_catalog_version, stable_hash, upsert_sources
from .exceptions import CatalogError
from .models import CatalogValidationReport


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_source_catalog(config: Config) -> pd.DataFrame:
    path = config.catalog.path
    if not path.exists():
        raise CatalogError(f"source catalog does not exist: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".ecsv":
        return Table.read(path, format="ascii.ecsv").to_pandas()
    if suffix in {".fits", ".fit", ".fts"}:
        return Table.read(path).to_pandas()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise CatalogError(f"unsupported catalog format: {path.suffix}")


def _normalize_source_id(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)
    return text.strip("_") or None


def _bool_or_default(value: Any, default: bool = True) -> bool:
    if value is None or pd.isna(value):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return default


def _required_column(df: pd.DataFrame, name: str, logical: str) -> None:
    if name not in df.columns:
        raise CatalogError(f"catalog is missing required {logical} column: {name}")


def normalize_sources(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    cat = config.catalog
    _required_column(df, cat.ra_column, "RA")
    _required_column(df, cat.dec_column, "Dec")
    has_id = cat.source_id_column in df.columns
    has_name = cat.source_name_column in df.columns
    if not has_id and not has_name:
        raise CatalogError("catalog must contain source_id or source_name")

    rows: list[dict[str, Any]] = []
    mapped_columns = {
        cat.source_id_column,
        cat.source_name_column,
        cat.ra_column,
        cat.dec_column,
        *cat.optional_columns.values(),
    }
    for idx, raw in df.iterrows():
        source_name = None
        if has_name and not pd.isna(raw.get(cat.source_name_column)):
            source_name = str(raw.get(cat.source_name_column)).strip() or None
        source_id = _normalize_source_id(raw.get(cat.source_id_column)) if has_id else None
        if source_id is None and config.catalog.generate_missing_source_id:
            source_id = _normalize_source_id(source_name) if source_name else f"SRC_{idx + 1:06d}"

        extra = {
            str(col): _jsonable(raw[col])
            for col in df.columns
            if col not in mapped_columns and not pd.isna(raw[col])
        }
        notes_col = cat.optional_columns.get("notes")
        if notes_col in df.columns and not pd.isna(raw.get(notes_col)):
            extra["notes"] = _jsonable(raw.get(notes_col))

        row = {
            "source_id": source_id,
            "source_name": source_name,
            "ra_deg": _as_float(raw.get(cat.ra_column)),
            "dec_deg": _as_float(raw.get(cat.dec_column)),
            "cutout_size_arcsec": _optional_float(raw, cat.optional_columns.get("cutout_size_arcsec")),
            "source_type": _optional_text(raw, cat.optional_columns.get("source_type")),
            "priority": _optional_int(raw, cat.optional_columns.get("priority")),
            "active": _bool_or_default(raw.get(cat.optional_columns.get("active")), True)
            if cat.optional_columns.get("active") in df.columns
            else True,
            "extra_json": json.dumps(extra, sort_keys=True, separators=(",", ":")),
        }
        row["row_hash"] = compute_source_row_hash(pd.Series(row))
        rows.append(row)
    return pd.DataFrame(rows)


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if pd.isna(value):
        return None
    return value


def _as_float(value: Any) -> float:
    if value is None or pd.isna(value):
        return math.nan
    return float(value)


def _optional_float(row: pd.Series, column: str | None) -> float | None:
    if not column or column not in row.index or pd.isna(row.get(column)):
        return None
    return float(row.get(column))


def _optional_int(row: pd.Series, column: str | None) -> int | None:
    if not column or column not in row.index or pd.isna(row.get(column)):
        return None
    return int(row.get(column))


def _optional_text(row: pd.Series, column: str | None) -> str | None:
    if not column or column not in row.index or pd.isna(row.get(column)):
        return None
    text = str(row.get(column)).strip()
    return text or None


def compute_source_row_hash(row: pd.Series) -> str:
    payload = {
        "source_id": row.get("source_id"),
        "source_name": row.get("source_name"),
        "ra_deg": _round_or_none(row.get("ra_deg"), 10),
        "dec_deg": _round_or_none(row.get("dec_deg"), 10),
        "cutout_size_arcsec": _round_or_none(row.get("cutout_size_arcsec"), 4),
        "source_type": row.get("source_type"),
        "priority": row.get("priority"),
        "active": bool(row.get("active", True)),
        "extra_json": row.get("extra_json") or "{}",
    }
    return stable_hash(payload)


def _round_or_none(value: Any, digits: int) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def validate_sources(df: pd.DataFrame, config: Config) -> CatalogValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    invalid_indices: set[int] = set()

    for idx, row in df.iterrows():
        if not row.get("source_id"):
            errors.append(f"row {idx}: missing source_id")
            invalid_indices.add(idx)
        if not config.catalog.allow_missing_name and not row.get("source_name"):
            errors.append(f"row {idx}: missing source_name")
            invalid_indices.add(idx)
        ra = row.get("ra_deg")
        dec = row.get("dec_deg")
        if not _finite_in_range(ra, 0.0, 360.0, upper_open=True):
            errors.append(f"row {idx}: RA must be finite and in [0, 360)")
            invalid_indices.add(idx)
        if not _finite_in_range(dec, -90.0, 90.0, upper_open=False):
            errors.append(f"row {idx}: Dec must be finite and in [-90, 90]")
            invalid_indices.add(idx)
        size = row.get("cutout_size_arcsec")
        if size is not None and not pd.isna(size):
            if not (config.cutouts.min_size_arcsec <= float(size) <= config.cutouts.max_size_arcsec):
                errors.append(f"row {idx}: cutout size outside configured limits")
                invalid_indices.add(idx)

    duplicated = df[df["source_id"].duplicated(keep=False)] if "source_id" in df.columns else pd.DataFrame()
    if not config.catalog.allow_duplicate_target_ids:
        for source_id in sorted(set(duplicated.get("source_id", []))):
            errors.append(f"duplicate source_id: {source_id}")
            invalid_indices.update(duplicated.index[duplicated["source_id"] == source_id].tolist())

    warnings.extend(_near_duplicate_warnings(df, config.catalog.duplicate_position_tolerance_arcsec))

    invalid_df = df.loc[sorted(invalid_indices)].copy() if invalid_indices else df.iloc[0:0].copy()
    valid_df = df.drop(index=sorted(invalid_indices)).reset_index(drop=True)
    return CatalogValidationReport(
        valid=len(errors) == 0,
        n_rows_input=len(df),
        n_rows_valid=len(valid_df),
        n_rows_invalid=len(invalid_df),
        valid_df=valid_df,
        invalid_df=invalid_df,
        warnings=warnings,
        errors=errors,
    )


def _finite_in_range(value: Any, low: float, high: float, *, upper_open: bool) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(numeric):
        return False
    return low <= numeric < high if upper_open else low <= numeric <= high


def _near_duplicate_warnings(df: pd.DataFrame, tolerance_arcsec: float) -> list[str]:
    warnings: list[str] = []
    if len(df) < 2:
        return warnings
    tol_deg = tolerance_arcsec / 3600.0
    rows = df[["source_id", "ra_deg", "dec_deg"]].dropna().reset_index(drop=True)
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            dra = (float(rows.loc[i, "ra_deg"]) - float(rows.loc[j, "ra_deg"])) * math.cos(
                math.radians((float(rows.loc[i, "dec_deg"]) + float(rows.loc[j, "dec_deg"])) / 2)
            )
            ddec = float(rows.loc[i, "dec_deg"]) - float(rows.loc[j, "dec_deg"])
            if math.hypot(dra, ddec) <= tol_deg:
                warnings.append(
                    f"near-duplicate coordinates: {rows.loc[i, 'source_id']} and {rows.loc[j, 'source_id']}"
                )
    return warnings


def catalog_table_hash(df: pd.DataFrame) -> str:
    records = df.sort_values("source_id").to_dict(orient="records") if not df.empty else []
    return stable_hash(records)


def write_normalized_sources(df: pd.DataFrame, config: Config, catalog_version_id: str | None = None) -> Path:
    version = catalog_version_id or f"catalog_{_utc_stamp()}_{catalog_table_hash(df)[:8]}"
    versions_dir = config.project.root / "catalog" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    snapshot = versions_dir / f"{version}.ecsv"
    latest = config.project.root / "catalog" / "normalized_sources.ecsv"
    table = Table.from_pandas(df)
    table.write(snapshot, format="ascii.ecsv", overwrite=True)
    table.write(latest, format="ascii.ecsv", overwrite=True)
    return snapshot


def ingest_catalog(conn, config: Config, run_id: str | None = None) -> tuple[str, CatalogValidationReport, dict[str, int]]:
    raw = read_source_catalog(config)
    normalized = normalize_sources(raw, config)
    report = validate_sources(normalized, config)
    if not report.valid:
        raise CatalogError("; ".join(report.errors))
    catalog_hash = catalog_table_hash(report.valid_df)
    catalog_version_id = f"cat_{_utc_stamp()}_{catalog_hash[:8]}"
    snapshot = write_normalized_sources(report.valid_df, config, catalog_version_id)
    try:
        normalized_rel = str(snapshot.relative_to(config.project.root))
    except ValueError:
        normalized_rel = str(snapshot)
    try:
        input_rel = str(config.catalog.path.relative_to(config.project.root))
    except ValueError:
        input_rel = str(config.catalog.path)
    insert_catalog_version(
        conn,
        catalog_version_id,
        input_rel,
        normalized_rel,
        catalog_hash,
        report.n_rows_input,
        report.n_rows_valid,
        report.n_rows_invalid,
        config_hash(config),
        run_id,
    )
    stats = upsert_sources(conn, catalog_version_id, report.valid_df)
    return catalog_version_id, report, stats
