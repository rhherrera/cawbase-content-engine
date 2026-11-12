#!/usr/bin/env bash
# Linux validation of the A04 experiments (Docker). Step 1 needs the network ONLY to install the pinned wheels and
# the image packages; step 2 (the processing) runs with --network none. Usage: bash run_linux.sh   (from experiments/a04)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
REV="$(git -C "$REPO" rev-parse HEAD)"; DIRTY="$(git -C "$REPO" status --porcelain -- experiments/a04 ':(exclude)experiments/a04/outputs' | head -c 500)"
WORK="$(mktemp -d)"; rsync -a --exclude .venv --exclude .git --exclude __pycache__ --exclude outputs/renders --exclude outputs/samples "$REPO/" "$WORK/"
docker build -q -t cce-a04-render "$HERE/docker" >/dev/null
docker volume create cce_a04_venv >/dev/null
docker run --rm -v "$WORK:/repo" -v cce_a04_venv:/venv -e UV_PROJECT_ENVIRONMENT=/venv -w /repo/experiments/a04 cce-a04-render uv sync --frozen --python 3.13
docker run --rm --network none -e GIT_REV="$REV" -e GIT_DIRTY="$DIRTY" -v "$WORK:/repo" -v cce_a04_venv:/venv -e UV_PROJECT_ENVIRONMENT=/venv -w /repo/experiments/a04 cce-a04-render \
  sh -c "uv run --frozen --no-sync python run_all.py linux && uv run --frozen --no-sync python licenses.py outputs/environment-linux.json > /dev/null && dpkg-query -W -f='\${Package} \${Version}\n' libreoffice-writer poppler-utils fonts-liberation fonts-dejavu-core qpdf > outputs/linux-packages.txt"
rm -rf "$HERE/outputs/renders/linux" "$HERE/outputs/samples/linux"; mkdir -p "$HERE/outputs/renders" "$HERE/outputs/samples"
cp "$WORK/experiments/a04/outputs/results-linux.json" "$WORK/experiments/a04/outputs/environment-linux.json" "$WORK/experiments/a04/outputs/linux-packages.txt" "$HERE/outputs/"
cp -R "$WORK/experiments/a04/outputs/renders/linux" "$HERE/outputs/renders/"; cp -R "$WORK/experiments/a04/outputs/samples/linux" "$HERE/outputs/samples/"; rm -rf "$HERE/outputs/samples/linux/_lo"
echo "done: revision $REV (dirty: ${DIRTY:-no})"
