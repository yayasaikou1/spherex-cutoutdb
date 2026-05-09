# DATA_MODEL.md

## Data-model objective

The local data model must preserve all metadata needed to trace a SPHEREx Level-2 Spectral Image MEF cutout back to its parent IRSA product, while supporting catalog updates, duplicate detection, retries, validation, and manifest export.

Use SQLite for authoritative state. CSV/ECSV/Parquet manifests are exports, not the primary state.

## Source catalog schema

The input source catalog may be CSV, ECSV, FITS table, or Parquet.

### Required logical fields

| Logical field | Type | Required | Notes |
|---|---:|---:|---|
| `source_id` | string | recommended | Stable unique identifier. If absent, may be generated from `source_name` or row number when configured. |
| `source_name` | string | recommended | Human-readable name. Required if `source_id` is absent. |
| `ra_deg` | float | yes | ICRS RA in decimal degrees after normalization. |
| `dec_deg` | float | yes | ICRS Dec in decimal degrees after normalization. |

### Optional logical fields

| Logical field | Type | Notes |
|---|---:|---|
| `cutout_size_arcsec` | float | Per-source cutout size override. |
| `source_type` | string | Free label such as `point`, `galaxy`, `extended`, `unknown`. No science behavior should depend on this in MVP. |
| `priority` | int | Optional scheduling priority. |
| `active` | bool | Allows retaining retired sources in DB. |
| `notes` | string | Free-text notes. |
| `extra_json` | JSON | Preserved user metadata. |

### Normalized source rules

- RA must be finite and in `[0, 360)` degrees.
- Dec must be finite and in `[-90, 90]` degrees.
- Source IDs must be stripped, normalized to safe text, and unique among active sources.
- Near-duplicate positions should be warned, not merged automatically.
- Source row hash is computed from normalized source ID, RA, Dec, cutout size, and all configured metadata columns.

## SQLite schema overview

The following tables are recommended:

```text
schema_migrations
source_catalog_versions
sources
discovery_products
source_product_matches
download_plan
cutouts
validation_results
failures
runs
run_events
manifest_exports
processing_version_events
```

## Common column conventions

- Primary keys are integer autoincrement unless a string run ID is more convenient.
- All times are ISO-8601 UTC strings.
- JSON columns are stored as TEXT containing canonical JSON.
- Boolean columns are stored as INTEGER 0/1.
- Large raw SIA rows are stored as JSON text.
- Paths are stored relative to project root when possible, with absolute path derivable.

## `schema_migrations`

Tracks local DB schema version.

| Column | Type | Notes |
|---|---:|---|
| `version` | integer primary key | Monotonic schema version. |
| `applied_at` | text | UTC timestamp. |
| `description` | text | Migration description. |

## `source_catalog_versions`

Represents each ingested catalog snapshot.

| Column | Type | Notes |
|---|---:|---|
| `catalog_version_id` | text primary key | e.g. `cat_20260430T120000Z_ab12cd34`. |
| `path` | text | Source catalog path. |
| `normalized_path` | text | Normalized catalog snapshot path. |
| `catalog_hash` | text | SHA256 of normalized source table. |
| `n_rows_input` | integer | Raw rows read. |
| `n_rows_valid` | integer | Valid sources. |
| `n_rows_invalid` | integer | Invalid rows. |
| `ingested_at` | text | UTC timestamp. |
| `config_hash` | text | Config hash used for column mapping. |
| `run_id` | text | Ingesting run. |

## `sources`

Authoritative source table.

| Column | Type | Notes |
|---|---:|---|
| `source_pk` | integer primary key | Internal key. |
| `source_id` | text unique | Stable source ID. |
| `source_name` | text | Human-readable name. |
| `ra_deg` | real | ICRS RA. |
| `dec_deg` | real | ICRS Dec. |
| `cutout_size_arcsec` | real | Optional override. |
| `source_type` | text | Preserved label only. |
| `priority` | integer | Optional scheduling priority. |
| `active` | integer | 1 if current catalog contains source. |
| `catalog_version_id` | text | Most recent version containing this source. |
| `row_hash` | text | Hash of normalized source row. |
| `extra_json` | text | Preserved optional metadata. |
| `first_seen_at` | text | UTC timestamp. |
| `last_seen_at` | text | UTC timestamp. |
| `retired_at` | text | UTC timestamp when absent from latest catalog. |

Indexes:

```sql
CREATE UNIQUE INDEX idx_sources_source_id ON sources(source_id);
CREATE INDEX idx_sources_active ON sources(active);
CREATE INDEX idx_sources_radec ON sources(ra_deg, dec_deg);
```

## `discovery_products`

One row per discovered parent SPHEREx product after SIA2 normalization. Multiple sources may map to the same parent product.

| Column | Type | Notes |
|---|---:|---|
| `product_id` | integer primary key | Internal product key. |
| `collection` | text | Queried SIA2 collection, e.g. `spherex_qr2`. |
| `obs_collection` | text | SIA result column if present. |
| `obs_publisher_did` | text | SIA/ObsCore identifier. |
| `obs_id` | text | SIA observation ID. |
| `observation_id` | text | Parsed SPHEREx observation ID. |
| `planning_period` | text | Parsed planning period, e.g. `2025W19_2B`. |
| `detector_id` | integer | 1 through 6 if parsed. |
| `bandpass` | text | e.g. `D3` or `SPHEREx-D3`. |
| `energy_bandpassname` | text | SIA column. |
| `em_min` | real | Wavelength lower bound in meters from SIA. |
| `em_max` | real | Wavelength upper bound in meters from SIA. |
| `em_res_power` | real | Spectral resolving power if present. |
| `t_min` | real | Observation start MJD if present. |
| `t_max` | real | Observation end MJD if present. |
| `t_exptime` | real | Exposure time if present. |
| `s_ra` | real | Product center RA from SIA. |
| `s_dec` | real | Product center Dec from SIA. |
| `s_region` | text | SIA region. |
| `s_pixel_scale` | real | Pixel scale if present. |
| `dist_to_point` | real | SIA returned distance if present. |
| `access_url` | text | Parent on-prem access URL. |
| `access_format` | text | SIA access format. |
| `access_estsize` | integer | Estimated product size if present. |
| `cloud_access_json` | text | Parsed/preserved cloud access metadata. |
| `parent_filename` | text | Basename of `access_url`. |
| `processing_version` | text | Parsed from filename/path/header when available. |
| `processing_date` | text | Parsed from filename/path/header when available. |
| `product_signature` | text | Stable hash/key for product identity. |
| `row_hash` | text | Hash of normalized discovery row. |
| `raw_sia_json` | text | Raw SIA row as JSON. |
| `first_discovered_at` | text | UTC timestamp. |
| `last_seen_at` | text | UTC timestamp. |
| `last_run_id` | text | Most recent discovery run. |

Uniqueness:

```sql
CREATE UNIQUE INDEX idx_products_signature ON discovery_products(product_signature);
CREATE INDEX idx_products_access_url ON discovery_products(access_url);
CREATE INDEX idx_products_obsdet ON discovery_products(observation_id, detector_id);
CREATE INDEX idx_products_version ON discovery_products(processing_version, processing_date);
```

Recommended `product_signature` priority:

1. `obs_publisher_did` if present plus `collection`;
2. otherwise normalized `access_url` plus `collection`;
3. include parent filename, processing version, processing date when parsed.

## `source_product_matches`

Maps sources to discovered parent products. This is the source-to-cutout planning bridge.

| Column | Type | Notes |
|---|---:|---|
| `match_id` | integer primary key | Internal key. |
| `source_id` | text | FK to `sources.source_id`. |
| `product_id` | integer | FK to `discovery_products.product_id`. |
| `collection` | text | Discovery collection. |
| `query_ra_deg` | real | RA used in SIA query. |
| `query_dec_deg` | real | Dec used in SIA query. |
| `search_radius_deg` | real | SIA POS circle radius. |
| `dist_to_point` | real | If available. |
| `coverage_status` | text | `covered`, `not_covered`, `unknown`. |
| `match_hash` | text | Unique hash of source/product/query config. |
| `active` | integer | 1 if current discovery still maps source to product. |
| `first_seen_at` | text | UTC timestamp. |
| `last_seen_at` | text | UTC timestamp. |
| `last_run_id` | text | Most recent run. |

Uniqueness:

```sql
CREATE UNIQUE INDEX idx_matches_unique ON source_product_matches(source_id, product_id, search_radius_deg);
CREATE INDEX idx_matches_source ON source_product_matches(source_id);
CREATE INDEX idx_matches_product ON source_product_matches(product_id);
```

## `download_plan`

Rows generated by `plan`. This table is run-specific and records planned actions.

| Column | Type | Notes |
|---|---:|---|
| `plan_id` | integer primary key | Internal key. |
| `run_id` | text | Planning run. |
| `source_id` | text | Source. |
| `product_id` | integer | Parent product. |
| `match_id` | integer | Source-product match. |
| `cutout_key` | text | Unique desired cutout key. |
| `cutout_ra_deg` | real | Center used for cutout. |
| `cutout_dec_deg` | real | Center used for cutout. |
| `cutout_size_arcsec` | real | Requested size. |
| `cutout_size_deg` | real | URL size value. |
| `cutout_url` | text | URL actually planned. |
| `local_path` | text | Final local path. |
| `access_method` | text | `onprem_cutout`, `cloud_full_product`, `cloud_cutout`, etc. |
| `action` | text | Planned action. |
| `reason` | text | Human-readable action reason. |
| `existing_cutout_id` | integer | Existing matching file, if any. |
| `priority` | integer | Optional scheduling priority. |
| `created_at` | text | UTC timestamp. |

Actions:

```text
download
skip_valid
validate_existing
redownload_invalid
redownload_processing_update
skip_duplicate
fail_no_access_url
fail_no_official_cloud_cutout
```

Indexes:

```sql
CREATE INDEX idx_plan_run_action ON download_plan(run_id, action);
CREATE INDEX idx_plan_source ON download_plan(source_id);
CREATE INDEX idx_plan_cutout_key ON download_plan(cutout_key);
```

## `cutouts`

Authoritative local cutout table.

| Column | Type | Notes |
|---|---:|---|
| `cutout_id` | integer primary key | Internal key. |
| `cutout_key` | text unique | Unique desired cutout identity. |
| `source_id` | text | Source ID. |
| `product_id` | integer | Parent product. |
| `local_path` | text unique | Final local file path relative to project. |
| `file_exists` | integer | Last known existence. |
| `file_size_bytes` | integer | Local file size. |
| `sha256` | text | Local file SHA256. |
| `access_method` | text | `onprem_cutout`, etc. |
| `parent_access_url` | text | Parent MEF access URL. |
| `cloud_access_json` | text | Preserved cloud access metadata. |
| `cutout_url_used` | text | Actual URL used. |
| `parent_filename` | text | Original parent MEF filename. |
| `collection` | text | SIA collection. |
| `observation_id` | text | Parsed/header observation ID. |
| `detector_id` | integer | Detector ID. |
| `planning_period` | text | Planning period. |
| `processing_version` | text | Processing version. |
| `processing_date` | text | Processing date. |
| `bandpass` | text | Band/detector label. |
| `em_min` | real | Discovery metadata. |
| `em_max` | real | Discovery metadata. |
| `cutout_ra_deg` | real | Requested center. |
| `cutout_dec_deg` | real | Requested center. |
| `cutout_size_arcsec` | real | Requested size. |
| `download_started_at` | text | UTC timestamp. |
| `download_completed_at` | text | UTC timestamp. |
| `download_run_id` | text | Run ID. |
| `validation_status` | text | Latest validation status. |
| `validation_run_id` | text | Latest validation run. |
| `validation_time` | text | UTC timestamp. |
| `failure_reason` | text | Latest failure if any. |
| `hdu_summary_json` | text | Presence, shapes, dtypes. |
| `wcs_summary_json` | text | Spatial/spectral WCS checks. |
| `psf_metadata_json` | text | PSF shape, header keyword summary. |
| `header_metadata_json` | text | Selected header cards. |
| `superseded_by_cutout_id` | integer | Newer processing version if any. |
| `superseded_at` | text | UTC timestamp. |
| `active` | integer | 1 if current preferred cutout. |

Indexes:

```sql
CREATE UNIQUE INDEX idx_cutouts_key ON cutouts(cutout_key);
CREATE INDEX idx_cutouts_source ON cutouts(source_id);
CREATE INDEX idx_cutouts_product ON cutouts(product_id);
CREATE INDEX idx_cutouts_validation ON cutouts(validation_status);
CREATE INDEX idx_cutouts_version ON cutouts(observation_id, detector_id, processing_version, processing_date);
```

## `validation_results`

Stores every validation attempt, not only the latest state.

| Column | Type | Notes |
|---|---:|---|
| `validation_id` | integer primary key | Internal key. |
| `run_id` | text | Validation run. |
| `cutout_id` | integer | FK to cutouts. |
| `local_path` | text | File validated. |
| `status` | text | Passed/failed/warn status. |
| `reason` | text | Short reason. |
| `warnings_json` | text | Warning list. |
| `errors_json` | text | Error list. |
| `file_size_bytes` | integer | Size observed. |
| `sha256` | text | Hash observed. |
| `required_hdus_present` | integer | 0/1. |
| `image_shape` | text | e.g. `[120, 120]`. |
| `flags_shape` | text | Shape. |
| `variance_shape` | text | Shape. |
| `zodi_shape` | text | Shape. |
| `psf_shape` | text | e.g. `[121, 101, 101]` or FITS order as observed. |
| `wcwave_summary_json` | text | Table columns/shapes. |
| `spatial_wcs_valid` | integer | 0/1. |
| `spectral_wcs_valid` | integer | 0/1. |
| `validated_at` | text | UTC timestamp. |

## `failures`

All failure events, including discovery, download, validation, and export failures.

| Column | Type | Notes |
|---|---:|---|
| `failure_id` | integer primary key | Internal key. |
| `run_id` | text | Run ID. |
| `source_id` | text | Nullable. |
| `product_id` | integer | Nullable. |
| `plan_id` | integer | Nullable. |
| `cutout_id` | integer | Nullable. |
| `phase` | text | `catalog`, `discovery`, `planning`, `download`, `validation`, `export`, `summary`. |
| `status` | text | `open`, `resolved`, `ignored`, `retryable`, `nonretryable`. |
| `reason` | text | Human-readable reason. |
| `exception_class` | text | Exception type if any. |
| `exception_message` | text | Sanitized message. |
| `url` | text | Relevant URL if any. |
| `local_path` | text | Relevant file path if any. |
| `attempt` | integer | Attempt count. |
| `max_attempts` | integer | Max attempt count. |
| `created_at` | text | UTC timestamp. |
| `resolved_at` | text | UTC timestamp. |

Indexes:

```sql
CREATE INDEX idx_failures_run_phase ON failures(run_id, phase);
CREATE INDEX idx_failures_status ON failures(status);
CREATE INDEX idx_failures_source ON failures(source_id);
```

## `runs`

Top-level command execution records.

| Column | Type | Notes |
|---|---:|---|
| `run_id` | text primary key | e.g. `run_20260430T120000Z_ab12cd34`. |
| `command` | text | CLI command. |
| `args_json` | text | CLI args. |
| `config_hash` | text | Config hash. |
| `config_snapshot_path` | text | Saved config snapshot. |
| `package_version` | text | Package version. |
| `python_version` | text | Python version. |
| `dependency_versions_json` | text | Key dependency versions. |
| `started_at` | text | UTC timestamp. |
| `finished_at` | text | UTC timestamp. |
| `status` | text | `running`, `success`, `partial_success`, `failed`. |
| `counts_json` | text | Summary counts. |
| `summary_path` | text | Run summary JSON path. |

## `run_events`

Append-only structured run log.

| Column | Type | Notes |
|---|---:|---|
| `event_id` | integer primary key | Internal key. |
| `run_id` | text | Run ID. |
| `event_time` | text | UTC timestamp. |
| `event_type` | text | Event type. |
| `source_id` | text | Nullable. |
| `product_id` | integer | Nullable. |
| `cutout_id` | integer | Nullable. |
| `message` | text | Concise human message. |
| `payload_json` | text | Structured details. |

## `manifest_exports`

Records exported manifest files.

| Column | Type | Notes |
|---|---:|---|
| `export_id` | integer primary key | Internal key. |
| `run_id` | text | Run ID. |
| `table_name` | text | Source table or view. |
| `format` | text | `csv`, `ecsv`, `parquet`, `json`. |
| `path` | text | Output path. |
| `n_rows` | integer | Rows exported. |
| `sha256` | text | Manifest file hash. |
| `exported_at` | text | UTC timestamp. |

## `processing_version_events`

Tracks changed/reprocessed parent products.

| Column | Type | Notes |
|---|---:|---|
| `event_id` | integer primary key | Internal key. |
| `source_id` | text | Source affected, nullable for global product change. |
| `old_product_id` | integer | Older parent product. |
| `new_product_id` | integer | Newer parent product. |
| `old_processing_version` | text | Old version. |
| `new_processing_version` | text | New version. |
| `old_processing_date` | text | Old date. |
| `new_processing_date` | text | New date. |
| `policy_applied` | text | `keep_all_mark_superseded`, `replace`, `ignore`, etc. |
| `created_at` | text | UTC timestamp. |
| `run_id` | text | Run detecting event. |

## Manifest schemas

### `latest_sources.csv`

| Column | Notes |
|---|---|
| `source_id` | Stable ID. |
| `source_name` | Name. |
| `ra_deg` | RA. |
| `dec_deg` | Dec. |
| `cutout_size_arcsec` | Effective size. |
| `active` | Current status. |
| `row_hash` | Source hash. |
| `catalog_version_id` | Latest catalog version. |

### `latest_discovery.parquet`

One row per current `source_product_matches` joined to `discovery_products`.

Required columns:

```text
source_id, source_name, source_ra_deg, source_dec_deg,
collection, obs_collection, obs_id, observation_id, planning_period,
detector_id, bandpass, energy_bandpassname, em_min, em_max,
t_min, t_max, access_url, access_format, access_estsize,
cloud_access_json, parent_filename, processing_version, processing_date,
product_signature, discovered_at
```

### `latest_download_plan.parquet`

One row per plan action for latest run.

Required columns:

```text
run_id, source_id, product_id, cutout_key, action, reason,
cutout_ra_deg, cutout_dec_deg, cutout_size_arcsec, cutout_url,
local_path, access_method, existing_cutout_id
```

### `latest_cutouts.parquet`

One row per local cutout record.

Required columns:

```text
source_id, product_id, cutout_key, local_path, file_size_bytes, sha256,
validation_status, failure_reason, parent_filename, parent_access_url,
cloud_access_json, cutout_url_used, collection, observation_id,
detector_id, planning_period, processing_version, processing_date,
bandpass, em_min, em_max, cutout_ra_deg, cutout_dec_deg,
cutout_size_arcsec, download_completed_at, validation_time,
hdu_summary_json, wcs_summary_json, psf_metadata_json
```

### `latest_failures.csv`

Required columns:

```text
run_id, source_id, product_id, plan_id, cutout_id, phase, status,
reason, exception_class, exception_message, url, local_path,
attempt, max_attempts, created_at, resolved_at
```

### `latest_source_coverage.csv`

Computed view for user summaries.

Required columns:

```text
source_id, source_name, ra_deg, dec_deg,
n_discovered_parent_mefs, n_valid_cutouts, n_failed_cutouts,
n_detectors, detectors, n_planning_periods, n_processing_versions,
em_min_min, em_max_max, first_t_min, last_t_max,
coverage_status
```

## File naming convention

Directory:

```text
data/cutouts/{source_slug}/{collection}/{planning_period}/{processing_version}/D{detector}/
```

Filename:

```text
{source_slug}__{parent_stem}__sz{size_arcsec:0.1f}as__{cutout_key8}.fits
```

Example:

```text
data/cutouts/M101/spherex_qr2/2025W19_2B/l2b-v20-2025-247/D3/
  M101__level2_2025W19_2B_0073_2D3_spx_l2b-v20-2025-247__sz360.0as__a1b2c3d4.fits
```

The DB, not the filename, is authoritative. The filename is intended to be readable and deterministic.

## Cutout key

`cutout_key` should be a SHA1 or SHA256-derived identifier from canonical JSON containing:

```json
{
  "source_id": "M101",
  "product_signature": "...",
  "cutout_ra_deg": 210.80227,
  "cutout_dec_deg": 54.34895,
  "cutout_size_arcsec": 360.0,
  "access_method": "onprem_cutout"
}
```

Round RA/Dec and size to a documented precision before hashing, e.g. RA/Dec to 1e-8 deg and size to 1e-4 arcsec.

## Duplicate detection

### Source duplicates

- Duplicate `source_id`: error unless `--allow-update-same-id` is active and row hash changed.
- Near-duplicate positions within `duplicate_position_tolerance_arcsec`: warning with both source IDs.
- Do not merge near-duplicates automatically.

### Discovery duplicates

- Same `product_signature`: update existing discovery row and `last_seen_at`.
- Same `access_url` across different collections: preserve both collection mappings but mark parent file duplicate in coverage summaries.
- Same observation/detector with new processing version/date: create `processing_version_events` row.

### Cutout duplicates

- Same `cutout_key`: same desired local product; skip if valid.
- Same SHA256 but different path: record as duplicate file content; do not automatically delete.
- Same source/product but different cutout size: separate cutout records.

## Changed-processing-version handling

Default policy: `keep_all_mark_superseded`.

When a new SIA2 discovery row represents the same observation/detector but a different processing version/date:

1. upsert the new parent product;
2. create a `processing_version_events` row;
3. plan a new cutout for the new product;
4. keep existing cutout files;
5. mark old cutouts as `active=0` and set `superseded_by_cutout_id` after the new cutout validates;
6. include both old and new cutouts in exported cutout manifest unless `--active-only` is requested.

Alternative policies:

- `keep_all_no_supersede`: preserve all versions and keep all active.
- `replace_archive_old`: move older files to `data/archive/` after new validation.
- `ignore_new_versions`: do not download new versions; record warning.

## PSF metadata preservation

For each cutout, store:

```text
psf_hdu_present
psf_shape
psf_dtype
psf_header_version_keywords
psf_xctr_keyword_count
psf_yctr_keyword_count
psf_header_hash
known_psf_header_issue_status
```

Do not modify the PSF HDU. If a known historical PSF-header issue is detected, record a validation warning only. Do not repair headers in this package unless the user explicitly requests a future non-default `metadata_repair_report` mode that writes no modified FITS products.
