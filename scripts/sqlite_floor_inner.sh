#!/usr/bin/env bash
# Inside the container: build an OLD SQLite from the official amalgamation, make THIS Python's own
# sqlite3 module load it (LD_LIBRARY_PATH on the dynamic libsqlite3), prove the version from inside
# the interpreter, then run the suite through the product's real driver path (aiosqlite -> sqlite3).
set -uo pipefail
OUT="${OUT:-/out}"; mkdir -p "$OUT"
export UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_NO_PROGRESS=1 PIP_ROOT_USER_ACTION=ignore
cd /work
summary() { echo "$*" | tee -a "$OUT/summary.txt"; }
: > "$OUT/summary.txt"
pip install --quiet uv >/dev/null 2>&1
uv sync --group dev --frozen > "$OUT/sync.log" 2>&1
PY=/opt/venv/bin/python

build_sqlite() { # build_sqlite <tarball-dir-name> <year> <autoconf-number> <version>
  local year="$1" num="$2" ver="$3" prefix="/opt/sqlite-$3"
  mkdir -p /tmp/sqlite-src && cd /tmp/sqlite-src
  curl -fsSL "https://www.sqlite.org/$year/sqlite-autoconf-$num.tar.gz" -o "sqlite-$ver.tar.gz" || return 1
  tar xzf "sqlite-$ver.tar.gz" && cd "sqlite-autoconf-$num"
  ./configure --prefix="$prefix" --disable-static >/dev/null 2>&1 && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1
  cd /work
}

for spec in "2022 3380500 3.38.5" "2022 3370200 3.37.2"; do
  set -- $spec
  year=$1; num=$2; ver=$3
  summary "== SQLite $ver (built from sqlite.org/$year/sqlite-autoconf-$num.tar.gz)"
  if ! build_sqlite "$year" "$num" "$ver"; then summary "BLOCKED: could not build SQLite $ver"; continue; fi
  export LD_LIBRARY_PATH="/opt/sqlite-$ver/lib"
  summary "in-process: $($PY -c 'import sqlite3, aiosqlite, sys; print("python", sys.version.split()[0], "| sqlite3.sqlite_version =", sqlite3.sqlite_version, "| aiosqlite", aiosqlite.__version__)')"
  if [ "$ver" = "3.38.5" ]; then
    export CCE_EXPECT_SQLITE_VERSION="$ver" CCE_REQUIRE_POSTGRES=1 CCE_TEST_POSTGRES_URL="postgresql+asyncpg://cce_admin:cce_test_pw@cce-linux-pg17:5432/postgres"
    "$PY" -m pytest -q -rs --junitxml="$OUT/junit-sqlite-$ver.xml" > "$OUT/pytest-sqlite-$ver.log" 2>&1
    summary "pytest with sqlite3 $ver: exit=$? | $(tail -n 1 "$OUT/pytest-sqlite-$ver.log")"
    unset CCE_EXPECT_SQLITE_VERSION CCE_REQUIRE_POSTGRES CCE_TEST_POSTGRES_URL
  else
    # below the declared floor the product must refuse to start (real library, not a monkeypatch)
    "$PY" - > "$OUT/below-floor-$ver.log" 2>&1 <<'PY'
import asyncio, sqlite3, tempfile, os
from cawbase_content_engine import ContentEngine, EngineConfig, InvalidConfigurationError
async def main():
    e = ContentEngine(EngineConfig(database_url=f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/x.db"))
    try:
        await e.start()
    except InvalidConfigurationError as exc:
        print("REFUSED:", exc.message)
        return 0
    print("NOT REFUSED"); return 1
raise SystemExit(asyncio.run(main()))
PY
    summary "below-floor start() with sqlite3 $ver: exit=$? | $(tail -n 1 "$OUT/below-floor-$ver.log")"
  fi
  unset LD_LIBRARY_PATH
done
summary "== done"
