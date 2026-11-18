#!/usr/bin/env bash
# A07 evidence: opening without repair and visual inspection with LibreOffice on Linux (Docker).
#   bash scripts/a07_render_evidence.sh OUT_DIR [IMAGE]
# 1. E01: a synthetic document RE-SAVED by LibreOffice Writer (an ordinary editor) inside the Linux image;
# 2. original / protected / restored DOCX of representative fixtures through the REAL pipeline (pytest, SQLite,
#    tests/test_a07_evidence.py) on the host;
# 3. each DOCX opened and converted headless by LibreOffice (log kept: any repair / error line is evidence) and
#    rendered with Poppler; versions, platform and the fonts embedded in every PDF are recorded.
# Requires docker and an image with LibreOffice + Poppler (default: cce-a04-render, Debian 12, from A04).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(mkdir -p "${1:?OUT_DIR}" && cd "$1" && pwd)"
IMAGE="${2:-cce-a04-render:latest}"
mkdir -p "$OUT/renders"
uv run python - "$OUT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from tests.test_a07_evidence import fixtures  # noqa: E402  (synthetic, built by construction)
import os
os.environ.setdefault("CCE_A07_EVIDENCE_DIR", sys.argv[1])
Path(sys.argv[1], "E01_source.docx").write_bytes(fixtures()["F01"])
PY
docker run --rm -v "$OUT:/out" "$IMAGE" bash -c '
  set -e
  export HOME=/tmp
  mkdir -p /tmp/e01
  soffice --headless --norestore --convert-to "docx:MS Word 2007 XML" --outdir /tmp/e01 /out/E01_source.docx \
    > /out/E01_resave.log 2>&1
  cp /tmp/e01/E01_source.docx /out/E01_libreoffice.docx
'
CCE_A07_EVIDENCE_DIR="$OUT" uv run pytest -q tests/test_a07_evidence.py -k sqlite -p no:randomly > "$OUT/pipeline.log" 2>&1
docker run --rm -v "$OUT:/out" "$IMAGE" bash -c '
  set -e
  export HOME=/tmp
  {
    echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "platform: $(uname -srm)"; cat /etc/debian_version | sed "s/^/debian: /"
    echo "libreoffice: $(libreoffice --version)"
    echo "poppler: $(pdftoppm -v 2>&1 | head -1)"
    echo "fonts installed:"; fc-list : family | sort -u | sed "s/^/  /"
  } > /out/environment.txt
  cd /out
  for f in F*_*.docx E01_*_*.docx E01_original.docx E01_protected.docx E01_restored.docx; do
    [ -f "$f" ] || continue
    base="${f%.docx}"
    soffice --headless --norestore --convert-to pdf --outdir /tmp/pdf "$f" > "renders/$base.convert.log" 2>&1 || echo "CONVERT_FAILED" >> "renders/$base.convert.log"
    if [ -f "/tmp/pdf/$base.pdf" ]; then
      cp "/tmp/pdf/$base.pdf" "renders/$base.pdf"
      pdffonts "renders/$base.pdf" > "renders/$base.fonts.txt" 2>&1 || true
      pdftoppm -r 70 -png "renders/$base.pdf" "renders/$base"
    fi
  done
  grep -il "repair\|corrupt\|error" renders/*.convert.log > renders/convert-warnings.txt || true
'
echo "evidence: $OUT"
