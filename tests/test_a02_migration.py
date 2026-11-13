"""A02 migration 0002 and compatibility: AC-A02-42, 43, 44, 46, 52."""

from __future__ import annotations

import enum
import shutil
import subprocess
import sys
import textwrap
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import AdminPermission, ContentEngine, EngineConfig
from cawbase_content_engine.constants import SCHEMA_LEVEL
from cawbase_content_engine.errors import MigrationError, SchemaIncompatibleError
from cawbase_content_engine.migrations import upgrade
from cawbase_content_engine.migrations.runner import DEFAULT_SCRIPT_LOCATION
from cawbase_content_engine.persistence.dialect import Backend
from cawbase_content_engine.persistence.tables import tables_for

from .a02_helpers import NO_CATALOG, ops_for
from .helpers import issue, make_scope, table_names
from .test_migrations import inventory, prepare_foreign

A02_TABLES = {"profiles", "profile_categories", "profile_sensitive_types", "profile_versions", "scope_profile_defaults"}


def chain(tmp, *, with_0002: bool, extra: str | None = None):
    """Revision directory with the A01 revision (and optionally the A02 one plus a test-only successor)."""
    root = tmp / f"chain-{uuid.uuid4().hex[:6]}"
    (root / "versions").mkdir(parents=True)
    shutil.copy(DEFAULT_SCRIPT_LOCATION / "versions" / "0001_initial.py", root / "versions")
    if with_0002:
        shutil.copy(DEFAULT_SCRIPT_LOCATION / "versions" / "0002_profiles.py", root / "versions")
    if extra:
        (root / "versions" / "0003.py").write_text(
            textwrap.dedent(f"""
            import sqlalchemy as sa
            from alembic import op
            revision = "0003"
            down_revision = "0002"
            def upgrade():
                {extra}
        """)
        )
    return root


async def dump_a01_tables(url: str, backend: str) -> dict:
    t = tables_for(Backend(backend))
    eng = create_async_engine(url)
    try:
        async with eng.connect() as c:
            out = {}
            for tbl in (
                t.scopes,
                t.clients,
                t.api_keys,
                t.api_key_admin_permissions,
                t.api_key_scope_grants,
                t.admin_events,
            ):
                rows = (await c.execute(sa.select(tbl).order_by(*tbl.primary_key.columns))).all()
                out[tbl.name] = [tuple(r) for r in rows]
            return out
    finally:
        await eng.dispose()


def tbl_scopes(backend: str) -> str:
    return "cawbase_content_engine.scopes" if backend == "postgresql" else "cawbase_content_engine_scopes"


class TestUpgradeFromA01:
    async def test_existing_data_is_untouched_and_no_profiles_are_created(
        self, db, tmp_path, monkeypatch
    ):  # AC-A02-42, 43
        backend, url = db
        await prepare_foreign(url)
        cfg = EngineConfig(database_url=url)
        r1 = await upgrade(cfg, script_location=chain(tmp_path, with_0002=False))  # an A01 installation
        assert r1.applied == ("0001",) and r1.to_level == 1
        monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 1)  # behave as an A01 runtime
        async with ContentEngine(
            EngineConfig(database_url=url)
        ) as e1:  # real A01 data, created through the public API (the A02 code path is not used)
            await make_scope(e1, "kept")
            _, key = await issue(e1, "svc", scopes={"kept": ["scope:read", "scope:write"]})
            # an A01 runtime's own write (the A05 code path needs the 0003 tables, which do not exist on this schema)
            async with e1._engine.begin() as c1:  # type: ignore[union-attr]
                await c1.execute(
                    sa.text(f"UPDATE {tbl_scopes(backend)} SET display_name = 'X', revision = revision + 1")
                )
        monkeypatch.undo()
        before_a01 = await dump_a01_tables(url, backend)
        before_objects = await inventory(url)
        # a runtime that requires level 2 refuses the level-1 schema until it is migrated
        assert SCHEMA_LEVEL == 3  # the current head; the level-1 schema is older
        with pytest.raises(SchemaIncompatibleError):
            await ContentEngine(cfg).start()
        # migrate 1 -> 2
        r2 = await upgrade(cfg)
        assert r2.applied == ("0002", "0003") and r2.from_revision == "0001" and r2.to_level == 3
        after_objects = await inventory(url)
        assert await dump_a01_tables(url, backend) == before_a01  # business data and history intact
        for name, entry in before_objects.items():  # nothing existing (A01 or foreign) altered or removed
            assert after_objects[name] == entry, name
        created = set(after_objects) - set(before_objects)
        assert {
            n.split(".")[-1].removeprefix("cawbase_content_engine_")
            for n in created
            if "ix_" not in n and "ux_" not in n and "_pkey" not in n and "_key" not in n and "_seq" not in n
        } >= A02_TABLES
        assert all("cawbase_content_engine" in n for n in created)  # only objects in the engine namespace
        again = await upgrade(cfg)
        assert again.already_current and again.applied == ()
        async with ContentEngine(cfg) as e2:
            assert (await e2.admin(await _admin(e2)).get_scope("kept")).display_name == "X"
            assert (await e2.authenticate_api_key(key.secret)).scope_grants["kept"]  # old key still works
            ops = await ops_for(e2, "kept", create_scope=False)
            assert (await ops.list_profiles("kept")).items == []  # no profile for an existing scope
            assert (await e2.schema_status()).min_runtime_level == 1 and (await e2.schema_status()).schema_level == 3

    async def test_from_empty_installs_both_revisions_in_one_transaction(self, db, tmp_path):  # AC-A02-42
        backend, url = db
        cfg = EngineConfig(database_url=url)
        failing = chain(tmp_path, with_0002=True, extra='op.execute("SELECT * FROM table_that_does_not_exist")')
        with pytest.raises(MigrationError):
            await upgrade(cfg, script_location=failing)
        names = await table_names(url)
        assert not [n for n in names if "cawbase_content_engine" in n], "0001 and 0002 roll back together"
        report = await upgrade(cfg)  # the real chain
        assert report.applied == ("0001", "0002", "0003") and report.to_level == 3 and report.from_revision is None

    async def test_a_failure_in_a_later_revision_rolls_back_0002_too(self, db, tmp_path):  # AC-A02-42
        backend, url = db
        cfg = EngineConfig(database_url=url)
        await upgrade(cfg, script_location=chain(tmp_path, with_0002=False))
        failing = chain(tmp_path, with_0002=True, extra='op.execute("SELECT * FROM table_that_does_not_exist")')
        with pytest.raises(MigrationError):
            await upgrade(cfg, script_location=failing)
        names = await table_names(url)
        assert not [n for n in names if n.endswith("profiles")], "0002's tables must not survive a later failure"
        assert (await upgrade(cfg)).applied == ("0002", "0003")


async def _admin(engine):
    return engine.trusted_admin_context(list(AdminPermission))


class TestForeignObjectsOnFreshInstall:
    async def test_fresh_install_keeps_foreign_objects(self, db):  # AC-A02-43
        _, url = db
        await prepare_foreign(url)
        before = await inventory(url)
        await upgrade(EngineConfig(database_url=url))
        after = await inventory(url)
        for name, entry in before.items():
            assert after[name] == entry


class TestSchemaMatchesMigration:
    async def test_runtime_table_definitions_match_what_the_migration_creates(self, migrated):  # AC-A02-42
        backend, url = migrated
        t = tables_for(Backend(backend))
        eng = create_async_engine(url)
        try:
            async with eng.connect() as c:
                schema = "cawbase_content_engine" if backend == "postgresql" else None

                def inspect(sync):
                    insp = sa.inspect(sync)
                    return {
                        tbl.name: (
                            {col["name"]: col["nullable"] for col in insp.get_columns(tbl.name, schema=schema)},
                            {ix["name"] for ix in insp.get_indexes(tbl.name, schema=schema)},
                        )
                        for tbl in (
                            t.profiles,
                            t.categories,
                            t.sensitive_types,
                            t.profile_versions,
                            t.scope_profile_defaults,
                        )
                    }

                found = await c.run_sync(inspect)
        finally:
            await eng.dispose()
        for tbl in (t.profiles, t.categories, t.sensitive_types, t.profile_versions, t.scope_profile_defaults):
            cols, indexes = found[tbl.name]
            assert cols == {c.name: c.nullable for c in tbl.columns}, tbl.name
            declared = {i.name for i in tbl.indexes}
            assert declared <= indexes, (tbl.name, declared - indexes)


class TestDatabaseConstraints:
    """The database itself rejects what the engine's own code never produces (defence in depth)."""

    async def _insert(self, engine, table, **values):
        async with engine._engine.begin() as c:
            await c.execute(sa.insert(table).values(**values))

    def _profile(self, **kw):
        base = dict(
            scope_id="a",
            profile_id="p",
            display_name="P",
            language="es",
            status="active",
            revision=1,
            draft_revision=1,
            ranking_mode="ranking",
            eligibility_threshold=0.0,
            tie_tolerance=0.0,
            ranking_revision=1,
        )
        from cawbase_content_engine.operations.common import now

        return {**base, "created_at": now(), "updated_at": now(), **kw}

    async def test_unique_and_check_constraints_reject_bad_rows(self, engine):  # AC-A02-46
        t = engine.tables
        from cawbase_content_engine.operations.common import now

        ops = await ops_for(engine, "a")
        await ops.create_profile("a", "p", "P", **NO_CATALOG)
        await ops.create_profile("a", "q", "Q", **NO_CATALOG)
        await ops.create_category("a", "p", "c1", "Uno", "d", priority=1)
        await ops.create_category("a", "p", "c2", "Dos", "d")
        ts = now()

        def cat(**kw):
            return dict(
                scope_id="a",
                profile_id="p",
                category_id="x",
                name="Tres",
                name_key="tres",
                definition="d",
                examples={},
                translations=None,
                priority=None,
                state="enabled",
                deleted_at=None,
                revision=1,
                created_at=ts,
                updated_at=ts,
                **kw,
            )

        bad_cats = [
            cat(**{}) | {"name_key": "uno"},  # duplicate name among non-deleted
            cat(**{}) | {"priority": 1},  # duplicate priority among enabled
            cat(**{}) | {"priority": 0},
            cat(**{}) | {"priority": 10001},
            cat(**{}) | {"state": "weird"},
            cat(**{}) | {"state": "deleted"},  # deleted needs deleted_at
            cat(**{}) | {"category_id": "c1"},  # primary key
            cat(**{})
            | {"profile_id": "ghost"},  # composite FK -> profiles (SQLite: needs foreign_keys=ON, set by the engine)
            cat(**{}) | {"revision": 0},
        ]
        for row in bad_cats:
            with pytest.raises(sa.exc.IntegrityError):
                await self._insert(engine, t.categories, **row)
        # the same name or priority is fine for a deleted / other-profile / disabled row
        await self._insert(engine, t.categories, **(cat() | {"name_key": "uno", "state": "deleted", "deleted_at": ts}))
        await self._insert(engine, t.categories, **(cat() | {"category_id": "y", "priority": 1, "state": "disabled"}))
        await self._insert(engine, t.categories, **(cat() | {"profile_id": "q", "name_key": "uno", "priority": 1}))
        for kw in (
            {"status": "weird"},
            {"language": "fr"},
            {"ranking_mode": "x"},
            {"eligibility_threshold": 1.5},
            {"tie_tolerance": -0.1},
            {"revision": 0},
            {"status": "deleted"},
            {"profile_id": "p"},
            {"catalog_id": "x"},
            {"scope_id": "ghost"},
        ):
            with pytest.raises(sa.exc.IntegrityError):
                await self._insert(engine, t.profiles, **self._profile(**kw))

        # versions: unique (profile, source_draft_revision) and unique version_id
        def ver(n, src, vid):
            return dict(
                scope_id="a",
                profile_id="p",
                version_number=n,
                version_id=vid,
                source_draft_revision=src,
                snapshot_schema_version=1,
                snapshot={},
                content_hash=b"0" * 32,
                snapshot_bytes=2,
                capabilities={},
                published_at=ts,
                published_by=None,
            )

        await self._insert(engine, t.profile_versions, **ver(1, 5, "pvr_a"))
        for row in (ver(2, 5, "pvr_b"), ver(2, 6, "pvr_a"), ver(1, 7, "pvr_c"), ver(0, 8, "pvr_d")):
            with pytest.raises(sa.exc.IntegrityError):
                await self._insert(engine, t.profile_versions, **row)
        # a single default row per scope (PK) and a composite FK to an existing profile
        await self._insert(engine, t.scope_profile_defaults, scope_id="a", profile_id="p", revision=1, updated_at=ts)
        for row in (
            dict(scope_id="a", profile_id="q", revision=1, updated_at=ts),
            dict(scope_id="b", profile_id="p", revision=1, updated_at=ts),
        ):
            with pytest.raises(sa.exc.IntegrityError):
                await self._insert(engine, t.scope_profile_defaults, **row)


class TestCompatibilityWithA01:
    """Compatibility with the A01 runtime is conditional (docs/persistence-and-migrations.md, 7.5).

    There is no A01 binary to run here, so its behaviour is reproduced with the *exact A01 permission enum*
    (the two members A01 knew) substituted in the loader that raises (`ScopePermission(row.permission)`).
    """

    class A01ScopePermission(enum.StrEnum):
        SCOPE_READ = "scope:read"
        SCOPE_WRITE = "scope:write"

    async def test_a01_loader_fails_on_profile_grants_even_for_revoked_keys(self, engine, monkeypatch):  # AC-A02-44
        await make_scope(engine, "a")
        client, new_key = await issue(engine, "new", scopes={"a": ["profiles:read"]})
        _, old_key = await issue(engine, "old", scopes={"a": ["scope:read"]})
        admin = engine.admin(await _admin(engine))
        from cawbase_content_engine.operations import api_keys as ak

        # the A02 runtime handles everything
        assert (await engine.authenticate_api_key(new_key.secret)).scope_grants["a"]
        monkeypatch.setattr(ak, "ScopePermission", self.A01ScopePermission)  # behave like the A01 loader
        assert (await engine.authenticate_api_key(old_key.secret)).scope_grants["a"]  # keys A01 knows keep working
        with pytest.raises(Exception) as e1:  # noqa: B017
            await engine.authenticate_api_key(new_key.secret)  # authentication of the new key fails
        with pytest.raises(Exception) as e2:  # noqa: B017
            await admin.list_api_keys(client.client_id)  # an administrative listing that loads those grants
        assert not isinstance(e1.value, AssertionError) and not isinstance(e2.value, AssertionError)
        monkeypatch.undo()
        # revoking does not delete the stored grants: the listing keeps failing for an A01 runtime
        await admin.revoke_api_key(new_key.info.key_id)
        monkeypatch.setattr(ak, "ScopePermission", self.A01ScopePermission)
        with pytest.raises(Exception):  # noqa: B017
            await admin.list_api_keys(client.client_id)
        monkeypatch.undo()
        async with engine._engine.connect() as c:  # type: ignore[union-attr]
            g = engine.tables.api_key_scope_grants
            stored = (
                (await c.execute(sa.select(g.c.permission).where(g.c.key_id == new_key.info.key_id))).scalars().all()
            )
        assert stored == ["profiles:read"]  # the grant is still stored after the revocation

    async def test_a_runtime_requiring_level_1_accepts_a_level_2_schema(self, migrated, monkeypatch):  # AC-A02-44
        _, url = migrated
        monkeypatch.setattr("cawbase_content_engine.schema.SCHEMA_LEVEL", 1)  # what an A01 runtime requires
        async with ContentEngine(EngineConfig(database_url=url)) as e:
            status = await e.schema_status()
            assert (
                status.compatible and status.schema_level == 3 and status.min_runtime_level == 1
            )  # A05 raised the level to 3
            assert (await e.readiness()).ready


class TestLightImport:
    def test_importing_the_package_does_not_load_the_catalog_or_heavy_modules(self):  # AC-A02-52
        code = (
            "import sys, cawbase_content_engine as c;"
            "print('cawbase_content_engine.catalog' in sys.modules);"
            "print(bool({'fastapi','alembic','asyncpg','aiosqlite'} & set(sys.modules)));"
            "c.starter_catalog();"
            "print('cawbase_content_engine.catalog' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
        ).stdout.split()
        assert out == ["False", "False", "True"]
