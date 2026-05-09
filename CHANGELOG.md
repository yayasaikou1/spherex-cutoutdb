# Changelog

## 1.0.0rc1 - 2026-05-07

Git tag: `v1.0.0-rc.1`.

- Rebuilt the release candidate from the current source tree instead of stale downloader-only artifacts.
- Included calibration, photometry, integrated workflow, and file-level downloader event-stream modules in the package.
- Added strict validated config handling for documented workflow, calibration, photometry, runtime, cleanup, and core sections.
- Preserved conservative science defaults: SAPM unit conversion, CWAVE/CBAND wavelengths, fixed-position PSF forced photometry, signed fluxes, separate detection and science recommendation states, source-masked background, and target-protected deblending.
- Added clean release-folder packaging and wheel/sdist smoke checks.
- Organized the GitHub-facing repository structure, public docs, and release
  management workflow for the release-candidate commit.
