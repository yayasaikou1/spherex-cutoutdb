# CLI Workflow

This release is organized around one operator path:

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name
spxcutdb config show --project ./project --effective --hash
spxcutdb config validate --project ./project
spxcutdb validate --project ./project --catalog input_catalog.csv
spxcutdb discover --project ./project --resume
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb run --project ./project --catalog input_catalog.csv --download-missing --resume --cleanup-cutouts success-after-source --qa-level standard
spxcutdb summary --project ./project
```

The required input catalog columns are:

- `Name`: durable target ID and output filename source.
- `RA_deg`: right ascension in degrees.
- `DEC_deg`: declination in degrees.
- `cutout_size_arcsec`: optional per-target cutout size.

`Name` values must be present and unique unless duplicate IDs are explicitly
enabled in config.

## Primary Commands

### `init`

Creates the project directory, strict YAML config, SQLite database, and managed
output directories.

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name
```

Use `--ra-column` and `--dec-column` if the catalog does not use `RA_deg` and
`DEC_deg`.

### `config show|validate|defaults|diff`

Inspects the effective config and config hash before a state-changing run.

```bash
spxcutdb config show --project ./project --effective --hash
spxcutdb config validate --project ./project
spxcutdb config diff --project ./project --against-defaults
```

Every state-changing run writes `effective_config.yaml`,
`effective_config.json`, `cli_overrides.json`, and the config hash under
`project/runs/<run_id>/`.

### `validate --catalog`

Runs catalog/project preflight.

```bash
spxcutdb validate --project ./project --catalog input_catalog.csv
```

It checks required columns, duplicate/missing target IDs, coordinate validity,
and project/config consistency. Local FITS cutout validation remains available
through `spxcutdb validate --path PATH` and `spxcutdb validate-cutouts`.

### `discover`

Queries SPHEREx parent products through the existing IRSA SIA2 discovery path.

```bash
spxcutdb discover --project ./project --resume
```

`discover` reads `catalog.path` from `./project/spherex_cutoutdb.yaml`; it
does not accept `--catalog`. The `init --catalog input_catalog.csv` command
writes that path into the project config. If you need a different catalog for a
standalone discovery run, update the project config first, then rerun
`spxcutdb config validate --project ./project`.

Use `--mock-sia examples/mock_sia_response.xml` for no-network smoke tests.

### `calibration sync|validate|status`

Registers the required Spectral WCS and solid-angle pixel map products.

```bash
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb calibration status --project ./project
```

Photometry is blocked or marked not science-recommended when required
calibration is missing or invalid.

### `run`

Runs the integrated catalog-to-spectrum workflow.

```bash
spxcutdb run \
  --project ./project \
  --catalog input_catalog.csv \
  --download-missing \
  --resume \
  --cleanup-cutouts success-after-source \
  --qa-level standard
```

`run` plans before download, skips valid measurements before network work,
hands missing cutouts to `downloader.iter_download_plan_results()` in batches,
fits validated cutouts with the V5 photometry stack, serializes DB writes,
writes per-source outputs, and deletes temporary successful cutouts only after
durable outputs validate.

Use this one-command fresh project form when desired:

```bash
spxcutdb run --project ./project --catalog input_catalog.csv --discover --sync-calibration --download-missing --resume --cleanup-cutouts success-after-source --qa-level standard
```

### `summary`

Summarizes workflow completeness and can rebuild missing compact outputs from
valid DB measurement rows without redownloading cutouts.

```bash
spxcutdb summary --project ./project
spxcutdb summary --project ./project --rebuild-missing-outputs
```

## Expert Commands

The lower-level commands remain available for debugging and staged operation:

- `catalog validate|ingest`
- `plan`
- `download`
- `coverage`
- `retry-failed`
- `clean-partials`
- `export-manifest`
- `photometry plan|source|run|rerun|summarize|clean-results|validate-results`
- `sync`

Use them when you need to inspect a specific layer. For normal release usage,
prefer `spxcutdb run`.

## No-Network Smoke

```bash
spxcutdb init ./smoke_spherex --catalog examples/input_catalog.csv --target-id-column Name --force
spxcutdb config validate --project ./smoke_spherex
spxcutdb validate --project ./smoke_spherex --catalog examples/input_catalog.csv
spxcutdb run \
  --project ./smoke_spherex \
  --catalog examples/input_catalog.csv \
  --discover \
  --mock-sia examples/mock_sia_response.xml \
  --no-download \
  --dry-run \
  --no-progress
spxcutdb summary --project ./smoke_spherex
```
