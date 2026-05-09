# Project Design

`spherex-cutoutdb` is a SPHEREx catalog-to-spectrum workflow tool. It keeps the
original discovery, planning, downloader, validation, and manifest database
machinery, and adds conservative calibration-aware V5 PSF forced photometry and
an integrated `spxcutdb run` operator path.

## Scope

The release supports:

- strict source catalog validation using `input_catalog.csv`-style tables;
- IRSA SIA2 discovery of SPHEREx Level-2 Spectral Image MEF parent products;
- documented cutout planning and the existing file-level downloader;
- required calibration sync, validation, registry, and resolution;
- fixed-position point-source PSF forced photometry at each catalog target;
- per-source spectra, QA images, provenance, output manifests, and summaries;
- resumable integrated runs with bounded workers, storage pressure limits,
  serialized DB writes, and safe cutout cleanup.

## Input Catalog Contract

The release tutorial uses `input_catalog.csv`:

```csv
Name,RA_deg,DEC_deg,cutout_size_arcsec
DemoTarget,150.116321,2.205830,180
```

`Name` is the durable target identity and source-name column. It must be unique
for normal production runs. `RA_deg` and `DEC_deg` are ICRS coordinates in
degrees. `cutout_size_arcsec` is optional and overrides the default cutout size
for a row.

## Science Policy

The photometry layer is intentionally conservative:

- Required calibration must validate before science-recommended measurements.
- Science wavelength and bandwidth come from Spectral WCS `CWAVE` and `CBAND`.
- MJy/sr to microJy/pixel conversion uses the solid-angle pixel map.
- Forced PSF fluxes are measured at the catalog target position for every valid
  cutout/calibration pair.
- Signed negative fluxes and non-detections remain in the output tables.
- Measurement success, detection status, and science recommendation are
  separate fields.
- Suspicious rows remain in outputs with flags and `science_recommended=false`.

## Integrated Runtime

`spxcutdb run` is the production entry point. It plans before network work,
skips current valid measurements, hands missing cutouts to
`downloader.iter_download_plan_results()` in bounded batches, fits validated
cutouts as they arrive, writes durable rows and outputs, and only then applies
cleanup policy.

Temporary successful cutouts may be deleted after source outputs and manifests
validate. Failed cutouts are kept by default. Calibration products are never
removed by cutout cleanup.

## Project Layout

Created by `spxcutdb init ./project --catalog input_catalog.csv
--target-id-column Name`:

```text
project/
  spherex_cutoutdb.yaml
  db/cutoutdb.sqlite
  data/cutouts/
  cache/
    calibrations/
  logs/
  manifests/
  results/
    spectra/
    plots/
    qa/
    provenance/
  runs/
    <run_id>/
      effective_config.yaml
      effective_config.json
      cli_overrides.json
```

## Configuration

The persistent project config stores durable identity and science policy:
catalog columns, discovery collections, calibration policy, photometry schema,
science thresholds, and output roots.

`batch_config.example.yaml` is a run-preset template for temporary runtime
choices such as worker counts, storage limits, cleanup mode, and QA level.
Explicit CLI flags override both config files and are written to
`cli_overrides.json`.

## Primary Release Commands

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

Standalone discovery uses the catalog path stored in
`project/spherex_cutoutdb.yaml`; `spxcutdb discover` does not accept
`--catalog`. The integrated `run` command does accept `--catalog` as a
temporary CLI override and records it in run provenance.
