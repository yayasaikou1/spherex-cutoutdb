from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


REQUIRED_WHEEL_MEMBERS = {
    "spherex_cutoutdb/calibration/__init__.py",
    "spherex_cutoutdb/photometry/__init__.py",
    "spherex_cutoutdb/integrated_workflow.py",
    "spherex_cutoutdb/downloader.py",
    "spherex_cutoutdb/schema.sql",
}

FORBIDDEN_RELEASE_FRAGMENTS = {
    "docs/codex/",
    "spherex_pipeline_md_plan/",
    "__pycache__",
    ".DS_Store",
    "Archive.zip",
    "Archive 2.zip",
    "dist/spherex_cutoutdb-0.1.0",
}


def test_version_metadata_is_release_candidate():
    import spherex_cutoutdb

    assert spherex_cutoutdb.__version__ == "1.0.0rc1"

    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.0.0rc1"' in pyproject
    assert "conservative PSF forced-photometry workflow" in pyproject


def test_release_build_artifacts_include_current_science_modules(tmp_path):
    pytest.importorskip("build")
    out_dir = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out_dir)],
        check=True,
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    wheel = next(out_dir.glob("spherex_cutoutdb-1.0.0rc1-*.whl"))
    sdist = next(out_dir.glob("spherex_cutoutdb-1.0.0rc1.tar.gz"))

    with zipfile.ZipFile(wheel) as zf:
        wheel_names = set(zf.namelist())
        assert REQUIRED_WHEEL_MEMBERS <= wheel_names
        assert not any(_has_forbidden_fragment(name) for name in wheel_names)
        metadata_name = next(name for name in wheel_names if name.endswith(".dist-info/METADATA"))
        metadata = zf.read(metadata_name).decode("utf-8")
        assert "Version: 1.0.0rc1" in metadata
        assert "conservative PSF forced-photometry workflow" in metadata
        assert "does not perform aperture photometry" not in metadata
        assert "downstream SPHEREx photometry layer" in metadata

    with tarfile.open(sdist) as tf:
        sdist_names = tf.getnames()
        assert not any(_has_forbidden_fragment(name) for name in sdist_names)
        assert any(name.endswith("/src/spherex_cutoutdb/calibration/__init__.py") for name in sdist_names)
        assert any(name.endswith("/src/spherex_cutoutdb/photometry/__init__.py") for name in sdist_names)
        assert any(name.endswith("/src/spherex_cutoutdb/integrated_workflow.py") for name in sdist_names)


def _has_forbidden_fragment(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return any(fragment in normalized for fragment in FORBIDDEN_RELEASE_FRAGMENTS)
