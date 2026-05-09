# QA Flags

The workflow keeps suspicious measurements in outputs and uses flags plus
`science_recommended=false` to prevent silent data loss.

Common flag categories:

- calibration blockers: missing or invalid Spectral WCS or SAPM
- pixel mask blockers: severe detector/pathology flags in fit pixels
- background blockers: insufficient source-masked pixels or unstable plane fit
- background fallback warning: `BACKGROUND_IMAGE_CLIPPED_FALLBACK` means the
  nominal detector/source flag mask was too restrictive and a documented
  image-clipped smooth background was used instead
- PSF blockers: missing plane-center metadata, invalid rendering, or truncation
- fit blockers: singular, ill-conditioned, or high-residual weighted fit
- deblend blockers: unstable joint fit, high target-neighbor covariance, or target-protection split
- diagnostic states: negative forced flux, non-detection, contamination risk, target possibly extended

Negative forced flux is not a failed measurement. It is a signed non-detection
state unless other QA failures are also present.

`failed_background` rows are not science measurements: flux/SNR fields are not
finite, `science_recommended=false`, and `science_reject_reason` records
`BACKGROUND_POOR`.
