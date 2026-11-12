#!/usr/bin/env bash
# Linux validation in Docker (NOT GitHub Actions). Disposable network + PostgreSQL containers, exclusive names.
#   bash scripts/linux_docker_validate.sh [OUT_DIR]
# Requires docker. Uses a snapshot of the working tree (tracked + untracked, honouring .gitignore).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-$(mktemp -d)/linux-evidence}"
mkdir -p "$OUT"
NET=cce-linux-net
VERS="${PG_VERSIONS:-15 16 17 18}"
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
for v in $VERS; do
  docker rm -f "cce-linux-pg$v" >/dev/null 2>&1 || true
  docker run -d --name "cce-linux-pg$v" --network "$NET" -e POSTGRES_USER=cce_admin \
    -e POSTGRES_PASSWORD=cce_test_pw "postgres:$v-alpine" >/dev/null
done
for v in $VERS; do
  until docker exec "cce-linux-pg$v" pg_isready -U cce_admin >/dev/null 2>&1; do sleep 1; done
done
# SNAPSHOT=head  -> exactly the committed state (git archive HEAD); default: working tree
# (tracked + untracked, honouring .gitignore). COPYFILE_DISABLE/--no-xattrs keep macOS AppleDouble
# (._*) files out of the Linux snapshot.
snapshot() {
  if [ "${SNAPSHOT:-worktree}" = head ]; then git archive --format=tar HEAD
  else git ls-files -z --cached --others --exclude-standard | COPYFILE_DISABLE=1 tar --no-xattrs --null -T - -cf -
  fi
}
snapshot | \
  docker run --rm -i --network "$NET" -v "$OUT:/out" -e OUT=/out -e PG_VERSIONS="$VERS" \
    -e LOWEST_PG="${LOWEST_PG:-17}" -e SNAPSHOT_KIND="${SNAPSHOT:-worktree}" -e GIT_REV="$(git rev-parse HEAD)" python:3.13-bookworm \
    bash -c 'mkdir -p /work && tar -xf - -C /work && bash /work/scripts/linux_inner.sh'
echo "evidence: $OUT"
