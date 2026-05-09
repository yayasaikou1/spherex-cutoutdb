# Validation Plan

This release is validated as a catalog-to-spectrum workflow, not a
downloader-only package.

## Required Offline Checks

Run from the repository root:

```bash
python -m compileall src
pytest -q
```

Build release artifacts from the repository root:

```bash
python -m compileall src
pytest -q
python -m build
```

The test suite must pass without live IRSA access.

## Catalog And Config Checks

Use `input_catalog.csv` for tutorial and release smoke commands:

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name --force
spxcutdb config show --project ./project --effective --hash
spxcutdb config validate --project ./project
spxcutdb validate --project ./project --catalog input_catalog.csv
```

Validation must reject:

- missing `Name`, RA, or Dec columns;
- duplicate or blank `Name` values unless explicitly allowed;
- invalid coordinates;
- unknown strict config keys in critical sections;
- unsafe cleanup/output paths.

## Calibration Checks

Required calibration products are:

- `spectral_wcs`
- `solid_angle_pixel_map`

Expected operator commands:

```bash
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb calibration status --project ./project
```

Measurement must be blocked or not science-recommended when required
calibration is missing, invalid, stale, or not matched to the measurement
detector/release policy.

## Integrated Workflow Checks

No-network dry-run smoke:

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

Behavior required by tests:

- plan before download;
- skip valid current measurements before network work;
- use `downloader.iter_download_plan_results()` for production download work;
- measure valid existing cutouts without redownload;
- rebuild missing compact outputs from valid DB measurements;
- keep failed cutouts by default;
- delete temporary successful cutouts only after durable rows, source outputs,
  provenance, and output manifests validate;
- never delete calibration products.

## Science Checks

The test suite must cover:

- Spectral WCS `CWAVE` and `CBAND` use for science wavelength and bandwidth;
- SAPM-based MJy/sr to microJy/pixel conversion;
- detector-coordinate PSF selection and oversampled rendering;
- signed negative flux and nondetection preservation;
- separated measurement, detection, and science recommendation fields;
- source-masked 2D background with fallback;
- target-protected neighbor finding;
- conservative fixed-position deblending with covariance/conditioning gates;
- suspicious rows retained with flags and `science_recommended=false`.

## Release Artifact Checks

After `python -m build`, inspect the wheel and sdist under `dist/`:

- version metadata is `1.0.0rc1`;
- wheel imports include `spherex_cutoutdb.calibration`,
  `spherex_cutoutdb.photometry`, `spherex_cutoutdb.integrated_workflow`, and
  `spherex_cutoutdb.downloader.iter_download_plan_results`;
- sdist includes `examples/input_catalog.csv`;
- sdist does not include legacy example catalogs, internal Codex docs, stale
  `dist/`, caches, logs, data products, or debug outputs.

Install the built wheel in a clean environment and run:

```bash
python - <<'PY'
import spherex_cutoutdb
import spherex_cutoutdb.calibration
import spherex_cutoutdb.photometry
import spherex_cutoutdb.integrated_workflow
from spherex_cutoutdb.downloader import iter_download_plan_results
print(spherex_cutoutdb.__version__)
print(iter_download_plan_results.__name__)
PY
spxcutdb --help
spxcutdb config --help
spxcutdb run --help
```
