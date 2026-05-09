# Troubleshooting

Useful checks:

```bash
spxcutdb config validate --project ./project
spxcutdb calibration status --project ./project
spxcutdb summary --project ./project
spxcutdb photometry validate-results --project ./project
```

Common issues:

- Missing calibration: run `spxcutdb calibration sync --product required`, then validate.
- Duplicate or missing `Name`: fix the catalog; strict validation rejects ambiguous target IDs.
- Missing cutouts: use `spxcutdb run --download-missing --resume` so the integrated workflow batches downloader work.
- Missing or stale outputs: run `spxcutdb summary --rebuild-missing-outputs`; valid DB measurements rebuild without redownloading.
- Storage backpressure: lower `runtime.max_inflight_cutouts` or increase `runtime.max_live_cutout_gb`; failed cutouts are retained by default.
- Non-detections: signed negative fluxes are preserved; filter on `science_recommended` for automated science selections.
