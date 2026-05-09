# spherex-cutoutdb Documentation

**Turn target catalogs into ready-to-inspect SPHEREx cutouts and spectra.**

`spherex-cutoutdb` is a command-line Python workflow for building local SPHEREx
cutout databases from input catalogs, running fixed-position PSF forced
photometry, and assembling per-source spectra.

This documentation provides installation instructions, a quickstart guide, an
end-to-end workflow tutorial, troubleshooting notes, and a Chinese tutorial for
new users.

> [!NOTE]
> This is a release-candidate documentation site. The command-line interface,
> configuration schema, database schema, and output schema may still change
> before `v1.0.0`.

> [!IMPORTANT]
> `spherex-cutoutdb` is not an official SPHEREx pipeline. Inspect QA products,
> image flags, calibration status, background behavior, and output manifests
> before using derived measurements for scientific analysis.

## Start here

- [Installation](INSTALL_HELP.md)
- [Quickstart](quickstart.md)
- [Integrated workflow tutorial](INTEGRATED_WORKFLOW_TUTORIAL.md)
- [Troubleshooting](troubleshooting.md)
- [Chinese tutorial](tutorials_SC.md)

## What the workflow does

The typical workflow is:

1. Prepare an input target catalog.
2. Initialize and validate a local project.
3. Discover available SPHEREx Level-2 cutout products.
4. Download and validate cutouts.
5. Run fixed-position PSF forced photometry.
6. Assemble per-source spectra and QA outputs.
7. Inspect the results before scientific use.

## Input catalog

The expected tutorial catalog is `input_catalog.csv` with these required
columns:

| Column | Description |
|---|---|
| `Name` | Unique target name. |
| `RA_deg` | Right ascension in decimal degrees. |
| `DEC_deg` | Declination in decimal degrees. |

Optional `cutout_size_arcsec` values can override the default cutout size for
individual targets.

## Scientific caveats

- Wavelength and bandwidth are in microns.
- Fluxes and uncertainties are reported in microJy.
- MJD values come from FITS metadata when present and may be missing for some
  products.
- `science_recommended=true` is the default automated quality-selection field.
- The workflow does not apply redshift or rest-frame transformations by default.
- The workflow does not apply Galactic extinction correction by default.
- Do not mix older official FITS products and newly generated local CSV products
  without checking the config hash, calibration IDs, schema version, code
  version, and output manifests.