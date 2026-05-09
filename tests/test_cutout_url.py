from __future__ import annotations

from pathlib import Path

from spherex_cutoutdb.filenames import deterministic_cutout_path, parse_parent_metadata, safe_slug
from spherex_cutoutdb.irsa_cutouts import arcsec_to_url_size_deg, build_cutout_url


def test_cutout_url_builder_plain_parent():
    url = build_cutout_url(
        "https://irsa.ipac.caltech.edu/ibe/data/spherex/qr2/level2/x.fits",
        210.80227,
        54.34895,
        arcsec_to_url_size_deg(60),
    )
    assert url.endswith("?center=210.8022700000,54.3489500000&size=0.01666666667")


def test_cutout_url_builder_existing_query():
    url = build_cutout_url("https://example.test/x.fits?foo=bar", 1, -2, 0.1)
    assert "foo=bar" in url
    assert "center=1.0000000000,-2.0000000000" in url
    assert "size=0.1" in url


def test_filename_path_determinism():
    parent = "level2_2025W19_2B_0073_2D3_spx_l2b-v20-2025-247.fits"
    parsed = parse_parent_metadata(parent)
    path1 = deterministic_cutout_path(Path("data"), "M 101", "spherex_qr2", parsed["planning_period"], parsed["processing_version"], parsed["detector_id"], parent, 60.0, "abcdef123456")
    path2 = deterministic_cutout_path(Path("data"), "M 101", "spherex_qr2", parsed["planning_period"], parsed["processing_version"], parsed["detector_id"], parent, 60.0, "abcdef123456")
    assert path1 == path2
    assert "M_101" in str(path1)
    assert parsed["detector_id"] == 3
    assert safe_slug("a b/c") == "a_b_c"
