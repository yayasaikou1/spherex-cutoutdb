# Quickstart

Install the release wheel, then create a project from `input_catalog.csv`.
The catalog must have unique `Name` values and coordinates in `RA_deg` and
`DEC_deg`; optional `cutout_size_arcsec` values override the default cutout
size per row.

```bash
python -m pip install spherex_cutoutdb-1.0.0rc1-py3-none-any.whl
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

The main science products are written under `results/spectra`,
`results/plots`, `results/qa`, and `results/provenance`.

The release package includes a tiny example at `examples/input_catalog.csv` for
CLI smoke tests. Real science runs should use your vetted catalog with unique
`Name` values.
