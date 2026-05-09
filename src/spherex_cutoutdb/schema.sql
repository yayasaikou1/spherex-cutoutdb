PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_catalog_versions (
  catalog_version_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  normalized_path TEXT,
  catalog_hash TEXT NOT NULL,
  n_rows_input INTEGER NOT NULL,
  n_rows_valid INTEGER NOT NULL,
  n_rows_invalid INTEGER NOT NULL,
  ingested_at TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  run_id TEXT
);

CREATE TABLE IF NOT EXISTS sources (
  source_pk INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL UNIQUE,
  source_name TEXT,
  ra_deg REAL NOT NULL,
  dec_deg REAL NOT NULL,
  cutout_size_arcsec REAL,
  source_type TEXT,
  priority INTEGER,
  active INTEGER NOT NULL DEFAULT 1,
  catalog_version_id TEXT,
  row_hash TEXT NOT NULL,
  extra_json TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  retired_at TEXT,
  FOREIGN KEY(catalog_version_id) REFERENCES source_catalog_versions(catalog_version_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_source_id ON sources(source_id);
CREATE INDEX IF NOT EXISTS idx_sources_active ON sources(active);
CREATE INDEX IF NOT EXISTS idx_sources_radec ON sources(ra_deg, dec_deg);

CREATE TABLE IF NOT EXISTS discovery_products (
  product_id INTEGER PRIMARY KEY AUTOINCREMENT,
  collection TEXT NOT NULL,
  obs_collection TEXT,
  obs_publisher_did TEXT,
  obs_id TEXT,
  observation_id TEXT,
  planning_period TEXT,
  detector_id INTEGER,
  bandpass TEXT,
  energy_bandpassname TEXT,
  em_min REAL,
  em_max REAL,
  em_res_power REAL,
  t_min REAL,
  t_max REAL,
  t_exptime REAL,
  s_ra REAL,
  s_dec REAL,
  s_region TEXT,
  s_pixel_scale REAL,
  dist_to_point REAL,
  access_url TEXT,
  access_format TEXT,
  access_estsize INTEGER,
  cloud_access_json TEXT,
  parent_filename TEXT,
  processing_version TEXT,
  processing_date TEXT,
  product_signature TEXT NOT NULL UNIQUE,
  row_hash TEXT NOT NULL,
  raw_sia_json TEXT NOT NULL,
  first_discovered_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_run_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_products_signature ON discovery_products(product_signature);
CREATE INDEX IF NOT EXISTS idx_products_access_url ON discovery_products(access_url);
CREATE INDEX IF NOT EXISTS idx_products_obsdet ON discovery_products(observation_id, detector_id);
CREATE INDEX IF NOT EXISTS idx_products_version ON discovery_products(processing_version, processing_date);

CREATE TABLE IF NOT EXISTS source_product_matches (
  match_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL,
  product_id INTEGER,
  collection TEXT NOT NULL,
  query_ra_deg REAL NOT NULL,
  query_dec_deg REAL NOT NULL,
  search_radius_deg REAL NOT NULL,
  dist_to_point REAL,
  coverage_status TEXT NOT NULL,
  match_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_run_id TEXT,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(product_id) REFERENCES discovery_products(product_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_unique ON source_product_matches(source_id, product_id, search_radius_deg);
CREATE INDEX IF NOT EXISTS idx_matches_source ON source_product_matches(source_id);
CREATE INDEX IF NOT EXISTS idx_matches_product ON source_product_matches(product_id);

CREATE TABLE IF NOT EXISTS download_plan (
  plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  source_id TEXT NOT NULL,
  product_id INTEGER,
  match_id INTEGER,
  cutout_key TEXT NOT NULL,
  cutout_ra_deg REAL NOT NULL,
  cutout_dec_deg REAL NOT NULL,
  cutout_size_arcsec REAL NOT NULL,
  cutout_size_deg REAL NOT NULL,
  cutout_url TEXT,
  local_path TEXT NOT NULL,
  access_method TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT,
  existing_cutout_id INTEGER,
  priority INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(product_id) REFERENCES discovery_products(product_id),
  FOREIGN KEY(match_id) REFERENCES source_product_matches(match_id),
  FOREIGN KEY(existing_cutout_id) REFERENCES cutouts(cutout_id)
);

CREATE INDEX IF NOT EXISTS idx_plan_run_action ON download_plan(run_id, action);
CREATE INDEX IF NOT EXISTS idx_plan_source ON download_plan(source_id);
CREATE INDEX IF NOT EXISTS idx_plan_cutout_key ON download_plan(cutout_key);

CREATE TABLE IF NOT EXISTS cutouts (
  cutout_id INTEGER PRIMARY KEY AUTOINCREMENT,
  cutout_key TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  product_id INTEGER,
  local_path TEXT NOT NULL UNIQUE,
  file_exists INTEGER NOT NULL DEFAULT 0,
  file_size_bytes INTEGER,
  sha256 TEXT,
  access_method TEXT NOT NULL,
  parent_access_url TEXT,
  cloud_access_json TEXT,
  cutout_url_used TEXT,
  parent_filename TEXT,
  collection TEXT,
  observation_id TEXT,
  detector_id INTEGER,
  planning_period TEXT,
  processing_version TEXT,
  processing_date TEXT,
  bandpass TEXT,
  em_min REAL,
  em_max REAL,
  cutout_ra_deg REAL,
  cutout_dec_deg REAL,
  cutout_size_arcsec REAL,
  download_started_at TEXT,
  download_completed_at TEXT,
  download_run_id TEXT,
  validation_status TEXT,
  validation_run_id TEXT,
  validation_time TEXT,
  failure_reason TEXT,
  hdu_summary_json TEXT,
  wcs_summary_json TEXT,
  psf_metadata_json TEXT,
  header_metadata_json TEXT,
  superseded_by_cutout_id INTEGER,
  superseded_at TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(product_id) REFERENCES discovery_products(product_id),
  FOREIGN KEY(superseded_by_cutout_id) REFERENCES cutouts(cutout_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cutouts_key ON cutouts(cutout_key);
CREATE INDEX IF NOT EXISTS idx_cutouts_source ON cutouts(source_id);
CREATE INDEX IF NOT EXISTS idx_cutouts_product ON cutouts(product_id);
CREATE INDEX IF NOT EXISTS idx_cutouts_validation ON cutouts(validation_status);
CREATE INDEX IF NOT EXISTS idx_cutouts_version ON cutouts(observation_id, detector_id, processing_version, processing_date);

CREATE TABLE IF NOT EXISTS validation_results (
  validation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  cutout_id INTEGER,
  local_path TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  warnings_json TEXT,
  errors_json TEXT,
  file_size_bytes INTEGER,
  sha256 TEXT,
  required_hdus_present INTEGER,
  image_shape TEXT,
  flags_shape TEXT,
  variance_shape TEXT,
  zodi_shape TEXT,
  psf_shape TEXT,
  wcwave_summary_json TEXT,
  spatial_wcs_valid INTEGER,
  spectral_wcs_valid INTEGER,
  validated_at TEXT NOT NULL,
  FOREIGN KEY(cutout_id) REFERENCES cutouts(cutout_id)
);

CREATE TABLE IF NOT EXISTS failures (
  failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  source_id TEXT,
  product_id INTEGER,
  plan_id INTEGER,
  cutout_id INTEGER,
  phase TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  exception_class TEXT,
  exception_message TEXT,
  url TEXT,
  local_path TEXT,
  attempt INTEGER,
  max_attempts INTEGER,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(product_id) REFERENCES discovery_products(product_id),
  FOREIGN KEY(plan_id) REFERENCES download_plan(plan_id),
  FOREIGN KEY(cutout_id) REFERENCES cutouts(cutout_id)
);

CREATE INDEX IF NOT EXISTS idx_failures_run_phase ON failures(run_id, phase);
CREATE INDEX IF NOT EXISTS idx_failures_status ON failures(status);
CREATE INDEX IF NOT EXISTS idx_failures_source ON failures(source_id);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  args_json TEXT,
  config_hash TEXT,
  config_snapshot_path TEXT,
  package_version TEXT,
  python_version TEXT,
  dependency_versions_json TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  counts_json TEXT,
  summary_path TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  event_time TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source_id TEXT,
  product_id INTEGER,
  cutout_id INTEGER,
  message TEXT,
  payload_json TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS manifest_exports (
  export_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  table_name TEXT NOT NULL,
  format TEXT NOT NULL,
  path TEXT NOT NULL,
  n_rows INTEGER NOT NULL,
  sha256 TEXT,
  exported_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS processing_version_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT,
  old_product_id INTEGER,
  new_product_id INTEGER,
  old_processing_version TEXT,
  new_processing_version TEXT,
  old_processing_date TEXT,
  new_processing_date TEXT,
  policy_applied TEXT,
  created_at TEXT NOT NULL,
  run_id TEXT,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(old_product_id) REFERENCES discovery_products(product_id),
  FOREIGN KEY(new_product_id) REFERENCES discovery_products(product_id)
);

CREATE TABLE IF NOT EXISTS calibration_products (
  calibration_id TEXT PRIMARY KEY,
  release TEXT NOT NULL,
  product_type TEXT NOT NULL,
  detector_id INTEGER,
  calibration_version TEXT,
  processing_date TEXT,
  filename TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  source_url TEXT,
  file_size_bytes INTEGER,
  sha256 TEXT,
  validation_status TEXT NOT NULL,
  validation_reason TEXT,
  hdu_summary_json TEXT,
  header_metadata_json TEXT,
  first_seen_at TEXT NOT NULL,
  last_validated_at TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_calibration_lookup
ON calibration_products(release, product_type, detector_id, calibration_version, processing_date, validation_status);

CREATE TABLE IF NOT EXISTS photometry_runs (
  photometry_run_id TEXT PRIMARY KEY,
  run_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  code_version TEXT NOT NULL,
  output_schema_version TEXT NOT NULL,
  counts_json TEXT,
  summary_path TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS photometry_work_items (
  work_item_id TEXT PRIMARY KEY,
  photometry_run_id TEXT,
  source_id TEXT NOT NULL,
  product_id INTEGER,
  cutout_key TEXT NOT NULL,
  cutout_id INTEGER,
  measurement_id TEXT NOT NULL,
  state TEXT NOT NULL,
  reason TEXT,
  work_key_json TEXT NOT NULL,
  source_row_hash TEXT,
  cutout_sha256 TEXT,
  validation_status TEXT,
  spectral_wcs_calibration_id TEXT,
  solid_angle_calibration_id TEXT,
  photometry_config_hash TEXT NOT NULL,
  code_version TEXT NOT NULL,
  output_schema_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(photometry_run_id) REFERENCES photometry_runs(photometry_run_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(product_id) REFERENCES discovery_products(product_id),
  FOREIGN KEY(cutout_id) REFERENCES cutouts(cutout_id),
  FOREIGN KEY(spectral_wcs_calibration_id) REFERENCES calibration_products(calibration_id),
  FOREIGN KEY(solid_angle_calibration_id) REFERENCES calibration_products(calibration_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_photometry_work_measurement
ON photometry_work_items(measurement_id);
CREATE INDEX IF NOT EXISTS idx_photometry_work_state
ON photometry_work_items(state);
CREATE INDEX IF NOT EXISTS idx_photometry_work_source
ON photometry_work_items(source_id);

CREATE TABLE IF NOT EXISTS photometry_measurements (
  measurement_id TEXT PRIMARY KEY,
  work_item_id TEXT,
  photometry_run_id TEXT,
  source_id TEXT NOT NULL,
  product_id INTEGER,
  cutout_id INTEGER,
  cutout_key TEXT NOT NULL,
  cutout_sha256 TEXT,
  wavelength_um REAL,
  bandwidth_um REAL,
  point_flux_uJy REAL,
  point_flux_err_uJy REAL,
  joint_flux_uJy REAL,
  joint_flux_err_uJy REAL,
  selected_flux_uJy REAL,
  selected_flux_err_uJy REAL,
  selected_snr REAL,
  science_mode TEXT,
  science_recommended INTEGER NOT NULL,
  detection_status TEXT NOT NULL,
  photometry_flags TEXT,
  image_flags INTEGER,
  fit_quality REAL,
  chi2_reduced REAL,
  n_valid_pixels INTEGER,
  background_uJy_per_pixel REAL,
  background_unc_uJy_per_pixel REAL,
  deblend_status TEXT,
  n_neighbors INTEGER,
  calibration_exact_match INTEGER,
  spectral_wcs_calibration_id TEXT,
  solid_angle_calibration_id TEXT,
  output_schema_version TEXT NOT NULL,
  photometry_config_hash TEXT NOT NULL,
  code_version TEXT NOT NULL,
  row_json TEXT NOT NULL,
  provenance_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES photometry_work_items(work_item_id),
  FOREIGN KEY(photometry_run_id) REFERENCES photometry_runs(photometry_run_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(product_id) REFERENCES discovery_products(product_id),
  FOREIGN KEY(cutout_id) REFERENCES cutouts(cutout_id)
);

CREATE INDEX IF NOT EXISTS idx_photometry_measurements_source
ON photometry_measurements(source_id);
CREATE INDEX IF NOT EXISTS idx_photometry_measurements_science
ON photometry_measurements(science_recommended, detection_status);

CREATE TABLE IF NOT EXISTS photometry_failures (
  photometry_failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
  photometry_run_id TEXT,
  work_item_id TEXT,
  source_id TEXT,
  product_id INTEGER,
  cutout_id INTEGER,
  failure_type TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  exception_class TEXT,
  traceback TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY(photometry_run_id) REFERENCES photometry_runs(photometry_run_id),
  FOREIGN KEY(work_item_id) REFERENCES photometry_work_items(work_item_id)
);

CREATE INDEX IF NOT EXISTS idx_photometry_failures_source
ON photometry_failures(source_id, failure_type, status);

CREATE TABLE IF NOT EXISTS photometry_output_products (
  output_product_id INTEGER PRIMARY KEY AUTOINCREMENT,
  photometry_run_id TEXT,
  source_id TEXT NOT NULL,
  measurement_id TEXT,
  product_type TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  file_size_bytes INTEGER,
  output_schema_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(photometry_run_id) REFERENCES photometry_runs(photometry_run_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(measurement_id) REFERENCES photometry_measurements(measurement_id)
);

CREATE INDEX IF NOT EXISTS idx_photometry_outputs_source
ON photometry_output_products(source_id, product_type);

CREATE TABLE IF NOT EXISTS photometry_source_summaries (
  source_id TEXT PRIMARY KEY,
  photometry_run_id TEXT,
  source_status TEXT NOT NULL,
  n_planned INTEGER NOT NULL,
  n_measured INTEGER NOT NULL,
  n_failed INTEGER NOT NULL,
  n_science_recommended INTEGER NOT NULL,
  spectrum_path TEXT,
  sed_plot_path TEXT,
  qa_summary_path TEXT,
  provenance_path TEXT,
  measurement_index_path TEXT,
  updated_at TEXT NOT NULL,
  summary_json TEXT,
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(photometry_run_id) REFERENCES photometry_runs(photometry_run_id)
);

CREATE TABLE IF NOT EXISTS workflow_runs (
  workflow_run_id TEXT PRIMARY KEY,
  run_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  args_json TEXT,
  counts_json TEXT,
  summary_path TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS workflow_source_states (
  workflow_run_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  state TEXT NOT NULL,
  planned_count INTEGER NOT NULL DEFAULT 0,
  terminal_count INTEGER NOT NULL DEFAULT 0,
  measured_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  output_status TEXT,
  cleanup_status TEXT,
  reason TEXT,
  updated_at TEXT NOT NULL,
  payload_json TEXT,
  PRIMARY KEY(workflow_run_id, source_id),
  FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(workflow_run_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_source_state
ON workflow_source_states(state);

CREATE TABLE IF NOT EXISTS workflow_events (
  workflow_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_run_id TEXT,
  run_id TEXT,
  event_time TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source_id TEXT,
  product_id INTEGER,
  cutout_id INTEGER,
  work_item_id TEXT,
  message TEXT,
  payload_json TEXT,
  FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(workflow_run_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_run
ON workflow_events(workflow_run_id, event_type);

CREATE TABLE IF NOT EXISTS cleanup_ledger (
  cleanup_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_run_id TEXT,
  run_id TEXT,
  source_id TEXT,
  cutout_id INTEGER,
  cutout_key TEXT NOT NULL,
  local_path TEXT NOT NULL,
  policy TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  deleted_bytes INTEGER,
  deleted_at TEXT,
  created_at TEXT NOT NULL,
  payload_json TEXT,
  FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(workflow_run_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id),
  FOREIGN KEY(source_id) REFERENCES sources(source_id),
  FOREIGN KEY(cutout_id) REFERENCES cutouts(cutout_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cleanup_ledger_cutout_policy
ON cleanup_ledger(cutout_key, policy);

INSERT OR IGNORE INTO schema_migrations(version, applied_at, description)
VALUES (1, datetime('now'), 'initial schema');
