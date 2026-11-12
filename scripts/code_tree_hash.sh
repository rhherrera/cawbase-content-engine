#!/usr/bin/env bash
# Hash of the tracked-or-untracked-not-ignored CODE state (everything except docs/README/CLAUDE.md),
# using a throw-away index. Equal hashes before and after committing prove the tested code is the committed code.
set -euo pipefail
cd "$(dirname "$0")/.."
IDX="$(mktemp)"; rm -f "$IDX"
export GIT_INDEX_FILE="$IDX"
git add -A -- src tests scripts examples pyproject.toml uv.lock .github .gitignore >/dev/null 2>&1
git write-tree
rm -f "$IDX"
