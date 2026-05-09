# Integrated catalog-to-spectrum workflow tutorial

This tutorial shows the production workflow for turning `input_catalog.csv`
into per-source spectra, plots, QA summaries, and provenance.

The integrated workflow is designed for large catalogs. It plans first, skips current measurements before requesting cutouts, downloads missing cutouts through the existing downloader, measures valid cutouts with the V5 photometry kernel, writes durable outputs, then safely removes temporary cutouts by policy.

## 1. Input catalog

For new workflow projects, use a catalog named `input_catalog.csv`. The target
identity is the catalog `Name` column.

Required columns:

- `Name`: durable target ID and output filename source.
- `RA_deg`: right ascension in degrees.
- `DEC_deg`: declination in degrees.

Example:

```csv
Name,RA_deg,DEC_deg
TDE_2025aarm,68.0516625,-5.3776417
TDE_2026dmt,127.2151458,39.0817528
```

Optional per-row cutout sizes can be supplied with `cutout_size_arcsec`.
The release package includes a tiny smoke-test catalog at
`examples/input_catalog.csv`. If your science catalog uses different coordinate
names, pass `--ra-column` and `--dec-column` during `init`.

## 2. Create the project

```bash
spxcutdb init \
  --project ./project \
  --catalog input_catalog.csv \
  --target-id-column Name
```

This creates:

```text
project/
  spherex_cutoutdb.yaml
  db/cutoutdb.sqlite
  data/cutouts/
  cache/
  results/
  logs/
```

For `--target-id-column Name`, both `catalog.source_id_column` and `catalog.source_name_column` are set to `Name`. Output filenames use a safe slug of that value.

## 3. Validate the project and catalog

Review the effective config before touching the network:

```bash
spxcutdb config show --project ./project --effective --hash
spxcutdb config validate --project ./project
spxcutdb config diff --project ./project --against-defaults
```

```bash
spxcutdb validate \
  --project ./project \
  --catalog input_catalog.csv
```

The config commands print the exact resolved project paths, runtime limits, and
config hash used for provenance. Validation checks that the catalog is readable,
required columns exist, target IDs are unique, coordinates are finite, project
paths are safe, and workflow runtime limits are consistent.

Local FITS cutout validation is still available:

```bash
spxcutdb validate-cutouts --project ./project
spxcutdb validate --project ./project --path project/data/cutouts
```

## 4. Discover SPHEREx observations

```bash
spxcutdb discover \
  --project ./project \
  --resume
```

Discovery uses the existing SIA discovery implementation and stores source-product matches in SQLite. The integrated `run` command consumes these matches; it does not implement a second discovery client.

`discover` reads the catalog path from `./project/spherex_cutoutdb.yaml`; it
does not accept `--catalog`. Use `spxcutdb init --catalog input_catalog.csv` to
write the persistent catalog path, or edit the project config and rerun
`spxcutdb config validate --project ./project` before discovery.

For a small smoke test:

```bash
spxcutdb discover --project ./project --resume --limit-sources 5
```

## 5. Configure a batch run

There are two YAML config layers in the integrated workflow:

- `project/spherex_cutoutdb.yaml` is the persistent project config written by
  `spxcutdb init`. It is always loaded by default for commands that use
  `--project ./project`.
- `batch_config.example.yaml` is the packaged run-preset template. It is loaded
  only when `--batch-config batch_config.example.yaml` is present.

Use `spherex_cutoutdb.yaml` for durable project identity and science policy:
catalog column mapping, discovery collections, calibration cache/products,
photometry schema/code versions, and science thresholds.

Use a batch config for batch-specific runtime policy: whether to download
missing cutouts, how many workers to use, storage pressure limits, cleanup
policy, QA level, and optional archive pacing.

The repository includes a batch template:

```text
batch_config.example.yaml
```

Use it directly as a documented starting point:

```bash
spxcutdb run \
  --project ./project \
  --catalog input_catalog.csv \
  --batch-config batch_config.example.yaml \
  --download-missing \
  --resume \
  --cleanup-cutouts success-after-source
```

For that command, config precedence is:

1. built-in package defaults;
2. `./project/spherex_cutoutdb.yaml`;
3. `batch_config.example.yaml`;
4. explicit CLI flags.

The explicit CLI flags set `catalog.path` to `input_catalog.csv`,
`workflow.download_missing` to `true`, and `cleanup.cutouts` to
`success-after-source`, even if either YAML file has a different value.
`--resume` controls workflow state reuse and is recorded in run provenance, but
it is not a config key.

Inspect the exact merged batch config before a long run:

```bash
spxcutdb config show --project ./project --batch-config batch_config.example.yaml --effective --hash
spxcutdb config validate --project ./project --batch-config batch_config.example.yaml
spxcutdb config diff --project ./project --batch-config batch_config.example.yaml --against-defaults
```

Important package/project defaults:

```yaml
download:
  max_workers: 64
  concurrency: 4096
  per_host_rate_limit_per_second: 4096
  per_host_max_concurrency: 2048
photometry:
  qa:
    full_plot_workers: 32
runtime:
  max_download_workers: 32
  max_fit_workers: 32
  max_source_workers: 64
  max_inflight_cutouts: 512
  max_live_cutout_gb: 10
  max_open_fits_files: 512
  max_image_workers_per_source: 432
  global_max_network_requests: 2048
  global_max_open_fits_files: 512
```

If you pass a batch config, it overrides the project defaults for that run
only. Keep smaller runtime values in the batch file only when you deliberately
want a conservative run preset. For example:

```yaml
workflow:
  source_chunk_size: 100
  download_missing: true
  skip_valid_measurements: true
  regenerate_missing_outputs: true
cutouts:
  default_size_arcsec: 60
  size_column: cutout_size_arcsec
  min_size_arcsec: 20
  max_size_arcsec: 3600
runtime:
  max_download_workers: 12
  max_fit_workers: 4
  max_source_workers: 1
  max_inflight_cutouts: 256
  max_live_cutout_gb: 5
  max_open_fits_files: 32
cleanup:
  cutouts: success-after-source
  keep_failed_cutouts: true
```

Do not duplicate the same setting in both YAML files unless you deliberately
want the batch file to override the project file. Batch config values are not
written back into `spherex_cutoutdb.yaml`; each run records the effective
merged config under `project/runs/<run_id>/`.

Cutout size is resolved explicitly:

1. If the catalog has a `cutout_size_arcsec` column for a source, that per-source value is used.
2. Otherwise, `--batch-config batch_config.example.yaml` uses `cutouts.default_size_arcsec`.
3. Without a batch override, the project `spherex_cutoutdb.yaml` value is used.
4. The internal model default is only a fallback for incomplete hand-written configs.

The `init` command writes `cutouts.default_size_arcsec: 60` by default unless you pass `--default-cutout-size-arcsec`.

## 6. Run the integrated workflow

```bash
spxcutdb run \
  --project ./project \
  --catalog input_catalog.csv \
  --batch-config batch_config.example.yaml \
  --download-missing \
  --resume \
  --cleanup-cutouts success-after-source
```

If discovery has not been run yet, `spxcutdb run` fails with a recommended
discovery command unless you explicitly request discovery. For a fresh project,
you can combine discovery, calibration sync, download, photometry, output, and
cleanup in one command:

```bash
spxcutdb run \
  --project ./project \
  --catalog input_catalog.csv \
  --discover \
  --sync-calibration \
  --download-missing \
  --resume \
  --cleanup-cutouts success-after-source \
  --qa-level standard
```

The manager does the following:

1. Loads active catalog sources and discovered source-product matches.
2. Builds the photometry plan before downloading.
3. Skips valid current photometry rows.
4. Sends valid existing cutouts directly to the fit queue.
5. Sends missing cutouts to `downloader.iter_download_plan_results()` in bounded batches.
6. Fits validated download events as they arrive.
7. Writes measurement rows through the manager connection.
8. Writes or rebuilds per-source CSV, SED, QA summary, provenance, measurement index, and output manifest from DB rows.
9. Deletes only safe temporary cutouts after current output manifests validate.

Required calibration is checked during planning. If Spectral WCS or solid-angle calibration is missing, affected work items are marked `calibration_missing` and the downloader is not started for them. Sync calibration first:

```bash
spxcutdb calibration sync --project ./project --product required
```

Or ask the integrated command to do that before planning:

```bash
spxcutdb run --project ./project --catalog input_catalog.csv --sync-calibration --download-missing --resume --cleanup-cutouts success-after-source --qa-level standard
```

## 7. Outputs

For `Name = TDE_2025aarm`, outputs are:

```text
project/results/
  spectra/TDE_2025aarm.csv
  plots/TDE_2025aarm_sed.png
  qa/TDE_2025aarm/TDE_2025aarm_qa_summary.png
  provenance/TDE_2025aarm_provenance.json
  provenance/TDE_2025aarm_measurement_index.json
  provenance/TDE_2025aarm_output_manifest.json
```

The CSV keeps science and audit information together:

- signed forced fluxes, including negative values;
- non-detections;
- detection status;
- science recommendation;
- photometry flags;
- calibration IDs;
- photometry config hash;
- code and output schema versions;
- fit quality metrics including `fit_ql_mean_abs_2p5pix`;
- calibration match fields: `detector_release_match`, `header_reference_match`, `exact_match`, and `calibration_match_quality`.

Rows with `science_recommended=false` are retained for audit and should not be treated as science-grade fluxes without inspection.

The output manifest records the measurement IDs, row count, row hash, output
schema, photometry code version, effective config hash, and output file
checksums. File existence alone is not enough for an output to be considered
current.

## 8. Summary and output rebuild

```bash
spxcutdb summary --project ./project
```

To rebuild missing or stale compact outputs from the database without
redownloading FITS cutouts:

```bash
spxcutdb summary \
  --project ./project \
  --rebuild-missing-outputs
```

This works after temporary cutout cleanup because compact outputs are rebuilt
from persisted measurement rows and provenance. Full per-measurement QA images
require FITS/model arrays and may not be rebuildable after cutouts are deleted.

## 9. Update mode

Use update mode after new SPHEREx products are available:

```bash
spxcutdb run \
  --project ./project \
  --catalog input_catalog.csv \
  --batch-config batch_config.example.yaml \
  --update \
  --download-missing \
  --resume
```

`run --update` uses the existing discovery path before integrated planning, then processes only missing or changed current work. Valid current measurements remain skipped, even if their temporary cutouts were deleted.

## 10. Cleanup policy

Default:

```yaml
cleanup:
  cutouts: success-after-source
  keep_failed_cutouts: true
```

Cutouts are deleted only when:

- the file is under the project-managed `data/cutouts/` directory;
- measurement or durable failure rows exist;
- per-source CSV, SED, QA summary, provenance, and measurement index validate;
- the per-source output manifest matches current DB measurement rows and file checksums;
- when `--qa-level full` is requested, the full-QA manifest and per-measurement
  PNG checksums are current;
- no active fit task references the cutout;
- the cleanup policy allows deletion.

Calibration files, cache metadata, manifests, logs, result products, and provenance are never removed by normal cutout cleanup.

For debugging:

```bash
spxcutdb run \
  --project ./project \
  --source-name TDE_2025aarm \
  --qa-level full \
  --qa-workers 4 \
  --cleanup-cutouts never \
  --verbose
```

Full QA is an output phase. Compact CSV, SED, QA-summary, provenance, and
manifest products are written first. The PNG writer then renders
`results/qa/<source>/measurements/<measurement_id>_qa.png` with the configured
worker pool. If a current DB measurement is missing a full-QA PNG and the
validated cutout still exists, the workflow remeasures that item to rebuild the
plot without redownloading. If the cutout was already cleaned, the valid
measurement remains valid and the workflow reports an operator hint instead of
redownloading only for QA.

## 11. Logs and progress

Each integrated run writes:

```text
project/logs/runs/<workflow_run_id>.log
project/logs/runs/<workflow_run_id>.jsonl
project/runs/<run_id>/effective_config.yaml
project/runs/<run_id>/effective_config.json
project/runs/<run_id>/cli_overrides.json
```

Use `--no-progress` for nohup or batch schedulers:

```bash
nohup spxcutdb run \
  --project ./project \
  --catalog input_catalog.csv \
  --batch-config batch_config.example.yaml \
  --download-missing \
  --resume \
  --no-progress > run.out 2>&1 &
```

Use `--verbose` to print per-event lines for planning, download retry events, fit status, output writing, and cleanup.

## 12. Recovery commands

Resume after interruption:

```bash
spxcutdb run --project ./project --catalog input_catalog.csv --batch-config batch_config.example.yaml --download-missing --resume
```

If the run reports that discovery is missing:

```bash
spxcutdb discover --project ./project --resume
spxcutdb run --project ./project --catalog input_catalog.csv --download-missing --resume
```

or rerun with:

```bash
spxcutdb run --project ./project --catalog input_catalog.csv --discover --download-missing --resume
```

Rebuild missing outputs only:

```bash
spxcutdb summary --project ./project --rebuild-missing-outputs
```

Inspect failed sources:

```bash
spxcutdb summary --project ./project --failed-only
```

Run one source with full diagnostics and retained cutouts:

```bash
spxcutdb run --project ./project --source-name TDE_2025aarm --qa-level full --qa-workers 4 --cleanup-cutouts never --verbose
```

Sync required calibration products if the workflow reports missing calibration:

```bash
spxcutdb calibration sync --project ./project --product required
```

## 13. Design guarantees

- The integrated workflow does not create a second downloader.
- Missing cutouts are handed to `downloader.iter_download_plan_results()` in batches.
- Valid measurements prevent redownload after cleanup.
- Valid existing cutouts measure without redownload.
- Missing output products rebuild from SQLite measurement rows.
- Failed cutouts are retained by default.
- Workflow DB writes are serialized through the manager thread.
- Temporary cutouts are deleted only after durable outputs validate.
