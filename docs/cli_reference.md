# CLI Reference

Primary commands:

- `spxcutdb init`: create a project config, directories, and SQLite database.
- `spxcutdb config show|validate|defaults|diff`: inspect strict effective config and config hash.
- `spxcutdb validate`: validate catalog preflight or local cutout FITS files.
- `spxcutdb discover`: query SPHEREx parent products through IRSA SIA2 using
  the catalog path stored in the project config.
- `spxcutdb calibration sync|validate|status`: manage required Spectral WCS and SAPM products.
- `spxcutdb run`: execute discovery/download/photometry/output/cleanup workflow.
- `spxcutdb summary`: summarize workflow completeness and rebuild missing outputs.

Expert commands remain available for lower-level operation:

- `catalog validate|ingest`
- `plan`
- `download`
- `validate-cutouts`
- `coverage`
- `retry-failed`
- `clean-partials`
- `export-manifest`
- `photometry plan|source|run|rerun|summarize|clean|clean-results|validate-results`
- `sync`

Use `--help` on any command for the current options.

Note that `spxcutdb discover` does not accept `--catalog`. Initialize or edit
`PROJECT/spherex_cutoutdb.yaml` first, then run:

```bash
spxcutdb discover --project PROJECT --resume
```
