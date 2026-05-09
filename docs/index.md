# spherex-cutoutdb Documentation

`spherex-cutoutdb` is a SPHEREx catalog-to-spectrum workflow for discovering
Level-2 cutouts, validating required calibration products, running conservative
fixed-position PSF forced photometry, and writing per-source spectra, QA plots,
and provenance.

Start here:

- [Quickstart](quickstart.md)
- [Integrated workflow tutorial](INTEGRATED_WORKFLOW_TUTORIAL.md)
- [Configuration reference](config_reference.md)
- [Science method](science_method.md)
- [Output schema](output_schema.md)
- [Troubleshooting](troubleshooting.md)

The expected tutorial catalog is `input_catalog.csv` with unique `Name`,
`RA_deg`, and `DEC_deg` columns. Optional `cutout_size_arcsec` values override
the default cutout size per target.

## Scientific Caveats

- Wavelength and bandwidth are in microns and come from Spectral WCS `CWAVE`
  and `CBAND` calibration products.
- Fluxes and uncertainties are reported in microJy.
- MJD values come from FITS metadata when present and may be missing for some
  products.
- `science_recommended=true` is the default automated quality-selection field.
- The workflow does not apply redshift/rest-frame transformations by default.
- The workflow does not apply Galactic extinction correction by default.
- Do not mix older official FITS products and newly generated local CSV
  products without checking config hash, calibration IDs, schema version, code
  version, and output manifests.

