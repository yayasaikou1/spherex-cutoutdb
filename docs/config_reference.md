# Config Reference

Configuration is strict. Unknown top-level or nested keys are rejected instead
of silently ignored.

Required top-level sections:

- `project`
- `catalog`
- `spherex`
- `discovery`
- `cutouts`
- `cloud`
- `planning`
- `download`
- `validation`
- `logging`
- `exports`
- `workflow`
- `cleanup`
- `calibration`
- `photometry`
- `runtime`

Precedence for integrated runs:

1. built-in defaults
2. project config, normally `PROJECT/spherex_cutoutdb.yaml`
3. optional `--batch-config`
4. explicit CLI overrides

Every state-changing command records effective config YAML/JSON, config hash,
and CLI overrides under the project run/provenance directories.

## Catalog Path And Discovery

`spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name`
writes the catalog path and column mapping to `./project/spherex_cutoutdb.yaml`.

Standalone discovery uses that persistent project config:

```bash
spxcutdb discover --project ./project --resume
```

`spxcutdb discover` does not accept `--catalog`. To discover against a
different input catalog, edit `catalog.path` in `./project/spherex_cutoutdb.yaml`
or recreate the project config with `spxcutdb init ... --catalog NEW.csv
--force`, then run:

```bash
spxcutdb config validate --project ./project
spxcutdb discover --project ./project --resume
```

The integrated `spxcutdb run` command does accept `--catalog` as a temporary
CLI override and records that override with the run provenance.
