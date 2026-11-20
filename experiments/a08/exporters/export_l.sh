#!/usr/bin/env bash
# Family L: LibreOffice Writer (image cce-a08:1). docx -> odt (Writer opens and saves) -> writer_pdf_Export.
set -euo pipefail
cd "$(dirname "$0")/.."
OPTS='{"ExportBookmarks":{"type":"boolean","value":"true"},"UseTaggedPDF":{"type":"boolean","value":"false"},"SelectPdfVersion":{"type":"long","value":"0"}}'
mkdir -p corpus/L
docker run --rm --network none -v "$PWD/fonts:/home/runner/.fonts:ro" -v "$PWD/corpus:/w" -e OPTS="$OPTS" cce-a08:1 bash -c '
  set -e; fc-cache -f >/dev/null 2>&1 || true; mkdir -p /tmp/o /tmp/p
  for t in T1 T2 T3 T4; do cp /w/sources/$t.docx /tmp/o/; done
  cd /tmp/o && soffice --headless --convert-to odt T1.docx T2.docx T3.docx T4.docx >/dev/null 2>&1
  for i in 1 2 3 4; do soffice --headless --convert-to "pdf:writer_pdf_Export:$OPTS" --outdir /tmp/p /tmp/o/T$i.odt 2>&1 | grep -v "^$" || true; cp /tmp/p/T$i.pdf /w/L/L$i.pdf; done
  soffice --version > /w/L/exporter-version.txt; echo "$OPTS" > /w/L/export-options.json'
ls -la corpus/L
