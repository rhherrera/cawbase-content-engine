#!/usr/bin/env bash
# macOS run of the D-A04-04 check: controls (rules), official corpus run, informative variants, verifier controls.
set -euo pipefail
cd "$(dirname "$0")"
fonts/fetch_fonts.sh >/dev/null
uv sync -q --frozen
P=.venv/bin/python
$P -m a08.controls macos
$P -m a08.run macos
$P -m a08.run --variant=TB+AT+RZ macos-inf
$P -m a08.controls macos-inf --variant=TB+RZ
$P -m a08.cv macos-inf
