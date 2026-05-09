"""Configuration loading, validation, and default project creation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .exceptions import ConfigError

ALLOWED_CUTOUT_COLLECTIONS = {"spherex_qr2", "spherex_qr2_deep"}
EXCLUDED_CUTOUT_COLLECTIONS = {"spherex_qr2_cal"}


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryConfig(StrictConfigModel):
    attempts: int = 3
    backoff_seconds: list[float] = Field(default_factory=lambda: [2, 5, 15])
    jitter_seconds: float = 1.0
    retry_http_status: list[int] = Field(default_factory=lambda: [408, 429, 500, 502, 503, 504])
    honor_retry_after: bool = True

    @field_validator("attempts")
    @classmethod
    def attempts_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("retry attempts must be at least 1")
        return value


class ProjectConfig(StrictConfigModel):
    name: str = "spherex_cutoutdb"
    root: Path = Path(".")
    database_path: Path = Path("db/cutoutdb.sqlite")
    data_root: Path = Path("data")
    manifest_root: Path = Path("manifests")
    log_root: Path = Path("logs")
    cache_root: Path = Path("cache")


class CatalogConfig(StrictConfigModel):
    path: Path = Path("catalog/sources.csv")
    source_id_column: str = "source_id"
    source_name_column: str = "source_name"
    ra_column: str = "ra_deg"
    dec_column: str = "dec_deg"
    ra_unit: str = "deg"
    dec_unit: str = "deg"
    allow_missing_name: bool = True
    allow_duplicate_target_ids: bool = False
    generate_missing_source_id: bool = True
    duplicate_position_tolerance_arcsec: float = 0.2
    optional_columns: dict[str, str] = Field(
        default_factory=lambda: {
            "cutout_size_arcsec": "cutout_size_arcsec",
            "source_type": "source_type",
            "priority": "priority",
            "active": "active",
            "notes": "notes",
        }
    )


class SpherexConfig(StrictConfigModel):
    release: str = "QR2"
    allowed_level: int = 2
    product_type: str = "spectral_image_mef"


class DiscoveryConfig(StrictConfigModel):
    sia_endpoint: str = "https://irsa.ipac.caltech.edu/SIA"
    collections: list[str] = Field(default_factory=lambda: ["spherex_qr2", "spherex_qr2_deep"])
    exclude_collections: list[str] = Field(default_factory=lambda: ["spherex_qr2_cal"])
    search_radius_arcsec: float = 1.0
    maxrec_per_source_collection: int = 25000
    concurrency: int = 4
    response_format: str = "votable"
    backend_preference: list[str] = Field(default_factory=lambda: ["pyvo", "astroquery", "requests"])
    retry: RetryConfig = Field(default_factory=RetryConfig)
    cache_ttl_hours: float = 0.0

    @field_validator("collections")
    @classmethod
    def validate_collections(cls, value: list[str]) -> list[str]:
        bad = sorted(set(value) - ALLOWED_CUTOUT_COLLECTIONS)
        excluded = sorted(set(value) & EXCLUDED_CUTOUT_COLLECTIONS)
        if excluded:
            raise ValueError("spherex_qr2_cal is not allowed for cutout discovery")
        if bad:
            raise ValueError(f"unsupported discovery collections: {', '.join(bad)}")
        if not value:
            raise ValueError("at least one discovery collection is required")
        return value

    @field_validator("search_radius_arcsec")
    @classmethod
    def radius_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("search_radius_arcsec must be positive")
        return value

    @field_validator("concurrency")
    @classmethod
    def concurrency_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("discovery.concurrency must be at least 1")
        return value


class CutoutsConfig(StrictConfigModel):
    default_size_arcsec: float = 180.0
    size_column: str = "cutout_size_arcsec"
    min_size_arcsec: float = 6.0
    max_size_arcsec: float = 3600.0
    size_unit_for_url: str = "deg"
    use_parent_access_url: bool = True
    append_query_parameters: dict[str, str] = Field(
        default_factory=lambda: {"center": "{ra_deg},{dec_deg}", "size": "{size_deg}"}
    )
    preserve_psf_hdu_unmodified: bool = True

    @model_validator(mode="after")
    def validate_sizes(self) -> "CutoutsConfig":
        if self.default_size_arcsec <= 0:
            raise ValueError("cutouts.default_size_arcsec must be positive")
        if self.min_size_arcsec <= 0:
            raise ValueError("cutouts.min_size_arcsec must be positive")
        if self.max_size_arcsec < self.min_size_arcsec:
            raise ValueError("cutouts.max_size_arcsec must be >= min_size_arcsec")
        if not (self.min_size_arcsec <= self.default_size_arcsec <= self.max_size_arcsec):
            raise ValueError("cutouts.default_size_arcsec must be within configured limits")
        return self


class CloudConfig(StrictConfigModel):
    prefer_cloud_for_full_products: bool = True
    prefer_cloud_for_cutouts: bool = False
    require_official_cloud_cutout_metadata: bool = True
    official_cutout_capability_key: str | None = None
    s3: dict[str, Any] = Field(
        default_factory=lambda: {
            "anonymous": True,
            "expected_bucket": "nasa-irsa-spherex",
            "expected_region": "us-east-1",
        }
    )

    @model_validator(mode="after")
    def validate_cloud_cutouts(self) -> "CloudConfig":
        if self.prefer_cloud_for_cutouts and not self.official_cutout_capability_key:
            raise ValueError(
                "cloud.prefer_cloud_for_cutouts requires cloud.official_cutout_capability_key"
            )
        return self


class PlanningConfig(StrictConfigModel):
    skip_existing_valid: bool = True
    redownload_invalid: bool = True
    version_policy: str = "keep_all_mark_superseded"
    duplicate_parent_policy: str = "unique_by_access_url"
    duplicate_cutout_policy: str = "unique_by_source_product_size_center"


class DownloadConfig(StrictConfigModel):
    concurrency: int = 4096
    max_workers: int | None = 64
    per_host_rate_limit_per_second: float = 4096
    per_host_max_concurrency: int = 2048
    connect_timeout_sec: float = 20.0
    read_timeout_sec: float = 180.0
    total_timeout_sec: float = 100000000.0
    min_download_rate_bytes_per_second: float = 0.0
    low_speed_time_sec: float = 30.0
    chunk_size_bytes: int = 1048576
    retry: RetryConfig = Field(
        default_factory=lambda: RetryConfig(
            attempts=4,
            backoff_seconds=[1, 2, 5, 15],
            retry_http_status=[408, 429, 500, 502, 503, 504],
            honor_retry_after=True,
        )
    )
    partial_suffix: str = ".part"
    atomic_rename: bool = True
    skip_existing: bool = True
    overwrite_existing: bool = False
    user_agent: str = "spherex-cutoutdb/1.0.0rc1"

    @model_validator(mode="after")
    def validate_download_settings(self) -> "DownloadConfig":
        if self.concurrency < 1:
            raise ValueError("download.concurrency must be at least 1")
        if self.max_workers is None:
            self.max_workers = self.concurrency
        if self.max_workers < 1:
            raise ValueError("download.max_workers must be at least 1")
        if self.per_host_rate_limit_per_second < 0:
            raise ValueError("download.per_host_rate_limit_per_second must be non-negative")
        if self.per_host_max_concurrency is not None and self.per_host_max_concurrency < 1:
            raise ValueError("download.per_host_max_concurrency must be at least 1")
        for name in ["connect_timeout_sec", "read_timeout_sec", "total_timeout_sec"]:
            if getattr(self, name) <= 0:
                raise ValueError(f"download.{name} must be positive")
        if self.min_download_rate_bytes_per_second < 0:
            raise ValueError("download.min_download_rate_bytes_per_second must be non-negative")
        if self.low_speed_time_sec <= 0:
            raise ValueError("download.low_speed_time_sec must be positive")
        if self.chunk_size_bytes <= 0:
            raise ValueError("download.chunk_size_bytes must be positive")
        return self


class ValidationConfig(StrictConfigModel):
    require_hdus: list[str] = Field(
        default_factory=lambda: ["IMAGE", "FLAGS", "VARIANCE", "PSF", "WCS-WAVE"]
    )
    require_spatial_wcs: bool = True
    require_spectral_wcs_or_wcwave: bool = True
    require_psf_hdu: bool = True
    check_fits_verify: bool = True
    compute_sha256: bool = True
    record_header_cards: list[str] = Field(
        default_factory=lambda: [
            "OBSID",
            "OBS_ID",
            "DETECTOR",
            "DETID",
            "VERSION",
            "PROCVER",
            "PROCDATE",
            "DATE",
            "DATE-OBS",
            "MJD-AVG",
            "MJD",
            "EXPID",
            "BAND",
            "FILTER",
            "CRPIX1",
            "CRPIX2",
            "CRPIX1A",
            "CRPIX2A",
        ]
    )
    psf_header_issue_policy: str = "warn_only"


class LoggingConfig(StrictConfigModel):
    verbose: bool = False
    rich: bool = True
    progress_bars: bool = True
    jsonl_run_log: bool = True
    log_level: str = "INFO"


class ExportsConfig(StrictConfigModel):
    formats: list[str] = Field(default_factory=lambda: ["csv", "parquet"])
    write_latest_symlinks: bool = True


class CalibrationConfig(StrictConfigModel):
    release: str = "QR2"
    required_products: list[str] = Field(default_factory=lambda: ["spectral_wcs", "solid_angle_pixel_map"])
    optional_products: list[str] = Field(default_factory=list)
    cache_root: Path = Path("cache/calibrations")
    version_policy: str = "exact_required"
    allow_latest_fallback: bool = False
    validate_on_use: bool = True
    download_source: str = "cloud"
    prefer_cloud: bool = True
    cloud_bucket: str = "nasa-irsa-spherex"
    cloud_region: str = "us-east-1"
    cloud_prefix: str = "{release_lower}"
    official_ibe_base_url: str = "https://irsa.ipac.caltech.edu/ibe/data/spherex/{release_lower}"
    official_ibe_listing_url: str = "https://irsa.ipac.caltech.edu/ibe/dir/list/spherex/{release_lower}"
    use_official_ibe: bool = True
    download_max_workers: int = 64
    download_timeout_sec: int = 180
    product_urls: dict[str, str] = Field(default_factory=dict)

    @field_validator("required_products")
    @classmethod
    def required_products_supported(cls, value: list[str]) -> list[str]:
        allowed = {"spectral_wcs", "solid_angle_pixel_map"}
        bad = sorted(set(value) - allowed)
        if bad:
            raise ValueError(f"unsupported required calibration product(s): {', '.join(bad)}")
        return value

    @model_validator(mode="after")
    def validate_download_settings(self) -> "CalibrationConfig":
        if self.download_max_workers < 1:
            raise ValueError("calibration.download_max_workers must be at least 1")
        if self.download_timeout_sec <= 0:
            raise ValueError("calibration.download_timeout_sec must be positive")
        if self.download_source not in {"auto", "cloud", "ibe"}:
            raise ValueError("calibration.download_source must be one of auto, cloud, ibe")
        return self


class PhotometryBackgroundConfig(StrictConfigModel):
    method: str = "source_masked_plane"
    model: str = "plane"
    engine: str = "photutils"
    allow_plane: bool = True
    min_unmasked_pixels: int = 10
    min_plane_pixels: int = 12
    plane_condition_number_max: float = 1.0e8
    sigma_clip: float = 3.0
    sigma_clip_iterations: int = 2
    photutils_box_size: int = 16
    photutils_filter_size: int = 3
    photutils_exclude_percentile: float = 80.0


class PhotometryMasksConfig(StrictConfigModel):
    fit_exclude_bits: list[str | int] = Field(
        default_factory=lambda: ["SUR_ERROR", "NONFUNC", "MISSING_DATA", "HOT", "COLD", "NONLINEAR", "PERSIST"]
    )
    background_exclude_bits: list[str | int] = Field(
        default_factory=lambda: [
            "TRANSIENT",
            "OVERFLOW",
            "SUR_ERROR",
            "NONFUNC",
            "MISSING_DATA",
            "HOT",
            "COLD",
            "NONLINEAR",
            "PERSIST",
            "OUTLIER",
            "SOURCE",
        ]
    )
    qa_propagate_bits: list[str | int] = Field(default_factory=list)
    science_blocker_bits: list[str | int] = Field(
        default_factory=lambda: ["OVERFLOW", "SUR_ERROR", "NONFUNC", "DICHROIC", "MISSING_DATA", "HOT", "COLD", "NONLINEAR", "PERSIST"]
    )
    footprint_threshold: float = 0.01
    target_protection_footprint_threshold: float = 0.2
    target_protection_radius_pixels: float = 2.0
    target_protection_dilate_pixels: int = 1
    source_finding_edge_buffer_pixels: int = 1


class PhotometryPsfConfig(StrictConfigModel):
    oversampling_factor: int = 10
    require_zone_center_header: bool = True
    allow_plane0_without_centers: bool = False


class PhotometryFitConfig(StrictConfigModel):
    condition_number_max: float = 1.0e8
    min_template_fraction_unmasked: float = 0.5
    central_radius_pixels: float = 2.0
    central_positive_residual_sigma: float = 3.0
    target_neighbor_correlation_max: float = 0.9
    uncertainty_inflation_max: float = 3.0
    science_fit_quality_max: float = 3.0


class PhotometryDeblendingConfig(StrictConfigModel):
    enabled: bool = True
    method: str = "connected_components_target_protected"
    max_neighbors: int = 8
    material_overlap_threshold: float = 0.05
    residual_snr_threshold: float = 5.0
    neighbor_flux_snr_threshold: float = 5.0
    min_component_pixels: int = 1
    merge_radius_pixels: float = 2.0
    min_distance_from_target_pixels: float = 2.0
    reject_components_touching_target_protection: bool = True


class PhotometryQaConfig(StrictConfigModel):
    full_plot_layout: str = "point_vs_joint_with_background"
    full_plot_workers: int = 32
    measurement_plot_dpi: int = 110
    measurement_plot_colorbars: bool = False

    @model_validator(mode="after")
    def validate_qa(self) -> "PhotometryQaConfig":
        if self.full_plot_workers < 1:
            raise ValueError("photometry.qa.full_plot_workers must be at least 1")
        if self.measurement_plot_dpi < 72:
            raise ValueError("photometry.qa.measurement_plot_dpi must be at least 72")
        return self


class PhotometryExtendedConfig(StrictConfigModel):
    enabled: bool = False
    require_user_morphology: bool = True
    recommend_only_if_isolated: bool = True


class PhotometryCleanupConfig(StrictConfigModel):
    delete_successful_cutouts: bool = True
    keep_failed_cutouts: bool = True


class PhotometryConfig(StrictConfigModel):
    output_root: Path = Path("results")
    default_cutout_size_arcsec: float = 180.0
    fit_box_pixels: int = 15
    psf_template_radius_pixels: int = 6
    detection_snr_threshold: float = 3.0
    qa_level: str = "standard"
    output_schema_version: str = "photometry_native_v6"
    code_version: str = "photometry_psf_wls_v6_nozodi_failclosed_bkgfallback"
    background: PhotometryBackgroundConfig = Field(default_factory=PhotometryBackgroundConfig)
    masks: PhotometryMasksConfig = Field(default_factory=PhotometryMasksConfig)
    psf: PhotometryPsfConfig = Field(default_factory=PhotometryPsfConfig)
    fit: PhotometryFitConfig = Field(default_factory=PhotometryFitConfig)
    deblending: PhotometryDeblendingConfig = Field(default_factory=PhotometryDeblendingConfig)
    qa: PhotometryQaConfig = Field(default_factory=PhotometryQaConfig)
    extended: PhotometryExtendedConfig = Field(default_factory=PhotometryExtendedConfig)
    cleanup: PhotometryCleanupConfig = Field(default_factory=PhotometryCleanupConfig)

    @model_validator(mode="after")
    def validate_photometry(self) -> "PhotometryConfig":
        if self.fit_box_pixels < 5:
            raise ValueError("photometry.fit_box_pixels must be at least 5")
        if self.psf_template_radius_pixels < 2:
            raise ValueError("photometry.psf_template_radius_pixels must be at least 2")
        if self.detection_snr_threshold <= 0:
            raise ValueError("photometry.detection_snr_threshold must be positive")
        if self.qa_level not in {"minimal", "standard", "full"}:
            raise ValueError("photometry.qa_level must be one of minimal, standard, full")
        return self


class WorkflowConfig(StrictConfigModel):
    source_chunk_size: int = 100
    download_missing: bool = True
    skip_valid_measurements: bool = True
    regenerate_missing_outputs: bool = True

    @model_validator(mode="after")
    def validate_workflow(self) -> "WorkflowConfig":
        if self.source_chunk_size < 1:
            raise ValueError("workflow.source_chunk_size must be at least 1")
        return self


class RuntimeConfig(StrictConfigModel):
    max_source_workers: int = 64
    max_download_workers: int = 32
    max_fit_workers: int = 32
    max_inflight_cutouts: int = 512
    max_live_cutout_gb: float = 10.0
    max_open_fits_files: int = 512
    max_image_workers_per_source: int = 432
    global_max_network_requests: int = 2048
    global_max_open_fits_files: int = 512
    event_flush_interval_sec: float = 5.0
    sqlite_writer: str = "manager"

    @model_validator(mode="after")
    def validate_runtime(self) -> "RuntimeConfig":
        for name in [
            "max_source_workers",
            "max_download_workers",
            "max_fit_workers",
            "max_inflight_cutouts",
            "max_open_fits_files",
            "max_image_workers_per_source",
            "global_max_network_requests",
            "global_max_open_fits_files",
        ]:
            if getattr(self, name) < 1:
                raise ValueError(f"runtime.{name} must be at least 1")
        if self.max_live_cutout_gb < 0:
            raise ValueError("runtime.max_live_cutout_gb must be non-negative")
        if self.event_flush_interval_sec <= 0:
            raise ValueError("runtime.event_flush_interval_sec must be positive")
        if self.max_fit_workers > self.max_open_fits_files:
            raise ValueError("runtime.max_fit_workers must not exceed runtime.max_open_fits_files")
        if self.sqlite_writer not in {"manager", "queue"}:
            raise ValueError("runtime.sqlite_writer must be manager or queue")
        return self


class CleanupConfig(StrictConfigModel):
    cutouts: str = "success-after-source"
    keep_failed_cutouts: bool = True

    @model_validator(mode="after")
    def validate_cleanup(self) -> "CleanupConfig":
        allowed = {"never", "success-after-measurement", "success-after-source", "success-after-run"}
        if self.cutouts not in allowed:
            raise ValueError(f"cleanup.cutouts must be one of {', '.join(sorted(allowed))}")
        return self


class Config(StrictConfigModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    spherex: SpherexConfig = Field(default_factory=SpherexConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    cutouts: CutoutsConfig = Field(default_factory=CutoutsConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    exports: ExportsConfig = Field(default_factory=ExportsConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    photometry: PhotometryConfig = Field(default_factory=PhotometryConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)


def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _resolve_project_path(project_root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (project_root / value)


def normalize_paths(config: Config, project: Path | None = None) -> Config:
    root = Path(project) if project is not None else Path(config.project.root)
    root = root.expanduser().resolve()
    data = config.model_dump()
    data["project"]["root"] = root
    for key in ["database_path", "data_root", "manifest_root", "log_root", "cache_root"]:
        data["project"][key] = _resolve_project_path(root, Path(data["project"][key]))
    data["catalog"]["path"] = _resolve_project_path(root, Path(data["catalog"]["path"]))
    if "calibration" in data and "cache_root" in data["calibration"]:
        data["calibration"]["cache_root"] = _resolve_project_path(root, Path(data["calibration"]["cache_root"]))
    if "photometry" in data and "output_root" in data["photometry"]:
        data["photometry"]["output_root"] = _resolve_project_path(root, Path(data["photometry"]["output_root"]))
    return Config.model_validate(data)


def config_to_canonical_json(config: Config) -> str:
    payload = config.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def config_hash(config: Config) -> str:
    return hashlib.sha256(config_to_canonical_json(config).encode("utf-8")).hexdigest()


def load_config(
    project: Path | str | None = None,
    config_path: Path | str | dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    if isinstance(config_path, dict) and overrides is None:
        overrides = config_path
        config_path = None
    project_path = Path(project or ".").expanduser().resolve()
    cfg_path = Path(config_path) if config_path else project_path / "spherex_cutoutdb.yaml"
    cfg_path = cfg_path.expanduser()
    if not cfg_path.is_absolute():
        cfg_path = (Path.cwd() / cfg_path).resolve()
    if not cfg_path.exists():
        raise ConfigError(f"config file does not exist: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if overrides:
        data = _deep_update(data, overrides)
    try:
        config = Config.model_validate(data)
        return normalize_paths(config, project_path)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def default_config_dict(
    project: Path,
    catalog_path: Path | None = None,
    default_cutout_size_arcsec: float = 180.0,
    include_deep: bool = True,
    *,
    target_id_column: str | None = None,
    ra_column: str | None = None,
    dec_column: str | None = None,
) -> dict[str, Any]:
    collections = ["spherex_qr2"]
    if include_deep:
        collections.append("spherex_qr2_deep")
    project_name = project.name or "spherex_cutoutdb"
    data = Config().model_dump(mode="json")
    catalog_mapping = {
        "path": str(catalog_path or Path("catalog/sources.csv")),
        "source_id_column": "source_id",
        "source_name_column": "source_name",
        "ra_column": "ra_deg",
        "dec_column": "dec_deg",
        "ra_unit": "deg",
        "dec_unit": "deg",
        "allow_missing_name": True,
        "allow_duplicate_target_ids": False,
        "generate_missing_source_id": True,
        "duplicate_position_tolerance_arcsec": 0.2,
        "optional_columns": {
            "cutout_size_arcsec": "cutout_size_arcsec",
            "source_type": "source_type",
            "priority": "priority",
            "active": "active",
            "notes": "notes",
        },
    }
    if target_id_column:
        catalog_mapping["source_id_column"] = target_id_column
        catalog_mapping["source_name_column"] = target_id_column
        catalog_mapping["allow_missing_name"] = False
        catalog_mapping["generate_missing_source_id"] = False
    if ra_column:
        catalog_mapping["ra_column"] = ra_column
    if dec_column:
        catalog_mapping["dec_column"] = dec_column
    data["project"] = {
        "name": project_name,
        "root": ".",
        "database_path": "db/cutoutdb.sqlite",
        "data_root": "data",
        "manifest_root": "manifests",
        "log_root": "logs",
        "cache_root": "cache",
    }
    data["catalog"] = catalog_mapping
    data["discovery"]["collections"] = collections
    data["cutouts"]["default_size_arcsec"] = default_cutout_size_arcsec
    return data


def infer_catalog_mapping(catalog_path: Path | None) -> dict[str, Any]:
    """Infer logical catalog column mappings from a catalog header.

    The inference is intentionally conservative: it only chooses common column
    names and prefers decimal-degree RA/Dec names over sexagesimal names.
    Unknown columns remain preserved through ``extra_json`` during ingestion.
    """

    if catalog_path is None or not catalog_path.exists():
        return {}
    columns = _read_catalog_columns(catalog_path)
    if not columns:
        return {}

    mapping: dict[str, Any] = {}
    source_id = _first_matching_column(columns, ["source_id", "sourceid", "Name", "name", "object_id", "objid", "ID", "id"])
    source_name = _first_matching_column(
        columns,
        ["source_name", "source", "Name", "name", "object_name", "objname", "tns_name"],
    )
    ra = _first_matching_column(
        columns,
        ["ra_deg", "RA_deg", "radeg", "RAJ2000_deg", "RAJ2000", "ra", "RA"],
    )
    dec = _first_matching_column(
        columns,
        ["dec_deg", "DEC_deg", "dedeg", "DEJ2000_deg", "DEJ2000", "dec", "DEC", "Dec"],
    )
    if source_id:
        mapping["source_id_column"] = source_id
    if source_name:
        mapping["source_name_column"] = source_name
    if ra:
        mapping["ra_column"] = ra
    if dec:
        mapping["dec_column"] = dec

    optional = {
        "cutout_size_arcsec": _first_matching_column(
            columns, ["cutout_size_arcsec", "cutout_arcsec", "size_arcsec", "cutout_size"]
        )
        or "cutout_size_arcsec",
        "source_type": _first_matching_column(
            columns, ["source_type", "Obj. Type", "object_type", "obj_type", "type", "Type"]
        )
        or "source_type",
        "priority": _first_matching_column(columns, ["priority", "Priority"]) or "priority",
        "active": _first_matching_column(columns, ["active", "Active"]) or "active",
        "notes": _first_matching_column(columns, ["notes", "Notes", "Remarks", "remarks", "comment", "comments"])
        or "notes",
    }
    mapping["optional_columns"] = optional
    return mapping


def _read_catalog_columns(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            return list(pd.read_csv(path, nrows=0).columns)
        if suffix == ".parquet":
            import pyarrow.parquet as pq

            return list(pq.read_schema(path).names)
        if suffix in {".ecsv", ".fits", ".fit", ".fts"}:
            from astropy.table import Table

            return list(Table.read(path).colnames)
    except Exception:
        return []
    return []


def _first_matching_column(columns: list[str], candidates: list[str]) -> str | None:
    exact = {column: column for column in columns}
    folded = {_fold_column(column): column for column in columns}
    for candidate in candidates:
        if candidate in exact:
            return exact[candidate]
    for candidate in candidates:
        found = folded.get(_fold_column(candidate))
        if found:
            return found
    return None


def _fold_column(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _resolve_catalog_for_config(project_path: Path, catalog_path: Path | str | None) -> tuple[Path | None, Path | None]:
    if catalog_path is None:
        return None, None
    raw = Path(catalog_path).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        cwd_candidate = (Path.cwd() / raw).resolve()
        project_candidate = (project_path / raw).resolve()
        resolved = project_candidate if project_candidate.exists() else cwd_candidate
    try:
        config_value = resolved.relative_to(project_path)
    except ValueError:
        config_value = resolved
    return config_value, resolved


def write_default_config(
    project: Path | str,
    catalog_path: Path | str | None = None,
    *,
    force: bool = False,
    default_cutout_size_arcsec: float = 180.0,
    include_deep: bool = True,
    target_id_column: str | None = None,
    ra_column: str | None = None,
    dec_column: str | None = None,
) -> Path:
    project_path = Path(project).expanduser().resolve()
    project_path.mkdir(parents=True, exist_ok=True)
    config_path = project_path / "spherex_cutoutdb.yaml"
    if config_path.exists() and not force:
        raise ConfigError(f"config already exists: {config_path}")
    cat, resolved_cat = _resolve_catalog_for_config(project_path, catalog_path)
    data = default_config_dict(
        project_path,
        cat,
        default_cutout_size_arcsec,
        include_deep,
        target_id_column=target_id_column,
        ra_column=ra_column,
        dec_column=dec_column,
    )
    inferred = infer_catalog_mapping(resolved_cat)
    if inferred:
        data["catalog"].update(inferred)
    if target_id_column:
        data["catalog"]["source_id_column"] = target_id_column
        data["catalog"]["source_name_column"] = target_id_column
        data["catalog"]["allow_missing_name"] = False
        data["catalog"]["generate_missing_source_id"] = False
    if ra_column:
        data["catalog"]["ra_column"] = ra_column
    if dec_column:
        data["catalog"]["dec_column"] = dec_column
    Config.model_validate(data)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False)
    return config_path


def ensure_project_directories(config: Config) -> None:
    roots = [
        config.project.root / "catalog" / "versions",
        config.project.database_path.parent,
        config.project.data_root / "cutouts",
        config.project.data_root / "partial",
        config.project.data_root / "quarantine",
        config.project.data_root / "archive",
        config.project.manifest_root / "runs",
        config.project.log_root / "runs",
        config.project.cache_root / "sia",
        config.project.cache_root / "tap",
        config.calibration.cache_root,
        config.calibration.cache_root / ".locks",
        config.project.cache_root / "photometry_scratch",
        config.photometry.output_root / "spectra",
        config.photometry.output_root / "plots",
        config.photometry.output_root / "qa",
        config.photometry.output_root / "provenance",
        config.photometry.output_root / "summaries",
        config.project.root / "provenance" / "config_snapshots",
        config.project.root / "runs",
    ]
    for path in roots:
        path.mkdir(parents=True, exist_ok=True)


def validate_effective_config(config: Config, *, check_catalog: bool = True) -> list[str]:
    """Return operator-facing validation errors for a resolved config."""

    errors: list[str] = []
    if config.runtime.max_fit_workers > config.runtime.max_open_fits_files:
        errors.append("runtime.max_fit_workers must not exceed runtime.max_open_fits_files")
    if config.cleanup.cutouts != "never" and config.runtime.max_live_cutout_gb <= 0:
        errors.append("runtime.max_live_cutout_gb must be positive when cleanup.cutouts is enabled")

    cutout_root = (config.project.data_root / "cutouts").resolve()
    try:
        cutout_root.relative_to(config.project.root)
    except ValueError:
        errors.append("project.data_root/cutouts must remain inside the project root for cleanup safety")

    if check_catalog:
        errors.extend(_validate_catalog_file(config))
    return errors


def _validate_catalog_file(config: Config) -> list[str]:
    path = config.catalog.path
    if not path.exists():
        return [f"catalog.path does not exist: {path}"]
    columns = _read_catalog_columns(path)
    if not columns:
        return [f"could not read catalog columns from: {path}"]

    errors: list[str] = []
    for logical, column in [
        ("target ID", config.catalog.source_id_column),
        ("source name", config.catalog.source_name_column),
        ("RA", config.catalog.ra_column),
        ("Dec", config.catalog.dec_column),
    ]:
        if column in columns:
            continue
        if logical == "source name" and config.catalog.allow_missing_name:
            continue
        errors.append(f"catalog is missing configured {logical} column: {column}")

    if config.catalog.source_id_column == "Name" and "Name" not in columns:
        errors.append("target ID column is configured as Name but the catalog has no Name column")

    if config.catalog.source_id_column in columns and not config.catalog.allow_duplicate_target_ids:
        try:
            values = _read_catalog_id_values(path, config.catalog.source_id_column)
        except Exception as exc:  # noqa: BLE001 - convert to config validation message
            errors.append(f"could not read catalog target IDs from {path}: {exc}")
        else:
            normalized = [None if value is None or pd.isna(value) else str(value).strip() for value in values]
            if any(not value for value in normalized):
                errors.append(f"catalog target ID column {config.catalog.source_id_column} has missing values")
            present = [value for value in normalized if value]
            duplicates = sorted({value for value in present if present.count(value) > 1})
            if duplicates:
                errors.append(
                    f"catalog target ID column {config.catalog.source_id_column} has duplicate value(s): "
                    + ", ".join(duplicates[:12])
                )
    return errors


def _read_catalog_id_values(path: Path, column: str) -> list[Any]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, usecols=[column])[column].tolist()
    if suffix == ".parquet":
        return pd.read_parquet(path, columns=[column])[column].tolist()
    if suffix in {".ecsv", ".fits", ".fit", ".fts"}:
        from astropy.table import Table

        return Table.read(path)[column].tolist()
    raise ConfigError(f"unsupported catalog format: {path.suffix}")
