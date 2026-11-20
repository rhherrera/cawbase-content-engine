#!/usr/bin/env bash
# Stage 2B on Linux local (cce-a08:1, Debian 13, aarch64): same code, same frozen inputs; qpdf --check runs here.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$(cd ../.. && pwd)
docker run --rm -v "$REPO:/repo" -w /repo/experiments/a08 -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uvc cce-a08:1 bash -c '
  set -e; fonts/fetch_fonts.sh >/dev/null; uv sync -q --frozen
  P=/tmp/venv/bin/python
  $P -m a08.controls2b stage2b-linux | grep -E "^(CA|CR|CS|summary)"
  $P -m a08.run2b stage2b-linux | grep -E "^[CLMWG][0-9]"
  $P -m a08.cv2b stage2b-linux | grep -E "^(CV|detected)"
  $P -m a08.summary2b stage2b-linux | head -1
  $P -c "import platform,pikepdf,pypdfium2,pypdf;print(platform.platform(), pikepdf.__version__, pypdfium2.version.PDFIUM_INFO, pypdf.__version__)"
  qpdf --version | head -1; pdftotext -v 2>&1 | head -1'
