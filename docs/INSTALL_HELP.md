# spherex-cutoutdb install help

This guide installs `spherex-cutoutdb` for both the original Quick Start
workflow and the integrated catalog-to-spectrum workflow. It assumes conda is
already installed.

Use conda-forge for the scientific stack, then install this repository with
`pip --no-deps`. This avoids pip replacing conda binary packages such as
`astropy`, `photutils`, `pyarrow`, and `matplotlib`.

## Dependency map

| Workflow layer | Main dependencies |
| --- | --- |
| Core package and CLI | `python>=3.10`, `setuptools`, `wheel`, `pydantic`, `PyYAML`, `rich`, `tqdm`, `pandas`, `pyarrow`, Python stdlib `sqlite3` |
| Catalog, discovery, planner, downloader | `astropy`, `pyvo`, `astroquery`, `requests`, `pandas` |
| Calibration cache | `astropy`, `numpy`, `requests`; durable files under project `cache/calibrations` |
| V5 photometry and integrated workflow | `numpy`, `astropy`, `photutils`, `matplotlib`, `pandas`, plus downloader and calibration dependencies |
| Development and tests | `pytest`, `pytest-cov`, `responses`, `build` |

## Recommended conda install

Run these commands from the repository root.

```bash
conda create -n spxcutdb python=3.11 -y
conda activate spxcutdb
conda config --env --add channels conda-forge
conda config --env --set channel_priority strict
conda install -y astropy photutils pyvo astroquery requests pydantic pyyaml rich tqdm pandas pyarrow matplotlib pytest pytest-cov responses build setuptools wheel pip
python -m pip install -e ".[dev]" --no-deps
```

For a runtime-only install without test/build extras, install the runtime
packages and then install the repository without dependencies:

```bash
conda create -n spxcutdb python=3.11 -y
conda activate spxcutdb
conda config --env --add channels conda-forge
conda config --env --set channel_priority strict
conda install -y astropy photutils pyvo astroquery requests pydantic pyyaml rich tqdm pandas pyarrow matplotlib setuptools wheel pip
python -m pip install -e . --no-deps
```

If you already created the environment, activate it before running any
`spxcutdb` command:

```bash
conda activate spxcutdb
```

## macOS

Install Xcode command line tools only if `git`, compilers, or headers are
missing:

```bash
xcode-select --install
```

Then use the recommended conda install above. On Apple Silicon, conda-forge
packages should install native `osx-arm64` builds when the conda installation is
native arm64. Avoid mixing Intel and Apple Silicon conda environments.

Verify the installed command:

```bash
spxcutdb --help
```

## WSL / Ubuntu

Install basic system tools first:

```bash
sudo apt-get update
sudo apt-get install -y build-essential git curl ca-certificates
```

Keep the repository and project data inside the WSL Linux filesystem, for
example under `~/work/`, not under `/mnt/c/`. The downloader and SQLite state
files are much slower and more fragile on mounted Windows paths.

For large downloader runs, raise the open-file limit in the current shell:

```bash
ulimit -n 4096
```

Then use the recommended conda install above.

## Linux CentOS 7.9

CentOS 7.9 is old and end-of-life. Do not use the system Python for this
project. Use conda-forge binary packages and avoid pip source builds whenever
possible.

Install basic tools:

```bash
sudo yum install -y git curl ca-certificates bzip2 tar gzip make gcc gcc-c++
```

Create the conda environment with conda-forge runtime libraries:

```bash
conda create -n spxcutdb python=3.11 -y
conda activate spxcutdb
conda config --env --add channels conda-forge
conda config --env --set channel_priority strict
conda install -y libstdcxx-ng openssl ca-certificates certifi astropy photutils pyvo astroquery requests pydantic pyyaml rich tqdm pandas pyarrow matplotlib pytest pytest-cov responses build setuptools wheel pip
python -m pip install -e ".[dev]" --no-deps
```

If conda attempts to solve with very new packages that do not support the host,
keep Python at `3.11` and install from conda-forge rather than switching to pip
source builds.

## Verify the environment

Run these checks from the repository root:

```bash
python -m pip check
spxcutdb --help
python -c "import astropy, photutils, pyvo, astroquery, pandas, pyarrow, matplotlib, requests, pydantic, yaml, rich; import spherex_cutoutdb; print('OK')"
pytest -q
```

`pytest -q` is the full test suite. It should not require live IRSA network
access by default.

## Quick Start smoke

Use `input_catalog.csv` with unique `Name`, `RA_deg`, and `DEC_deg` columns.
The recommended smoke path is the integrated workflow because it records the
effective config, config hash, and CLI overrides for the run.

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name --force
spxcutdb config show --project ./project --effective --hash
spxcutdb config validate --project ./project
spxcutdb validate --project ./project --catalog input_catalog.csv
spxcutdb discover --project ./project --resume
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb run --project ./project --catalog input_catalog.csv --download-missing --resume --cleanup-cutouts success-after-source --qa-level standard
spxcutdb summary --project ./project
```

The older expert path downloads cutouts first and then runs downstream
photometry. Use it only when you need to debug the downloader or photometry
planner separately:

```bash
spxcutdb init ./project --catalog input_catalog.csv --target-id-column Name --force --no-include-deep
spxcutdb catalog validate --project ./project --verbose
spxcutdb catalog ingest --project ./project
spxcutdb discover --project ./project --concurrency 32 --verbose
spxcutdb plan --project ./project --export-plan
spxcutdb download --project ./project --max-workers 32 --verbose
spxcutdb coverage --project ./project
spxcutdb calibration sync --project ./project --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project ./project
spxcutdb photometry plan --project ./project
```

## Integrated workflow smoke

The integrated workflow plans first, skips valid photometry before requesting
cutouts, downloads missing cutouts through the existing downloader, runs V5
photometry, writes durable outputs, and safely cleans temporary cutouts.

You can also ask the integrated run to discover and sync calibration before
planning:

```bash
spxcutdb run --project ./project --catalog input_catalog.csv --discover --sync-calibration --download-missing --resume --cleanup-cutouts success-after-source --qa-level standard
```

## Troubleshooting

### `spxcutdb: command not found`

Activate the conda environment and reinstall the editable package:

```bash
conda activate spxcutdb
python -m pip install -e ".[dev]" --no-deps
python -m pip show spherex-cutoutdb
```

If the package is installed but the command is still missing, run:

```bash
python -m spherex_cutoutdb --help
```

### Binary import failures

Errors importing `astropy`, `photutils`, `pyarrow`, or `matplotlib` usually
mean pip replaced conda packages or the environment mixed incompatible channels.
Create a clean environment with strict conda-forge priority and reinstall the
repository with `--no-deps`.

### SSL or certificate errors

Update certificate packages inside the conda environment:

```bash
conda install -y -c conda-forge ca-certificates certifi openssl
python -c "import ssl; print(ssl.get_default_verify_paths())"
```

### `calibration_missing` and zero downloads

The integrated workflow checks required calibration before submitting cutouts to
the downloader. If the summary shows `calibration_missing`, sync and validate
calibration first:

```bash
spxcutdb calibration sync --project ./project --product required
spxcutdb calibration validate --project ./project
```

Then rerun with `--resume --download-missing`.

### WSL slow I/O

Move the repository and project directory from `/mnt/c/...` to a native Linux
path such as `~/work/...`. SQLite and many small FITS/JSON writes are much
slower on Windows-mounted paths.

### CentOS 7.9 compiler or source-build failures

Do not debug old system compilers first. Prefer conda-forge binaries and keep
the editable install as:

```bash
python -m pip install -e . --no-deps
```

If pip tries to compile scientific packages, the dependency was not installed
from conda-forge.

### Matplotlib cache or permission errors

The workflow writes PNG SED and QA products. If matplotlib cannot write its
cache in a batch environment, set a writable cache directory:

```bash
mkdir -p "$PWD/.matplotlib-cache"
export MPLCONFIGDIR="$PWD/.matplotlib-cache"
```
