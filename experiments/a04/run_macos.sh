#!/usr/bin/env bash
# macOS run (no network needed after `uv sync`). Usage: bash run_macos.sh   (from experiments/a04)
set -euo pipefail
cd "$(dirname "$0")"
uv sync --frozen --python 3.13
rm -rf outputs/renders/macos outputs/samples/macos
uv run --no-sync python run_all.py macos
uv run --no-sync python licenses.py outputs/environment-macos.json > /dev/null
