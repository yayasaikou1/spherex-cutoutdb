# Calibration

Photometry requires detector-keyed calibration products:

- `spectral_wcs`: FITS product with `CWAVE` and `CBAND` HDUs
- `solid_angle_pixel_map`: FITS image product with positive pixel solid angles

Use:

```bash
spxcutdb calibration sync --project ./project --product required
spxcutdb calibration validate --project ./project
spxcutdb calibration status --project ./project
```

The resolver records product path, detector, release/version metadata, SHA256,
validation status, and provenance. Missing required calibration blocks
photometry and prevents science recommendation. Calibration products are never
deleted by cutout cleanup.
