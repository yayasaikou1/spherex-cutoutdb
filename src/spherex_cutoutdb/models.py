"""Small dataclasses for operation results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(slots=True)
class CatalogValidationReport:
    valid: bool
    n_rows_input: int
    n_rows_valid: int
    n_rows_invalid: int
    valid_df: pd.DataFrame
    invalid_df: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DiscoveryResult:
    source_id: str
    rows: pd.DataFrame
    failures: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class DownloadResult:
    plan_id: int | None
    cutout_key: str
    local_path: Path
    success: bool
    status: str
    file_size_bytes: int = 0
    sha256: str | None = None
    attempts: int = 0
    reason: str | None = None


@dataclass(slots=True)
class DownloadSummary:
    attempted: int = 0
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0
    total_targets: int = 0
    successful_targets: int = 0
    partially_failed_targets: int = 0
    failed_targets: int = 0


@dataclass(slots=True)
class ValidationResult:
    path: Path
    status: str
    reason: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    file_size_bytes: int = 0
    sha256: str | None = None
    required_hdus_present: bool = False
    image_shape: list[int] | None = None
    flags_shape: list[int] | None = None
    variance_shape: list[int] | None = None
    zodi_shape: list[int] | None = None
    psf_shape: list[int] | None = None
    hdu_summary: dict[str, Any] = field(default_factory=dict)
    wcs_summary: dict[str, Any] = field(default_factory=dict)
    psf_metadata: dict[str, Any] = field(default_factory=dict)
    header_metadata: dict[str, Any] = field(default_factory=dict)
    wcwave_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccessDecision:
    access_method: str
    allowed: bool
    reason: str
    url: str | None = None
