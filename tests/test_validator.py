from __future__ import annotations

import logging

from astropy.io import fits

from conftest import make_synthetic_cutout
from spherex_cutoutdb.config import load_config, write_default_config
from spherex_cutoutdb.validator import validate_cutout


def _config(tmp_path, tiny_catalog_path):
    cfg_path = write_default_config(tmp_path, tiny_catalog_path)
    return load_config(tmp_path, cfg_path)


def test_validator_valid_cutout_passes(tmp_path, tiny_catalog_path, synthetic_cutout):
    cfg = _config(tmp_path / "project", tiny_catalog_path)
    result = validate_cutout(synthetic_cutout, cfg)
    assert result.status in {"passed", "passed_with_warnings"}
    assert result.required_hdus_present
    assert result.psf_metadata["psf_hdu_present"]
    assert result.sha256


def test_validator_missing_psf_fails(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path / "project", tiny_catalog_path)
    path = make_synthetic_cutout(tmp_path / "missing_psf.fits", missing="PSF")
    result = validate_cutout(path, cfg)
    assert result.status in {"failed_psf_hdu", "failed_missing_hdu"}


def test_validator_missing_zodi_passes_default_science_validation(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path / "project", tiny_catalog_path)
    path = make_synthetic_cutout(tmp_path / "missing_zodi.fits")
    with fits.open(path, memmap=False, lazy_load_hdus=False) as hdul:
        hdus = [hdu.copy() for hdu in hdul if hdu.name.upper() != "ZODI"]
    fits.HDUList(hdus).writeto(path, overwrite=True)

    result = validate_cutout(path, cfg)

    assert result.status in {"passed", "passed_with_warnings"}
    assert result.required_hdus_present
    assert result.zodi_shape is None


def test_validator_bad_shape_fails(tmp_path, tiny_catalog_path):
    cfg = _config(tmp_path / "project", tiny_catalog_path)
    path = make_synthetic_cutout(tmp_path / "bad_shape.fits", bad_shape=True)
    result = validate_cutout(path, cfg)
    assert result.status == "failed_invalid_fits"


def test_validator_suppresses_astropy_wcs_sip_info(tmp_path, tiny_catalog_path, caplog):
    cfg = _config(tmp_path / "project", tiny_catalog_path)
    path = make_synthetic_cutout(tmp_path / "sip_without_ctype_suffix.fits")
    with fits.open(path, mode="update") as hdul:
        header = hdul["IMAGE"].header
        header["A_ORDER"] = 2
        header["B_ORDER"] = 2
        header["A_0_2"] = 1.0e-8
        header["B_2_0"] = 1.0e-8
    caplog.set_level(logging.INFO, logger="astropy.wcs.wcs")
    result = validate_cutout(path, cfg)
    assert result.status in {"passed", "passed_with_warnings"}
    assert not any("Inconsistent SIP distortion information" in record.getMessage() for record in caplog.records)
