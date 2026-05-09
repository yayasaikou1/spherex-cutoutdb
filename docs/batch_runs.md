# Batch Runs

Use `batch_config.example.yaml` for temporary run controls such as worker
counts, storage limits, cleanup policy, and QA level. Keep durable source
identity, calibration policy, and science thresholds in the project config.

Recommended pattern:

```bash
spxcutdb config show --project ./project --batch-config batch_config.example.yaml --effective --hash
spxcutdb config validate --project ./project --batch-config batch_config.example.yaml
spxcutdb run --project ./project --catalog input_catalog.csv --batch-config batch_config.example.yaml --download-missing --resume
```

The integrated manager plans before downloading, skips valid current
measurements, batches missing cutouts through `iter_download_plan_results()`,
fits validated cutouts through bounded workers, serializes SQLite writes, and
cleans successful temporary cutouts only after durable source outputs validate.
