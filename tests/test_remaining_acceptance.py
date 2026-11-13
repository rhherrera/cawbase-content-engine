"""Criteria not covered elsewhere: AC-21, AC-24/86 (product chain), AC-30, AC-63..65, AC-73, AC-74."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import textwrap
import uuid
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

import cawbase_content_engine.errors as errors_module
from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    DatabaseUnavailableError,
    EngineConfig,
    HostTransaction,
    LimitExceededError,
    ScopePermission,
    ValidationFailedError,
    create_initial_admin_key,
)
from cawbase_content_engine.errors import MigrationError
from cawbase_content_engine.http import create_router
from cawbase_content_engine.http.service import create_service_app
from cawbase_content_engine.migrations import DEFAULT_SCRIPT_LOCATION, upgrade

from .helpers import admin_ctx, issue, make_host_engine, make_scope, table_names


def extended_chain(tmp: Path, *extra: tuple[str, str, str]) -> Path:
    """The product revisions plus test-only successors (never shipped)."""
    root = tmp / "chain"
    (root / "versions").mkdir(parents=True)
    shutil.copy(DEFAULT_SCRIPT_LOCATION / "versions" / "0001_initial.py", root / "versions")
    for rev, down, body in extra:
        (root / "versions" / f"{rev}.py").write_text(
            textwrap.dedent(f'''
            import sqlalchemy as sa
            from alembic import op
            from cawbase_content_engine.migrations.ns import namespace
            revision = "{rev}"
            down_revision = "{down}"
            def upgrade():
                ns = namespace(op.get_bind())
                {body}
        ''')
        )
    return root


ADD_TABLE = 'op.create_table(ns.table("extra_{n}"), sa.Column("id", sa.Integer, primary_key=True), schema=ns.schema)'
BUMP = 'op.execute(sa.text("UPDATE " + ns.qualified("engine_state") + " SET schema_level = {n}"))'
FAIL = 'op.execute("SELECT * FROM table_that_does_not_exist")'


class TestProductChainUpgrades:
    async def test_update_preserves_data_and_foreign_objects(self, db, tmp_path, monkeypatch):  # AC-21
        backend, url = db
        cfg = EngineConfig(database_url=url)
        eng = create_async_engine(url)
        async with eng.begin() as c:
            await c.execute(sa.text("CREATE TABLE app_things (id INTEGER PRIMARY KEY, v TEXT)"))
            await c.execute(sa.text("INSERT INTO app_things VALUES (1, 'keep')"))
        # baseline = the A01 chain (0001 only); the test-only "0002" below stands for "a later revision"
        await upgrade(cfg, script_location=extended_chain(tmp_path / "base"))
        monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 1)  # an A01-level runtime on a level-1 schema
        async with ContentEngine(cfg) as e:
            await make_scope(e, "keep-me")
        monkeypatch.undo()
        chain = extended_chain(
            tmp_path, ("0002", "0001", ADD_TABLE.format(n=2) + "\n                " + BUMP.format(n=2))
        )
        report = await upgrade(cfg, script_location=chain)
        assert report.applied == ("0002",) and report.to_level == 2
        monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 1)  # the runtime requirement of this scenario
        async with ContentEngine(cfg) as e:  # still compatible: newer schema, runtime requirement unchanged
            assert (await e.admin(await admin_ctx(e)).get_scope("keep-me")).scope_id == "keep-me"
        async with eng.connect() as c:
            assert (await c.execute(sa.text("SELECT v FROM app_things"))).scalar_one() == "keep"
        await eng.dispose()

    async def test_several_pending_revisions_with_intermediate_failure_roll_back_as_one(
        self, db, tmp_path
    ):  # AC-24, AC-86
        backend, url = db
        cfg = EngineConfig(database_url=url)
        chain = extended_chain(
            tmp_path,
            ("0002", "0001", ADD_TABLE.format(n=2) + "\n                " + BUMP.format(n=2)),
            ("0003", "0002", ADD_TABLE.format(n=3)),
            ("0004", "0003", FAIL),
        )
        with pytest.raises(MigrationError):
            await upgrade(cfg, script_location=chain)
        names = await table_names(url)
        assert not [n for n in names if "cawbase_content_engine" in n], "0001..0003 must not survive a failure in 0004"
        good = extended_chain(
            tmp_path / "ok",
            ("0002", "0001", ADD_TABLE.format(n=2) + "\n                " + BUMP.format(n=2)),
            ("0003", "0002", ADD_TABLE.format(n=3)),
        )
        report = await upgrade(cfg, script_location=good)
        assert report.applied == ("0001", "0002", "0003")  # all pending revisions in one go, from scratch

    async def test_no_intermediate_commit_is_observable(self, db, tmp_path):  # AC-86
        backend, url = db
        probe = tmp_path / "probe.json"
        if backend == "sqlite":
            # While 0002 runs, a *different* connection must not see 0001's tables.
            body = (
                "import json, sqlite3\n"
                f"                c = sqlite3.connect({url.split('///', 1)[1]!r}, timeout=1)\n"
                "                seen = [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE name LIKE 'cawbase_content_engine_%'\")]\n"
                "                c.close()\n"
                f"                open({str(probe)!r}, 'w').write(json.dumps(seen))"
            )
            expected: object = []
        else:
            # 0001 created engine_state's row in this very transaction: its xmin must equal our xid.
            body = (
                "import json\n"
                "                same = op.get_bind().execute(sa.text(\n"
                "                    'SELECT (xmin::text)::bigint = (txid_current() % 4294967296) FROM cawbase_content_engine.engine_state')).scalar_one()\n"
                f"                open({str(probe)!r}, 'w').write(json.dumps(bool(same)))"
            )
            expected = True
        chain = extended_chain(tmp_path, ("0002", "0001", body))
        await upgrade(EngineConfig(database_url=url), script_location=chain)
        assert json.loads(probe.read_text()) == expected, "revisions ran in different transactions"


class TestSqliteAndPostgresConcurrencyDiffer:
    async def test_sqlite_single_writer_surfaces_a_retryable_error(self, tmp_path):  # AC-30, AC-74
        url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
        cfg = EngineConfig(database_url=url, statement_timeout_ms=300)
        await upgrade(cfg)
        async with ContentEngine(cfg) as e:
            await make_scope(e, "seed")
            holder = sqlite3.connect(tmp_path / "c.db", isolation_level=None)
            holder.execute("BEGIN IMMEDIATE")  # e.g. a migrator or another writer in the shared file
            ops = e.admin(await admin_ctx(e))
            with pytest.raises(DatabaseUnavailableError) as err:
                await ops.create_scope("blocked", "Blocked")
            assert err.value.retryable is True
            assert (await ops.get_scope("seed")).scope_id == "seed", "WAL: readers continue during the write lock"
            holder.execute("ROLLBACK")
            holder.close()
            assert (await ops.create_scope("blocked", "Blocked")).created  # retry succeeds

    async def test_postgres_concurrent_writers_do_not_block_each_other(self, db):  # AC-74
        backend, url = db
        if backend != "postgresql":
            pytest.skip("PostgreSQL only")
        await upgrade(EngineConfig(database_url=url))
        host = await make_host_engine(backend, url)
        e = ContentEngine(EngineConfig(async_engine=host))
        await e.start()
        ctx = await admin_ctx(e)
        async with host.begin() as conn:
            await e.admin(ctx, transaction=HostTransaction(conn)).create_scope("held", "uncommitted write")
            created = await asyncio.wait_for(e.admin(ctx).create_scope("other", "Other"), 5)  # MVCC: no blocking
            assert created.created
        await e.aclose()
        await host.dispose()


class TestDiagnosticsLimitsErrors:
    async def test_public_and_authenticated_diagnostics_leak_no_infrastructure(self, db):  # AC-63, AC-13
        backend, url = db
        await upgrade(EngineConfig(database_url=url))
        engine = ContentEngine(EngineConfig(database_url=url))
        await engine.start()
        boot = await create_initial_admin_key(engine)
        app = create_service_app(engine)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                bodies = [
                    (await c.get("/livez")).text,
                    (await c.get("/readyz")).text,
                    (
                        await c.get(
                            "/v1/admin/diagnostics",
                            headers={"Authorization": f"Bearer {boot.secret.get_secret_value()}"},
                        )
                    ).text,
                ]
        parsed = sa.engine.make_url(url)
        needles = [
            url,
            parsed.database or "",
            str(parsed.host or ""),
            str(parsed.username or ""),
            str(parsed.password or ""),
            str(Path(parsed.database or "").parent) if backend == "sqlite" else "",
            "/Users/",
            "/private/",
            "postgresql+asyncpg",
        ]
        for body in bodies:
            for needle in filter(None, needles):
                assert needle not in body, (needle, body)
        diag = json.loads(bodies[2])
        assert set(diag) == {
            "package_version",
            "backend",
            "schema_level",
            "min_runtime_level",
            "required_schema_level",
            "runtime_level",
            "migration_mode",
            "state",
        }
        await engine.aclose()

    async def test_every_error_code_has_the_documented_http_status(self):  # AC-64
        documented = {
            "authentication_failed": 401,
            "permission_denied": 403,
            "resource_not_accessible": 404,
            "resource_not_found": 404,
            "conflict": 409,
            "idempotency_conflict": 409,
            "scope_disabled": 409,
            "revision_conflict": 412,
            "precondition_required": 428,
            "validation_failed": 422,
            "limit_exceeded": 422,
            "engine_not_ready": 503,
            "database_unavailable": 503,
            "schema_missing": 503,
            "schema_incompatible": 503,
            "internal_error": 500,
            # A02
            "profile_disabled": 409,
            "profile_not_selectable": 409,
            "profile_not_published": 409,
            "profile_is_default": 409,
            "default_profile_not_set": 404,
            "invalid_state_transition": 409,
            "name_conflict": 409,
            "priority_conflict": 409,
            "profile_invalid": 422,
            # A03
            "capabilities_not_configured": 409,
            "inference_not_configured": 503,
            "inference_unavailable": 503,
            "inference_timeout": 504,
            "inference_output_invalid": 502,
            "inference_fixture_not_found": 501,
            # A05
            "storage_not_configured": 503,
            "documents_not_configured": 503,
            "storage_unavailable": 503,
            "stored_object_unavailable": 503,
            "upload_in_progress": 409,
            "upload_aborted": 400,
            "content_mismatch": 422,
            "file_not_usable": 409,
            "file_expiring": 409,
            "quota_exceeded": 429,
            "job_not_succeeded": 409,
            "job_not_terminal": 409,
            "job_data_expired": 410,
        }
        no_http = {
            "configuration_invalid",
            "migration_failed",
            "migration_lock_timeout",
            "migration_permission_denied",
            "transaction_state_invalid",
            "transaction_broken",
            "transaction_incompatible",
            "bootstrap_not_allowed",
            "recovery_not_allowed",
            "secret_delivery_failed",
            "isolation_unavailable",
        }
        classes = [
            c
            for c in vars(errors_module).values()
            if isinstance(c, type) and issubclass(c, errors_module.EngineError) and c is not errors_module.EngineError
        ]
        by_code = {}
        for cls in classes:
            by_code.setdefault(cls.code, set()).add(cls.http_status)
        assert {c for c, s in by_code.items() if s != {None}} - no_http == set(documented)
        for code, status in documented.items():
            expected = {413, 422} if code == "limit_exceeded" else {status}  # body size => 413, field limits => 422
            assert by_code[code] == expected, code
        assert all(by_code[c] == {None} for c in no_http)
        # PayloadTooLarge shares limit_exceeded but is the 413 body-size case
        assert errors_module.PayloadTooLargeError.http_status == 413

    async def test_operational_limits(self, engine):  # AC-65
        ops = engine.admin(await admin_ctx(engine))
        lim = engine.config.limits
        await make_scope(engine, "a")
        client = (await ops.create_client("svc", "Svc")).value
        with pytest.raises(LimitExceededError):
            await ops.create_scope("big", "x" * (lim.max_display_name + 1))
        with pytest.raises(ValidationFailedError):
            await ops.list_scopes(limit=lim.max_page_size + 1)
        # grants per key
        too_many = {
            f"s{i}": [ScopePermission.SCOPE_READ, ScopePermission.SCOPE_WRITE]
            for i in range(lim.max_scope_grants_per_key)
        }
        with pytest.raises(LimitExceededError):
            await ops.issue_api_key(
                client.client_id, request_id=str(uuid.uuid4()), label="l", grants={"scopes": too_many}
            )

    async def test_active_keys_per_client_limit(self, migrated):  # AC-65
        from cawbase_content_engine import Limits

        _, url = migrated
        e = ContentEngine(EngineConfig(database_url=url, limits=Limits(max_active_keys_per_client=2)))
        await e.start()
        ops = e.admin(await admin_ctx(e))
        client = (await ops.create_client("svc", "Svc")).value
        issued = []
        for _ in range(2):
            issued.append(
                await ops.issue_api_key(
                    client.client_id,
                    request_id=str(uuid.uuid4()),
                    label="l",
                    grants={"admin": [AdminPermission.ADMIN_SCOPES_READ]},
                )
            )
        with pytest.raises(LimitExceededError):
            await ops.issue_api_key(
                client.client_id,
                request_id=str(uuid.uuid4()),
                label="l",
                grants={"admin": [AdminPermission.ADMIN_SCOPES_READ]},
            )
        await ops.revoke_api_key(issued[0].info.key_id)  # revoked keys stop counting
        await ops.issue_api_key(
            client.client_id,
            request_id=str(uuid.uuid4()),
            label="l",
            grants={"admin": [AdminPermission.ADMIN_SCOPES_READ]},
        )
        await e.aclose()

    async def test_repeated_state_changes_and_revocations_write_no_new_events(self, engine):  # AC-73
        ops = engine.admin(await admin_ctx(engine))
        await make_scope(engine, "a")
        client, issued = await issue(engine, "svc", scopes={"a": [ScopePermission.SCOPE_READ]})

        async def count() -> int:
            async with engine._engine.connect() as c:
                return (
                    await c.execute(sa.select(sa.func.count()).select_from(engine.tables.admin_events))
                ).scalar_one()

        for call in (
            lambda: ops.set_scope_status("a", "disabled"),
            lambda: ops.set_client_status(client.client_id, "disabled"),
            lambda: ops.revoke_api_key(issued.info.key_id),
        ):
            await call()
            before = await count()
            await call()
            await call()
            assert await count() == before

    async def test_readiness_reports_degraded_when_the_database_stops_answering(self, migrated, monkeypatch):  # AC-62
        _, url = migrated
        engine = ContentEngine(EngineConfig(database_url=url))
        await engine.start()
        assert (await engine.readiness()).ready
        monkeypatch.setattr(ContentEngine, "schema_status", _raise_unavailable)
        report = await engine.readiness()
        assert (report.ready, report.state) == (False, "degraded_database")
        app = FastAPI()
        app.include_router(create_router(engine))
        await engine.aclose()


async def _raise_unavailable(self):
    raise DatabaseUnavailableError()
