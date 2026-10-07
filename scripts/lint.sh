#!/usr/bin/env bash
# Run exactly what CI runs for the hub (ruff lint + format check).
# Usage: scripts/lint.sh          check only, same as CI
#        scripts/lint.sh --fix    apply lint fixes and format in place
set -euo pipefail
cd "$(dirname "$0")/../hub"
command -v ruff >/dev/null || { echo "ruff not found: pip install -e 'hub[dev]' (or pipx install ruff)" >&2; exit 127; }
paths=(src tests examples)
if [[ "${1:-}" == "--fix" ]]; then
  ruff check --fix "${paths[@]}"
  ruff format "${paths[@]}"
else
  ruff check "${paths[@]}"
  ruff format --check "${paths[@]}"
fi
