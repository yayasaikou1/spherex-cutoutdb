from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tiny_catalog_path() -> Path:
    return Path(__file__).parent / "data" / "tiny_sources.csv"


def make_synthetic_cutout(path: Path, *, missing: str | None = None, bad_shape: bool = False) -> Path:
    image = np.ones((8, 8), dtype="f4")
    flags = np.zeros((8, 8), dtype="i2")
    variance = np.ones((8, 8), dtype="f4")
    zodi = np.ones((7, 8), dtype="f4") if bad_shape else np.ones((8, 8), dtype="f4")
    header = fits.Header()
    header["CTYPE1"] = "RA---TAN"
    header["CTYPE2"] = "DEC--TAN"
    header["CRVAL1"] = 210.80227
    header["CRVAL2"] = 54.34895
    header["CRPIX1"] = 4.0
    header["CRPIX2"] = 4.0
    header["CD1_1"] = -0.0002777778
    header["CD1_2"] = 0.0
    header["CD2_1"] = 0.0
    header["CD2_2"] = 0.0002777778
    header["DETECTOR"] = 3
    header["PROCVER"] = "l2b-v20"
    hdus = [fits.PrimaryHDU()]
    candidates = {
        "IMAGE": fits.ImageHDU(image, header=header, name="IMAGE"),
        "FLAGS": fits.ImageHDU(flags, name="FLAGS"),
        "VARIANCE": fits.ImageHDU(variance, name="VARIANCE"),
        "ZODI": fits.ImageHDU(zodi, name="ZODI"),
        "PSF": fits.ImageHDU(np.ones((2, 5, 5), dtype="f4"), name="PSF"),
        "WCS-WAVE": fits.BinTableHDU.from_columns(
            [fits.Column(name="WAVELENGTH", array=np.array([1.0, 2.0]), format="E")],
            name="WCS-WAVE",
        ),
    }
    for name, hdu in candidates.items():
        if name != missing:
            hdus.append(hdu)
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList(hdus).writeto(path, overwrite=True)
    return path


@pytest.fixture
def synthetic_cutout(tmp_path: Path) -> Path:
    return make_synthetic_cutout(tmp_path / "synthetic.fits")
