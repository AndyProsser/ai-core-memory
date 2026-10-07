#!/usr/bin/env bash
# One-time per clone: use the versioned hooks in .githooks/.
set -euo pipefail
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "git hooks enabled (.githooks/pre-commit formats + lints staged hub/*.py)"
