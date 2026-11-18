#!/usr/bin/env bash
# Stage 2B on macOS: controls, measurement on the frozen corpus2b, verifier controls, floor summary, contact sheets.
set -euo pipefail
cd "$(dirname "$0")"
fonts/fetch_fonts.sh >/dev/null
uv sync -q --frozen
P=.venv/bin/python
$P -m a08.controls2b stage2b-macos
$P -m a08.run2b stage2b-macos
$P -m a08.cv2b stage2b-macos
$P -m a08.summary2b stage2b-macos
$P -m a08.sheets2b stage2b-macos
