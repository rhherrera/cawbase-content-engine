"""Real processes: CLI and the standalone service. AC-29, AC-46, AC-95..102, AC-100/101."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import time

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine.constants import MIGRATION_LOCK_KEY

PREFIX = "CAWBASE_CONTENT_ENGINE_"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def env_for(url: str, **extra: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(PREFIX)}
    base[PREFIX + "DATABASE_URL"] = url
    base.update({PREFIX + k: v for k, v in extra.items()})
    return base


def cli(env: dict[str, str], *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cawbase_content_engine", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/",
    )


class Service:
    def __init__(self, env: dict[str, str], port: int, tmp_path, *args: str) -> None:
        self.port = port
        self.log = tmp_path / f"service-{port}.log"
        self._fh = open(self.log, "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "cawbase_content_engine", "serve", "--port", str(port), *args],
            env=env,
            stdout=self._fh,
            stderr=subprocess.STDOUT,
            cwd="/",
        )

    def logs(self) -> str:
        self._fh.flush()
        return self.log.read_text()

    async def wait_ready(self, timeout: float = 40) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"service exited early ({self.proc.returncode}): {self.logs()}")
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/readyz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        raise AssertionError(f"service not ready: {self.logs()}")

    async def wait_exit(self, timeout: float = 30) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (rc := self.proc.poll()) is not None:
                return rc
            await asyncio.sleep(0.1)
        raise AssertionError(f"service did not exit: {self.logs()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._fh.close()


@pytest.fixture
def service_factory(tmp_path):
    started: list[Service] = []

    def make(env: dict[str, str], *args: str) -> Service:
        s = Service(env, free_port(), tmp_path, *args)
        started.append(s)
        return s

    yield make
    for s in started:
        s.stop()


class TestCli:
    async def test_end_to_end_cli(self, db, tmp_path):  # AC-46, AC-95, AC-97
        backend, url = db
        env = env_for(url)
        assert cli(env, "version").stdout.strip() == "0.1.0"
        check = cli(env, "check")
        assert check.returncode == 4 and json.loads(check.stdout)["reason"] == "schema_missing"
        mig = cli(env, "migrate")
        assert mig.returncode == 0 and json.loads(mig.stdout)["applied"] == ["0001"]
        assert json.loads(cli(env, "migrate").stdout)["already_current"] is True
        assert cli(env, "check").returncode == 0

        boot = cli(env, "bootstrap-admin")
        secret = boot.stdout.strip()
        assert boot.returncode == 0 and secret.startswith("cce1.") and secret not in boot.stderr
        again = cli(env, "bootstrap-admin")
        assert (
            again.returncode != 0
            and "bootstrap_not_allowed" in again.stderr
            and "cce1." not in again.stdout + again.stderr
        )

        no_confirm = cli(env, "recover-admin")
        assert no_confirm.returncode != 0 and "cce1." not in no_confirm.stdout
        f = tmp_path / "recovered.key"
        rec = cli(env, "recover-admin", "--confirm", "--secret-file", str(f))
        assert rec.returncode == 0 and rec.stdout.strip() == ""  # nothing on stdout when a file is used
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600 and f.read_text().strip().startswith("cce1.")
        assert f.read_text().strip() not in rec.stderr
        clash = cli(env, "recover-admin", "--confirm", "--secret-file", str(f))  # O_EXCL: no overwrite
        assert clash.returncode == 2 and f.read_text().strip().startswith("cce1.")

    def test_failures_never_print_the_connection_url(self):  # AC-13, AC-50
        env = env_for("postgresql+asyncpg://admin:Hunter2Pw@127.0.0.1:1/nodb", CONNECT_TIMEOUT_S="1")
        for args in (("check",), ("migrate",), ("bootstrap-admin",)):
            r = cli(env, *args)
            assert (
                r.returncode != 0 and "Hunter2Pw" not in r.stdout + r.stderr and "127.0.0.1" not in r.stdout + r.stderr
            )

    def test_missing_configuration_is_exit_2(self):
        r = cli({k: v for k, v in os.environ.items() if not k.startswith(PREFIX)}, "check")
        assert r.returncode == 2


class TestParallelMigratorProcesses:
    async def test_parallel_migrate_processes_apply_the_sequence_once(self, db):  # AC-23 (separate OS processes)
        _, url = db
        env = env_for(url, LOCK_TIMEOUT_S="60")
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "cawbase_content_engine", "migrate"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd="/",
            )
            for _ in range(4)
        ]
        outs = [p.communicate(timeout=120) for p in procs]
        assert [p.returncode for p in procs] == [0, 0, 0, 0], outs
        applied = sorted(len(json.loads(o[0])["applied"]) for o in outs)
        assert applied == [0, 0, 0, 1], "exactly one process applies the sequence; the others find nothing pending"
        assert cli(env, "check").returncode == 0


class TestStandaloneService:
    async def _bootstrap(self, url: str) -> str:
        r = cli(env_for(url), "bootstrap-admin")
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    async def test_migrates_then_listens_and_serves_authenticated_requests(self, db, service_factory):  # AC-29
        _, url = db
        svc = service_factory(env_for(url))
        await svc.wait_ready()
        assert svc.logs().count('"migration_started"') == 1 and svc.logs().count('"migration_finished"') == 1
        secret = await self._bootstrap(url)
        r = httpx.get(f"http://127.0.0.1:{svc.port}/v1/auth/self", headers={"Authorization": f"Bearer {secret}"})
        assert r.status_code == 200 and len(r.json()["grants"]["admin"]) == 6
        assert httpx.get(f"http://127.0.0.1:{svc.port}/livez").json() == {"status": "alive"}
        assert httpx.get(f"http://127.0.0.1:{svc.port}/openapi.json").status_code == 404
        assert secret not in svc.logs() and "Authorization" not in svc.logs()

    async def test_nothing_listens_until_preparation_completes(self, db, service_factory):  # AC-29, AC-100
        backend, url = db
        # a cooperating migrator elsewhere holds the migration lock
        if backend == "postgresql":
            holder = create_async_engine(url)
            conn = await holder.connect()
            await conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
            release = lambda: asyncio.ensure_future(
                conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            )  # noqa: E731
        else:
            sq = sqlite3.connect(url.split("///", 1)[1], isolation_level=None)
            sq.execute("BEGIN IMMEDIATE")
            release = lambda: sq.execute("ROLLBACK")  # noqa: E731
        svc = service_factory(env_for(url, LOCK_TIMEOUT_S="60"))
        port_was_open = []
        for _ in range(25):  # ~5 s while the service waits for the lock
            port_was_open.append(port_open(svc.port))
            assert svc.proc.poll() is None
            await asyncio.sleep(0.2)
        assert not any(port_was_open), "the port must not accept connections during preparation"
        r = release()
        if asyncio.iscoroutine(r) or isinstance(r, asyncio.Future):
            await r
        await svc.wait_ready()
        if backend == "postgresql":
            await conn.close()
            await holder.dispose()
        else:
            sq.close()
        assert port_open(svc.port)

    async def test_migration_timeout_exits_nonzero_and_never_listens(self, db, service_factory):  # AC-100
        backend, url = db
        if backend == "postgresql":
            holder = create_async_engine(url)
            conn = await holder.connect()
            await conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
        else:
            sq = sqlite3.connect(url.split("///", 1)[1], isolation_level=None)
            sq.execute("BEGIN IMMEDIATE")
        svc = service_factory(env_for(url, LOCK_TIMEOUT_S="1"))
        rc = await svc.wait_exit()
        assert rc == 3 and not port_open(svc.port)
        logs = svc.logs()
        assert "startup_failed" in logs and "migration_lock_timeout" in logs
        assert "Traceback" not in logs
        if backend == "postgresql":
            await conn.close()
            await holder.dispose()
        else:
            sq.close()

    async def test_external_migration_mode_requires_a_compatible_schema(self, db, service_factory):  # AC-100
        _, url = db
        svc = service_factory(env_for(url, MIGRATE_ON_START="false"))
        assert await svc.wait_exit() == 4 and not port_open(svc.port)
        eng = create_async_engine(url)
        async with eng.connect() as c:  # it did NOT migrate behind our back
            names = await c.run_sync(lambda s: sa.inspect(s).get_table_names())
        await eng.dispose()
        assert not [n for n in names if "engine_state" in n]
        assert cli(env_for(url), "migrate").returncode == 0
        ok = service_factory(env_for(url, MIGRATE_ON_START="false"))
        await ok.wait_ready()
        assert "migration_started" not in ok.logs()

    async def test_incompatible_schema_exits_4(self, db, service_factory):  # AC-27, AC-100
        backend, url = db
        assert cli(env_for(url), "migrate").returncode == 0
        tbl = (
            "cawbase_content_engine.engine_state" if backend == "postgresql" else "cawbase_content_engine_engine_state"
        )
        eng = create_async_engine(url)
        async with eng.begin() as c:
            await c.execute(sa.text(f"UPDATE {tbl} SET min_runtime_level = 99"))
        await eng.dispose()
        svc = service_factory(env_for(url))
        assert await svc.wait_exit() == 4 and not port_open(svc.port)

    async def test_invalid_configuration_exits_2(self, db, service_factory):
        _, url = db
        for extra in ({"PORT": "99999"}, {"WORKERS": "0"}, {"MIGRATE_ON_START": "maybe"}, {"LOG_LEVEL": "LOUD"}):
            svc = service_factory(env_for(url, **extra))
            svc.proc.wait(30)
            assert svc.proc.returncode == 2, extra

    async def test_two_workers_migrate_once_and_workers_never_migrate(self, db, service_factory):  # AC-101
        _, url = db
        svc = service_factory(env_for(url), "--workers", "2")
        await svc.wait_ready()
        logs = svc.logs()
        assert logs.count('"migration_started"') == 1, logs
        for _ in range(8):  # both workers answer
            assert httpx.get(f"http://127.0.0.1:{svc.port}/readyz").status_code == 200

    async def test_separate_ddl_credentials_for_the_launcher_postgres(self, db, service_factory, tmp_path):  # AC-102
        backend, url = db
        if backend != "postgresql":
            pytest.skip("PostgreSQL only")
        admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
        db_name = sa.engine.make_url(url).database
        async with admin.connect() as c:
            await c.execute(sa.text("CREATE ROLE cce_svc_rt LOGIN PASSWORD 'rt' NOSUPERUSER"))
            await c.execute(sa.text(f'GRANT CONNECT ON DATABASE "{db_name}" TO cce_svc_rt'))
        runtime = (
            sa.engine.make_url(url).set(username="cce_svc_rt", password="rt").render_as_string(hide_password=False)
        )
        try:
            # with the runtime credentials alone the launcher cannot migrate and stays down...
            denied = service_factory(env_for(runtime))
            assert await denied.wait_exit() == 3 and not port_open(denied.port)
            assert "rt@" not in denied.logs()
            # ...with a separate DDL URL it prepares, then serves with the runtime role
            async with admin.connect() as c:
                await c.execute(sa.text("CREATE SCHEMA IF NOT EXISTS cawbase_content_engine"))
                await c.execute(sa.text("GRANT USAGE ON SCHEMA cawbase_content_engine TO cce_svc_rt"))
                await c.execute(
                    sa.text(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA cawbase_content_engine GRANT SELECT, INSERT, UPDATE ON TABLES TO cce_svc_rt"
                    )
                )
                await c.execute(
                    sa.text(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA cawbase_content_engine GRANT USAGE ON SEQUENCES TO cce_svc_rt"
                    )
                )
            ddl = env_for(runtime, MIGRATION_DATABASE_URL=url)
            svc = service_factory(ddl)
            await svc.wait_ready()
            assert "hunter" not in svc.logs() and url not in svc.logs()
        finally:
            async with admin.connect() as c:
                await c.execute(sa.text("DROP OWNED BY cce_svc_rt"))
                await c.execute(sa.text("DROP ROLE IF EXISTS cce_svc_rt"))
            await admin.dispose()
