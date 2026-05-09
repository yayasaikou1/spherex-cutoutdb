#!/usr/bin/env bash
set -euo pipefail

PROJECT="${1:-./demo_project}"

spxcutdb init "$PROJECT" --catalog examples/input_catalog.csv --target-id-column Name --force
spxcutdb config show --project "$PROJECT" --effective --hash
spxcutdb config validate --project "$PROJECT"
spxcutdb validate --project "$PROJECT" --catalog examples/input_catalog.csv
spxcutdb discover --project "$PROJECT" --resume --limit-sources 1
spxcutdb calibration sync --project "$PROJECT" --product required --download-source cloud --max-workers 8
spxcutdb calibration validate --project "$PROJECT"
spxcutdb run --project "$PROJECT" --catalog examples/input_catalog.csv --download-missing --resume --cleanup-cutouts success-after-source --qa-level standard
spxcutdb summary --project "$PROJECT"
