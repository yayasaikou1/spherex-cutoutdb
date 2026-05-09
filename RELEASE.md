# Release Management

This project uses lightweight Git tags and GitHub Releases.

## Version Names

- Python package versions use PEP 440, for example `1.0.0rc1`.
- Git tags use release-style names, for example `v1.0.0-rc.1`.
- Final releases use tags such as `v1.0.0`.

## Normal Development

1. Work on feature branches from `main`.
2. Keep user-facing docs, tests, and `CHANGELOG.md` updated with code changes.
3. Run local validation before opening or merging a pull request:

```bash
python -m compileall src
pytest -q
python -m build
```

## Release Candidate

1. Confirm `pyproject.toml` and `src/spherex_cutoutdb/__init__.py` use the
   intended package version.
2. Update `CHANGELOG.md` and public docs.
3. Build and test from a clean checkout:

```bash
python -m pip install -e ".[dev]"
python -m compileall src
pytest -q
python -m build
```

4. Create an annotated tag:

```bash
git tag -a v1.0.0-rc.1 -m "spherex-cutoutdb v1.0.0-rc.1"
git push origin main
git push origin v1.0.0-rc.1
```

5. Create a GitHub Release from the tag and attach the wheel and sdist from
   `dist/`.

## Final Release

After release-candidate validation:

1. Update package version from `1.0.0rcN` to `1.0.0`.
2. Move changelog content under a final `1.0.0` heading.
3. Repeat the build/test/install smoke checks.
4. Tag and publish:

```bash
git tag -a v1.0.0 -m "spherex-cutoutdb v1.0.0"
git push origin main
git push origin v1.0.0
```

## Documentation Publishing

The repository includes a minimal MkDocs configuration. To preview docs:

```bash
python -m pip install mkdocs
mkdocs serve
```

To publish manually to GitHub Pages after the repository remote is configured:

```bash
mkdocs gh-deploy
```

Alternatively, configure GitHub Pages in the web UI to publish from a
documentation deployment workflow or a `gh-pages` branch.

