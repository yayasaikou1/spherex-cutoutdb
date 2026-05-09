# SPHEREx Photometry Tutorial

This tutorial is for advanced or diagnostic runs where you want to operate the
photometry layer directly. Most users should start with
`spxcutdb run`, which integrates discovery, download, calibration checks,
photometry, output writing, and cleanup. Use the lower-level photometry
commands here when cutouts already exist or when you need to inspect planner
states source by source.

## 1. Prepare Inputs

For a normal release project, start from `input_catalog.csv` and let the
integrated workflow create the durable project state:

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name --force
spxcutdb validate --project ./project --catalog input_catalog.csv
spxcutdb discover --project ./project --resume
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb run --project ./project --catalog input_catalog.csv --download-missing --resume --cleanup-cutouts never --qa-level standard
```

The `--cleanup-cutouts never` setting keeps successful FITS cutouts available
for the direct photometry commands below. For production low-storage runs, use
`--cleanup-cutouts success-after-source` instead.

If you are deliberately using the older expert command chain, the equivalent
pre-photometry preparation is:

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name --force
spxcutdb catalog validate --project ./project --verbose
spxcutdb catalog ingest --project ./project
spxcutdb discover --project ./project --verbose
spxcutdb plan --project ./project --export-plan
spxcutdb download --project ./project --max-workers 6 --verbose
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb calibration status --project ./project
```

Expected calibration status has one valid `spectral_wcs` and one valid
`solid_angle_pixel_map` row per detector needed by the project.

## 2. Plan Photometry

Run:

```bash
spxcutdb photometry plan --project ./project
```

The planner does not measure fluxes. It classifies what would happen next.

Common states:

| State | Meaning | Next command |
|---|---|---|
| `photometry_valid` | Matching photometry already exists. | Nothing needed. |
| `cutout_valid_measurement_missing` | The cutout exists and validates, but photometry is missing or stale. | Run `photometry source` or `photometry run`. |
| `cutout_missing_or_invalid` | The cutout is absent or invalid. | Run `plan`, `download`, and `validate`, then rerun `photometry plan`. |
| `calibration_missing` | Required calibration is not available or not valid. | Run `calibration sync`, then `calibration validate`. |
| `download_failed` | Downloader could not fetch the cutout. | Inspect downloader failures, then retry. |
| `validation_failed` | FITS validation failed. | Revalidate or redownload the cutout. |

Example:

```text
Photometry plan: {'cutout_valid_measurement_missing': 594}
```

This is a ready-to-measure state. It means 594 valid cutouts are present and
calibration is available, but no matching photometry rows have been written yet.

## 3. Measure One Source First

Run one source with full QA and keep cutouts while checking the output:

```bash
spxcutdb photometry source --project ./project --source-name <Name> --qa-level full --cleanup-cutouts none
```

If your catalog is keyed by source ID:

```bash
spxcutdb photometry source --project ./project --source-id <source_id> --qa-level full --cleanup-cutouts none
```

Expected outputs:

```text
results/spectra/<source_name>.csv
results/plots/<source_name>_sed.png
results/qa/<source_name>/<source_name>_qa_summary.png
results/qa/<source_name>/measurements/<measurement_id>_qa.png
results/provenance/<source_name>_provenance.json
results/provenance/<source_name>_measurement_index.json
```

Inspect the CSV before running the full catalog. Negative and non-detected
forced fluxes are valid measurements and are preserved. Use
`science_recommended` to select conservative science rows.

## 4. Run a Small Batch

```bash
spxcutdb photometry run --project ./project --limit-sources 10 --qa-level standard --cleanup-cutouts none
```

For a full-QA batch smoke test, keep the batch small and let the QA writer use
its output worker pool:

```bash
spxcutdb photometry run --project ./project --limit-sources 10 --qa-level full --qa-workers 4 --verbose --cleanup-cutouts none
```

The progress bar reports the current phase and `qa=<written>/<total>` while
full per-measurement PNGs are being written. `--verbose` prints per-source
timing and full-QA plot counts, which makes it clear when the run has moved
from measurement into image writing.

Full-QA PNGs are tracked separately from compact source outputs. If a matching
DB measurement is already valid but its full-QA PNG or manifest is missing,
batch `photometry run --qa-level full` remeasures that item only when the
validated cutout file is still present. If cleanup already removed the cutout,
the valid measurement is kept and the run does not redownload solely to rebuild
the diagnostic PNG.

Then validate products:

```bash
spxcutdb photometry summarize --project ./project
spxcutdb photometry validate-results --project ./project
```

Rerun the planner:

```bash
spxcutdb photometry plan --project ./project
```

Measured items should move from `cutout_valid_measurement_missing` to
`photometry_valid`.

## 5. Run the Full Catalog

After the one-source and small-batch checks are clean:

```bash
spxcutdb photometry run --project ./project --qa-level standard --max-source-workers 8
spxcutdb photometry summarize --project ./project
spxcutdb photometry validate-results --project ./project
```

Default cleanup deletes successful temporary cutouts only after DB rows and
durable outputs validate. Failed, invalid, or calibration-blocked cutouts are
retained for debugging.

## 6. Rerun Existing Photometry

Normal `photometry run` is resumable: rows in `photometry_valid` are skipped,
and skipped rows do not redownload cutouts. To intentionally remeasure with the
current config/code/schema identity, use `--force-rerun` or the `rerun`
shortcut.

Preview a single-source rerun:

```bash
spxcutdb photometry plan --project ./project --source-name <Name> --force-rerun
```

Rerun one source with full QA and keep cutouts:

```bash
spxcutdb photometry rerun --project ./project --source-name <Name> --qa-level full --cleanup-cutouts none
```

Rerun the catalog with direct-run process workers:

```bash
spxcutdb photometry rerun --project ./project --qa-level standard --max-source-workers 8
spxcutdb photometry summarize --project ./project
spxcutdb photometry validate-results --project ./project
```

If earlier cleanup deleted successful temporary cutouts, a forced rerun may
download them again. To avoid that during QA/debug cycles, run the first pass
with `--cleanup-cutouts none` or `--retain-cutouts all`.

If you want to remove the current photometry products first and then use the
normal run command, use `clean-results`:

```bash
spxcutdb photometry clean-results --project ./project --source-name <Name> --dry-run
spxcutdb photometry clean-results --project ./project --source-name <Name> --yes
spxcutdb photometry plan --project ./project --source-name <Name>
spxcutdb photometry run --project ./project --source-name <Name> --qa-level full --cleanup-cutouts none
```

For the whole project:

```bash
spxcutdb photometry clean-results --project ./project --all --dry-run
spxcutdb photometry clean-results --project ./project --all --yes
spxcutdb photometry run --project ./project --qa-level standard --max-source-workers 8
```

`clean-results` removes photometry measurements, work items, failure rows,
source summaries, output-product registry rows, and generated `results/`
photometry files. It leaves downloaded cutout records and calibration products
intact.

## 7. Speed Knobs

The science-grade photometry implementation is unchanged by the speed knobs.
The package default keeps the previous high-throughput source-worker value
(`runtime.max_source_workers: 64`), but examples below pass smaller explicit
limits while validating a new project. In direct `photometry run`,
`--max-source-workers` controls measurement worker processes by default:

```bash
spxcutdb photometry run --project ./project --qa-level minimal --max-source-workers 16
```

Use `qa-level minimal` or `standard` for large throughput runs, and reserve
`qa-level full` for audits because full per-measurement PNGs are I/O-heavy.
When `--qa-level full` is used without an explicit `--max-source-workers`, the
runner auto-caps worker concurrency and reports the cap reason. Use
`--worker-backend thread` only as a debugging fallback if the local platform
cannot start process workers. Full-QA PNG writing is parallelized separately
from science measurement:

```bash
spxcutdb photometry run --project ./project --limit-sources 10 --qa-level full --max-source-workers 8 --qa-workers 4
```

`--qa-workers` controls only PNG rendering worker processes. Lower it to `1`
on memory-constrained machines, or raise it if CPU is idle and disk writes are
not saturated. The default full-QA writer uses `measurement_plot_dpi: 110` and
scale annotations instead of slow per-panel colorbars; enable colorbars only
when needed with `--qa-colorbars` or
`photometry.qa.measurement_plot_colorbars: true`. The per-source cutout loop
remains low-storage and writes durable outputs before cleanup.

The default V5 background engine is `photutils`: it uses `photutils`
background/RMS machinery for fast source-masked 2D background prefiltering,
then keeps the V5 robust plane model and constant fallback semantics. To audit
the pure NumPy path, set `photometry.background.engine: numpy` in the config.

## 8. Troubleshooting

### `Photometry plan: {'cutout_valid_measurement_missing': N}`

This is not an error. Run:

```bash
spxcutdb photometry run --project ./project --qa-level standard
```

Use `--limit-sources` or `photometry source` first if you want a smaller smoke
test.

### `Photometry plan: {'calibration_missing': N}`

Check calibration:

```bash
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb calibration status --project ./project
```

If status shows valid calibration for every needed detector but planning still
reports `calibration_missing`, inspect the work-item reason in SQLite; it may be
a resolver/version-policy issue rather than missing files.

### `photometry_valid`

The current source/cutout/calibration/config/code/schema identity already has a
valid measurement. Reruns skip it and do not redownload the cutout.

To force remeasurement:

```bash
spxcutdb photometry rerun --project ./project --source-name <Name> --qa-level full
```

### `cutout_missing_or_invalid`

Run:

```bash
spxcutdb plan --project ./project --source-name <Name> --export-plan
spxcutdb download --project ./project --max-workers 6 --verbose
spxcutdb validate --project ./project --update-db
spxcutdb photometry plan --project ./project
```

Then run photometry after the state changes to
`cutout_valid_measurement_missing`.

Photometry itself does not download missing cutouts. This keeps network retry,
partial-file cleanup, FITS validation, and download provenance in the downloader
module.
