"""SPHEREx SIA2 discovery and VOTable normalization."""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from astropy.io.votable import parse_single_table
from astropy.table import Table

from .cloud_access import parse_cloud_access
from .config import Config
from .database import stable_hash
from .exceptions import DiscoveryError
from .filenames import parent_filename_from_url, parse_parent_metadata
from .models import DiscoveryResult


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_sia_params(
    collection: str,
    ra_deg: float,
    dec_deg: float,
    radius_deg: float,
    maxrec: int,
    response_format: str = "votable",
) -> dict[str, str]:
    return {
        "COLLECTION": collection,
        "POS": f"circle {float(ra_deg):.10f} {float(dec_deg):.10f} {float(radius_deg):.10g}",
        "RESPONSEFORMAT": response_format.upper(),
        "MAXREC": str(int(maxrec)),
    }


def read_votable_bytes(content: bytes) -> pd.DataFrame:
    return _votable_to_dataframe(io.BytesIO(content))


def read_votable_file(path: Path) -> pd.DataFrame:
    return _votable_to_dataframe(path)


def _votable_to_dataframe(source: str | Path | io.BytesIO) -> pd.DataFrame:
    """Read a VOTable preserving FIELD ``name`` values over generated IDs.

    IRSA SIA VOTables can use generic FIELD IDs such as ``col_15`` while the
    scientifically meaningful ObsCore names live in FIELD ``name``. Astropy's
    high-level Table conversion may expose the IDs as dataframe columns, so we
    parse the VOTable metadata and rename those columns back to FIELD names.
    """

    votable = parse_single_table(source)
    table = votable.to_table(use_names_over_ids=True)
    id_to_name = {
        field.ID: field.name
        for field in votable.fields
        if field.ID and field.name and field.ID != field.name
    }
    if id_to_name:
        for old, new in id_to_name.items():
            if old in table.colnames and new not in table.colnames:
                table.rename_column(old, new)
    return table.to_pandas()


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if value is pd.NA:
        return None
    if hasattr(value, "mask") and bool(getattr(value, "mask", False)):
        return None
    if pd.isna(value):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            pass
    return value


def _row_dict(row: pd.Series) -> dict[str, Any]:
    return {str(key): _clean_value(value) for key, value in row.items()}


def _get(raw: dict[str, Any], *names: str) -> Any:
    lower = {key.lower(): value for key, value in raw.items()}
    for name in names:
        if name in raw:
            return raw[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def normalize_sia_dataframe(
    df: pd.DataFrame,
    *,
    source_id: str,
    collection: str,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    discovered_at = _now()
    for _, row in df.iterrows():
        raw = _row_dict(row)
        access_url = _get(raw, "access_url", "accessUrl", "access_url ")
        parent_filename = parent_filename_from_url(access_url)
        parsed = parse_parent_metadata(parent_filename, access_url)
        cloud_access = parse_cloud_access(_get(raw, "cloud_access"))
        raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
        normalized = {
            "source_id": source_id,
            "collection": collection,
            "s_ra": _get(raw, "s_ra"),
            "s_dec": _get(raw, "s_dec"),
            "obs_collection": _get(raw, "obs_collection"),
            "obs_id": _get(raw, "obs_id"),
            "obs_publisher_did": _get(raw, "obs_publisher_did"),
            "energy_bandpassname": _get(raw, "energy_bandpassname"),
            "em_min": _get(raw, "em_min"),
            "em_max": _get(raw, "em_max"),
            "em_res_power": _get(raw, "em_res_power"),
            "t_min": _get(raw, "t_min"),
            "t_max": _get(raw, "t_max"),
            "t_exptime": _get(raw, "t_exptime"),
            "access_url": access_url,
            "access_format": _get(raw, "access_format"),
            "access_estsize": _get(raw, "access_estsize"),
            "cloud_access_json": json.dumps(cloud_access, sort_keys=True, separators=(",", ":")),
            "s_region": _get(raw, "s_region"),
            "s_pixel_scale": _get(raw, "s_pixel_scale"),
            "dist_to_point": _get(raw, "dist_to_point"),
            "discovered_at": discovered_at,
            "raw_sia_json": raw_json,
            "parent_filename": parent_filename,
            "planning_period": parsed["planning_period"],
            "observation_id": _get(raw, "obs_id") or parsed["observation_id"],
            "detector_id": parsed["detector_id"],
            "bandpass": parsed["bandpass"] or _get(raw, "energy_bandpassname"),
            "processing_version": parsed["processing_version"],
            "processing_date": parsed["processing_date"],
        }
        normalized["product_signature"] = compute_product_signature(normalized)
        normalized["row_hash"] = stable_hash(
            {
                key: normalized.get(key)
                for key in [
                    "collection",
                    "obs_publisher_did",
                    "obs_id",
                    "access_url",
                    "parent_filename",
                    "processing_version",
                    "processing_date",
                ]
            }
        )
        rows.append(normalized)
    return pd.DataFrame(rows)


def compute_product_signature(row: dict[str, Any] | pd.Series) -> str:
    get = row.get
    if get("obs_publisher_did"):
        identity = {
            "collection": get("collection"),
            "obs_publisher_did": get("obs_publisher_did"),
            "processing_version": get("processing_version"),
            "processing_date": get("processing_date"),
        }
    else:
        identity = {
            "collection": get("collection"),
            "access_url": get("access_url"),
            "parent_filename": get("parent_filename"),
            "observation_id": get("observation_id"),
            "detector_id": get("detector_id"),
            "processing_version": get("processing_version"),
            "processing_date": get("processing_date"),
        }
    return stable_hash(identity)


def discover_for_source(
    source: dict[str, Any] | pd.Series,
    config: Config,
    run_id: str | None,
    console: Any | None = None,
    *,
    collections: list[str] | None = None,
    mock_sia: Path | None = None,
    session: requests.Session | None = None,
) -> DiscoveryResult:
    source_id = str(source["source_id"])
    all_rows: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    selected_collections = collections or config.discovery.collections
    for collection in selected_collections:
        try:
            if mock_sia:
                raw_df = read_votable_file(Path(mock_sia))
            else:
                raw_df = query_sia_for_source(source, collection, config, session=session)
            normalized = normalize_sia_dataframe(raw_df, source_id=source_id, collection=collection)
            all_rows.append(normalized)
            if console is not None:
                console.print(f"{source_id} {collection}: {len(normalized)} candidate parent MEFs")
        except Exception as exc:  # noqa: BLE001 - failures are recorded, not hidden
            failures.append(
                {
                    "source_id": source_id,
                    "phase": "discovery",
                    "status": "retryable",
                    "reason": f"{collection} discovery failed",
                    "exception_class": exc.__class__.__name__,
                    "exception_message": str(exc),
                }
            )
    rows = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    return DiscoveryResult(source_id=source_id, rows=rows, failures=failures)


def query_sia_for_source(
    source: dict[str, Any] | pd.Series,
    collection: str,
    config: Config,
    *,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    client = session or requests.Session()
    radius_deg = float(config.discovery.search_radius_arcsec) / 3600.0
    params = build_sia_params(
        collection,
        float(source["ra_deg"]),
        float(source["dec_deg"]),
        radius_deg,
        config.discovery.maxrec_per_source_collection,
        config.discovery.response_format,
    )
    headers = {"User-Agent": config.download.user_agent}
    last_exc: Exception | None = None
    for attempt in range(1, config.discovery.retry.attempts + 1):
        try:
            response = client.get(
                config.discovery.sia_endpoint,
                params=params,
                headers=headers,
                timeout=(config.download.connect_timeout_sec, config.download.read_timeout_sec),
            )
            if response.status_code in config.discovery.retry.retry_http_status:
                raise DiscoveryError(f"HTTP {response.status_code}")
            response.raise_for_status()
            return read_votable_bytes(response.content)
        except Exception as exc:  # noqa: BLE001 - retry boundary
            last_exc = exc
            if attempt >= config.discovery.retry.attempts:
                break
            delay = config.discovery.retry.backoff_seconds[
                min(attempt - 1, len(config.discovery.retry.backoff_seconds) - 1)
            ]
            time.sleep(delay)
    raise DiscoveryError(str(last_exc) if last_exc else "SIA discovery failed")


def build_match_rows(
    source: dict[str, Any] | pd.Series,
    rows_df: pd.DataFrame,
    product_ids_by_signature: dict[str, int],
    config: Config,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    radius_deg = float(config.discovery.search_radius_arcsec) / 3600.0
    for _, row in rows_df.iterrows():
        product_id = product_ids_by_signature.get(row["product_signature"])
        payload = {
            "source_id": source["source_id"],
            "product_id": product_id,
            "search_radius_deg": round(radius_deg, 12),
            "collection": row["collection"],
        }
        records.append(
            {
                "source_id": source["source_id"],
                "product_id": product_id,
                "collection": row["collection"],
                "query_ra_deg": float(source["ra_deg"]),
                "query_dec_deg": float(source["dec_deg"]),
                "search_radius_deg": radius_deg,
                "dist_to_point": row.get("dist_to_point"),
                "coverage_status": "covered",
                "match_hash": stable_hash(payload),
            }
        )
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)
