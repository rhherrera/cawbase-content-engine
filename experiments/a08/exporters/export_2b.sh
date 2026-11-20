#!/usr/bin/env bash
# Stage 2B exports of the NEW template T5 for the agent's families (L5, C5, M5) into a staging dir; intake freezes them.
# Same flows and options as stage 2 (export_l.sh, export_c.sh, export_m.swift). T1 cases reuse the stage-2 PDFs.
#   exporters/export_2b.sh <staging_dir>
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:?staging dir}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
# C5: Chromium print, empty disposable profile, file:// only
CH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
prof=$(mktemp -d)
"$CH" --headless --disable-gpu --no-pdf-header-footer --run-all-compositor-stages-before-draw --no-first-run \
      --user-data-dir="$prof" --print-to-pdf="$OUT/C5.pdf" "file://$PWD/corpus2b/sources/html/T5.html" 2>/dev/null
rm -rf "$prof"
"$CH" --version > "$OUT/C5.exporter-version.txt"
# L5: LibreOffice Writer in cce-a08:1 (docx -> odt -> writer_pdf_Export, same options as stage 2)
OPTS='{"ExportBookmarks":{"type":"boolean","value":"true"},"UseTaggedPDF":{"type":"boolean","value":"false"},"SelectPdfVersion":{"type":"long","value":"0"}}'
docker run --rm --network none -v "$PWD/fonts:/home/runner/.fonts:ro" -v "$PWD/corpus2b/sources:/s:ro" -v "$OUT:/o" -e OPTS="$OPTS" cce-a08:1 bash -c '
  set -e; fc-cache -f >/dev/null 2>&1 || true; mkdir -p /tmp/o /tmp/p; cp /s/T5.docx /tmp/o/
  cd /tmp/o && soffice --headless --convert-to odt T5.docx >/dev/null 2>&1
  soffice --headless --convert-to "pdf:writer_pdf_Export:$OPTS" --outdir /tmp/p /tmp/o/T5.odt >/dev/null 2>&1; cp /tmp/p/T5.pdf /o/L5.pdf
  soffice --version > /o/L5.exporter-version.txt; echo "$OPTS" > /o/L5.export-options.json'
# M5: macOS print path (AppKit docx -> NSPrintOperation), fonts registered for the process only
swift exporters/export_m.swift fonts corpus2b/sources "$OUT" T5.docx=M5 > "$OUT/M5.exporter-version.txt"
ls -la "$OUT"
