#!/usr/bin/env bash
# Family C: Chromium print (Google Chrome stable, headless, macOS). Empty disposable profile, file:// only.
set -euo pipefail
cd "$(dirname "$0")/.."
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p corpus/C
for i in 1 2 3 4; do
  prof=$(mktemp -d)
  "$CH" --headless --disable-gpu --no-pdf-header-footer --run-all-compositor-stages-before-draw --no-first-run \
        --user-data-dir="$prof" --print-to-pdf="$PWD/corpus/C/C$i.pdf" "file://$PWD/corpus/sources/html/T$i.html" 2>/dev/null
  rm -rf "$prof"
done
"$CH" --version > corpus/C/exporter-version.txt
ls -la corpus/C
