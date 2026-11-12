#!/usr/bin/env bash
# Runs INSIDE the Linux container (python:3.13 on Debian). Invoked by scripts/linux_docker_validate.sh.
# Every stage logs to $OUT; a failing stage does not stop the independent ones.
set -uo pipefail
OUT="${OUT:-/out}"
PGVERS="${PG_VERSIONS:-15 16 17 18}"
mkdir -p "$OUT"
export UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_NO_PROGRESS=1 PIP_ROOT_USER_ACTION=ignore
cd /work

summary() { echo "$*" | tee -a "$OUT/summary.txt"; }
stage() { # stage <name> <command...>
  local name="$1"; shift
  echo "+ $*" > "$OUT/$name.log"
  "$@" >> "$OUT/$name.log" 2>&1
  local rc=$?
  summary "$name: exit=$rc | $(tail -n 1 "$OUT/$name.log" | cut -c1-150)"
}

: > "$OUT/summary.txt"
summary "== snapshot: ${SNAPSHOT_KIND:-?} (repo HEAD at launch: ${GIT_REV:-?})"
summary "== platform"
summary "$(uname -srm) | $(. /etc/os-release && echo "$PRETTY_NAME")"
python - <<'PY' | tee -a "$OUT/summary.txt"
import platform, sqlite3, sys
print("python", platform.python_version(), "| sqlite3.sqlite_version (in this process)", sqlite3.sqlite_version)
PY
pip install --quiet uv >/dev/null 2>&1
summary "$(uv --version)"
uv sync --group dev --frozen > "$OUT/sync.log" 2>&1 || { summary "sync FAILED"; exit 1; }

summary "== static"
stage ruff-check uv run ruff check .
stage ruff-format uv run ruff format --check src tests examples scripts
stage mypy uv run mypy

summary "== suites (CCE_REQUIRE_POSTGRES=1; a missing PostgreSQL is a failure)"
for v in $PGVERS; do
  export CCE_REQUIRE_POSTGRES=1
  export CCE_TEST_POSTGRES_URL="postgresql+asyncpg://cce_admin:cce_test_pw@cce-linux-pg$v:5432/postgres"
  summary "PostgreSQL server $v: $(uv run python - <<PY
import asyncio, asyncpg
async def m():
    c = await asyncpg.connect("postgresql://cce_admin:cce_test_pw@cce-linux-pg$v:5432/postgres")
    print(await c.fetchval("show server_version")); await c.close()
asyncio.run(m())
PY
)"
  stage "pytest-pg$v" uv run pytest -q -rs --junitxml="$OUT/junit-pg$v.xml"
done

if [ -n "${LOWEST_PG:-}" ]; then
  export CCE_REQUIRE_POSTGRES=1 CCE_TEST_POSTGRES_URL="postgresql+asyncpg://cce_admin:cce_test_pw@cce-linux-pg$LOWEST_PG:5432/postgres"
  summary "== lowest declared bounds (installed versions printed from the resolved environment)"
  uv run --isolated --resolution lowest-direct --group dev python - > "$OUT/lowest-versions.txt" 2> "$OUT/lowest-versions.err" <<'PY'
import importlib.metadata as m
names = ["sqlalchemy","pydantic","asyncpg","aiosqlite","alembic","fastapi","uvicorn","pytest","pytest-asyncio","httpx"]
print({n: m.version(n) for n in names})
PY
  summary "lowest versions: $(cat "$OUT/lowest-versions.txt")"
  stage pytest-lowest uv run --isolated --resolution lowest-direct --group dev pytest -q -rs --junitxml="$OUT/junit-lowest.xml"
fi

summary "== distribution (wheel + sdist installed into clean venvs outside the tree; AC-07 uses PostgreSQL ${DIST_PG:-17})"
export CCE_REQUIRE_POSTGRES=1 CCE_TEST_POSTGRES_URL="postgresql+asyncpg://cce_admin:cce_test_pw@cce-linux-pg${DIST_PG:-17}:5432/postgres"
stage pytest-dist uv run pytest -m dist -q -rs
stage artifact-audit bash -c 'rm -rf /tmp/dist && uv build --out-dir /tmp/dist . >/dev/null 2>&1 && uv run python scripts/audit_artifacts.py /tmp/dist/*'
summary "== import measurements from the installed wheel (clean venv, Linux)"
uv run python scripts/measure_distribution.py --cold-runs 5 --warm-runs 15 > "$OUT/measure-base.json" 2> "$OUT/measure-base.err"
summary "measure-base: exit=$? | $(python - <<PY
import json; d=json.load(open("$OUT/measure-base.json"))
c=d["cold_first_import (bytecode cleared before each sample)"]; w=d["warm_import (new processes, bytecode available)"]
print("cold median/max/n = %s/%s/%s ms; warm median/max/n = %s/%s/%s ms; size %s KiB" % (c["median_ms"], c["max_ms"], c["samples"], w["median_ms"], w["max_ms"], w["samples"], d["installed_size_kib_excluding_pycache"]))
PY
)"
summary "== done"
