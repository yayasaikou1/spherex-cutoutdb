# Science Method

The release science path is fixed-position point-source forced photometry on
validated SPHEREx Level-2 Spectral Image MEF cutouts.

Core rules:

- Required calibration products are Spectral WCS and solid-angle pixel map.
- Science wavelength and bandwidth are sampled from Spectral WCS `CWAVE` and `CBAND`.
- Pixel surface brightness is converted from MJy/sr to uJy/pixel using SAPM values converted to steradians.
- Photometry uses the `IMAGE` extension directly. The `ZODI` extension is not
  subtracted and is not used as a background prior or QA/science-selection input.
- Every valid cutout with valid calibration gets a target-position point-source PSF forced measurement.
- Signed negative fluxes and non-detections are retained.
- Measurement result, detection status, and science recommendation are separate.
- Background uses a source-masked 2D plane. If detector/source flags leave too
  few nominal background pixels, the workflow rebuilds a background mask from
  `IMAGE` with iterative 3-sigma clipping, combines it with hard bad-pixel and
  invalid-variance masks, and fits a smooth 2D background. If that fallback
  still has too few valid pixels, the row is marked `failed_background` and no
  science flux is recommended.
- Neighbor finding protects the target footprint and does not split target residuals into artificial neighbors.
- Joint deblending is fixed-position, covariance/conditioning gated, and conservative.
- Suspicious rows remain in output tables with flags and `science_recommended=false`.

Use `science_recommended=true` for automated science selections unless flagged
rows have been manually reviewed.

## Units And Assumptions

- `wavelength_um` and `bandwidth_um` are in microns.
- Fluxes and flux uncertainties are in microJy.
- Observation times use MJD metadata when present in the FITS products; time
  fields may be missing for products without usable headers.
- Quality filtering should use `science_recommended` for automated selection,
  while retaining non-recommended rows for audit.
- The workflow does not apply redshift corrections or transform measurements
  into the rest frame by default.
- The workflow does not apply Galactic extinction corrections by default.
- Output CSV products are tied to their calibration IDs, config hash, schema
  version, code version, and output manifest. Do not merge old official FITS
  products with newly generated local CSV products unless those provenance
  fields have been checked.
