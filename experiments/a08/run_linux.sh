#!/usr/bin/env bash
# Linux run of the D-A04-04 check in the cce-a08:1 image (Debian 13, aarch64): same code, same frozen inputs.
set -euo pipefail
cd "$(dirname "$0")"
REPO=$(cd ../.. && pwd)
docker run --rm -v "$REPO:/repo" -w /repo/experiments/a08 -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uvc cce-a08:1 bash -c '
  set -e; fonts/fetch_fonts.sh >/dev/null; uv sync -q --frozen
  P=/tmp/venv/bin/python
  $P -m a08.controls linux | grep -E "^(CA|CR|CS)"
  $P -m a08.run linux | grep -E "^[CLMWG][0-9]"
  $P -m a08.run --variant=TB+AT+RZ linux-inf | grep -E "^[CLMWG][0-9]"
  $P -m a08.controls linux-inf --variant=TB+RZ | grep -E "^(CS)"
  $P -m a08.cv linux-inf | grep -E "^CV"
  $P -c "import platform,pikepdf,pypdfium2,pypdf;print(platform.platform(), pikepdf.__version__, pypdfium2.version.PDFIUM_INFO, pypdf.__version__)"
  qpdf --version | head -1; pdftotext -v 2>&1 | head -1'
