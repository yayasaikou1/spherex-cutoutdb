"""Deterministic source slugs, product parsing, and local paths."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse


def short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def safe_slug(text: str | None, *, max_length: int = 64) -> str:
    value = (text or "unknown").strip()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value).strip("_")
    if not value:
        value = "unknown"
    if len(value) <= max_length:
        return value
    suffix = short_hash(value)
    keep = max_length - len(suffix) - 1
    return f"{value[:keep].rstrip('_')}_{suffix}"


def parent_filename_from_url(access_url: str | None) -> str | None:
    if not access_url:
        return None
    path = urlparse(access_url).path
    name = Path(path).name
    return name or None


def parse_parent_metadata(parent_filename: str | None, access_url: str | None = None) -> dict[str, object]:
    filename = parent_filename or parent_filename_from_url(access_url) or ""
    stem = Path(filename).stem
    detector = None
    detector_match = re.search(r"(?:^|[_-])(?:D|[0-9]D)([1-6])(?:[_\-.]|$)", stem, re.IGNORECASE)
    if detector_match:
        detector = int(detector_match.group(1))
    else:
        simple = re.search(r"D([1-6])", stem, re.IGNORECASE)
        if simple:
            detector = int(simple.group(1))

    planning_period = None
    pp_match = re.search(r"(\d{4}W\d{2}[_-][A-Za-z0-9]+)", stem)
    if pp_match:
        planning_period = pp_match.group(1)

    processing_version = None
    pv_match = re.search(r"(l2[ab]-v[^_\s]+)", stem, re.IGNORECASE)
    if pv_match:
        processing_version = pv_match.group(1)
    else:
        pv_match = re.search(r"(v\d+(?:[.-]\d+)*)", stem, re.IGNORECASE)
        if pv_match:
            processing_version = pv_match.group(1)

    processing_date = None
    pd_match = re.search(r"(\d{4}[-_]\d{3}|\d{8})", stem)
    if pd_match:
        processing_date = pd_match.group(1).replace("_", "-")

    observation_id = None
    obs_match = re.search(r"(?:^|[_-])(\d{4,})(?:[_-]|$)", stem)
    if obs_match:
        observation_id = obs_match.group(1)

    return {
        "parent_filename": filename or None,
        "parent_stem": stem or "parent",
        "detector_id": detector,
        "bandpass": f"D{detector}" if detector is not None else None,
        "planning_period": planning_period or "unknown_period",
        "processing_version": processing_version or "unknown_version",
        "processing_date": processing_date,
        "observation_id": observation_id,
    }


def deterministic_cutout_filename(
    source_id: str,
    parent_filename: str | None,
    cutout_size_arcsec: float,
    cutout_key: str,
) -> str:
    source_slug = safe_slug(source_id)
    parent_stem = safe_slug(Path(parent_filename or "parent").stem, max_length=96)
    return f"{source_slug}__{parent_stem}__sz{cutout_size_arcsec:0.1f}as__{cutout_key[:8]}.fits"


def deterministic_cutout_path(
    data_root: Path,
    source_id: str,
    collection: str,
    planning_period: str | None,
    processing_version: str | None,
    detector_id: int | None,
    parent_filename: str | None,
    cutout_size_arcsec: float,
    cutout_key: str,
) -> Path:
    source_slug = safe_slug(source_id)
    period = safe_slug(planning_period or "unknown_period")
    version = safe_slug(processing_version or "unknown_version")
    detector = f"D{detector_id}" if detector_id is not None else "Dunknown"
    filename = deterministic_cutout_filename(source_id, parent_filename, cutout_size_arcsec, cutout_key)
    return data_root / "cutouts" / source_slug / collection / period / version / detector / filename
