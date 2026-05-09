# Release Notes

## 1.0.0rc1

This release candidate is built from the current working source tree and
replaces stale downloader-only `0.1.0` artifacts.

Included:

- complete `src/spherex_cutoutdb/` package
- calibration cache sync/validation/resolution
- V5 conservative PSF forced-photometry workflow
- integrated catalog-to-spectrum manager
- file-level downloader event seam `iter_download_plan_results()`
- strict validated configuration and config-hash provenance
- refreshed README/tutorial flow centered on `input_catalog.csv`
- clean wheel/sdist and release-folder packaging checks

Known limitations:

- No live IRSA smoke is required for this release candidate.
- Extended-source handling remains conservative and not the default science path.
- The repository currently ships a license notice reserving rights until maintainers select a distribution license.
