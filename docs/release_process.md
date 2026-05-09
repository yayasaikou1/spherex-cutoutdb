# Release Process

The root `RELEASE.md` contains the full release workflow. This page is the
GitHub Pages summary.

Short version:

```bash
python -m compileall src
pytest -q
python -m build
git tag -a v1.0.0-rc.1 -m "spherex-cutoutdb v1.0.0-rc.1"
git push origin main
git push origin v1.0.0-rc.1
```

Attach the generated wheel and sdist from `dist/` to the GitHub Release. Do not
commit generated release artifacts.
