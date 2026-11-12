#!/usr/bin/env bash
# Declared SQLite floor (>= 3.38) in a Linux container, through the product's own driver path.
#   bash scripts/sqlite_floor_validate.sh [OUT_DIR]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-$(mktemp -d)/sqlite-floor-evidence}"; mkdir -p "$OUT"
NET=cce-linux-net
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
docker rm -f cce-linux-pg17 >/dev/null 2>&1 || true
docker run -d --name cce-linux-pg17 --network "$NET" -e POSTGRES_USER=cce_admin -e POSTGRES_PASSWORD=cce_test_pw postgres:17-alpine >/dev/null
until docker exec cce-linux-pg17 pg_isready -U cce_admin >/dev/null 2>&1; do sleep 1; done
git ls-files -z --cached --others --exclude-standard | COPYFILE_DISABLE=1 tar --no-xattrs --null -T - -cf - | \
  docker run --rm -i --network "$NET" -v "$OUT:/out" -e OUT=/out python:3.13-bookworm \
    bash -c 'mkdir -p /work && tar -xf - -C /work && bash /work/scripts/sqlite_floor_inner.sh'
echo "evidence: $OUT"
