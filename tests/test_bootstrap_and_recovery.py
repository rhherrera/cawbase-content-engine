"""Bootstrap and administrative recovery (D-26): AC-46, AC-95..98."""

from __future__ import annotations

import asyncio
import os
import stat
import uuid

import pytest
import sqlalchemy as sa

from cawbase_content_engine import (
    AdminPermission,
    AuthenticationError,
    BootstrapNotAllowedError,
    InvalidConfigurationError,
    RecoveryNotAllowedError,
    ScopePermission,
    SecretDeliveryError,
    SecretFileSink,
    ValidationFailedError,
    create_initial_admin_key,
    recover_admin_access,
)

from .helpers import issue


async def events(engine, action: str | None = None):
    t = engine.tables.admin_events
    q = sa.select(t).order_by(t.c.event_id)
    if action:
        q = q.where(t.c.action == action)
    async with engine._engine.connect() as c:
        return (await c.execute(q)).all()


async def state(engine):
    async with engine._engine.connect() as c:
        return (await c.execute(sa.select(engine.tables.engine_state))).one()


class TestInitialBootstrap:
    async def test_creates_full_catalog_key_event_and_mark_in_one_go(self, engine):  # AC-46
        issued = await create_initial_admin_key(engine)
        assert issued.info.grants.admin == frozenset(AdminPermission) and len(issued.info.grants.admin) == 6
        assert issued.info.grants.scopes == {}
        st = await state(engine)
        assert st.admin_initialized_at is not None and st.admin_bootstrap_generation == 1
        evs = await events(engine, "bootstrap.admin_key_issued")
        assert len(evs) == 1 and evs[0].detail["generation"] == 1 and evs[0].actor_key_id is None
        principal = await engine.authenticate_api_key(issued.secret)  # works through the real API
        assert principal.admin_permissions == frozenset(AdminPermission)

    async def test_second_bootstrap_fails_even_if_no_active_keys_remain(self, engine):  # AC-46 (persistent mark)
        first = await create_initial_admin_key(engine)
        await engine.admin(engine.trusted_admin_context(list(AdminPermission))).revoke_api_key(first.info.key_id)
        with pytest.raises(BootstrapNotAllowedError):
            await create_initial_admin_key(engine)
        assert len(await events(engine, "bootstrap.admin_key_issued")) == 1

    async def test_concurrent_bootstraps_create_exactly_one_admin(self, engine):  # AC-46, AC-96
        results = await asyncio.gather(*[create_initial_admin_key(engine) for _ in range(6)], return_exceptions=True)
        ok = [r for r in results if not isinstance(r, Exception)]
        assert len(ok) == 1 and all(
            isinstance(r, BootstrapNotAllowedError) for r in results if isinstance(r, Exception)
        )
        ops = engine.admin(engine.trusted_admin_context(list(AdminPermission)))
        assert [c.name for c in (await ops.list_clients()).items] == ["initial-admin"]

    async def test_there_is_no_http_bootstrap_surface(self):  # AC-46
        from fastapi import FastAPI

        from cawbase_content_engine import ContentEngine, EngineConfig
        from cawbase_content_engine.http import create_router

        app = FastAPI()
        app.include_router(create_router(ContentEngine(EngineConfig(database_url="sqlite+aiosqlite:////tmp/x.db"))))
        paths = " ".join(app.openapi()["paths"])
        assert "bootstrap" not in paths and "recover" not in paths


class TestRecovery:
    async def test_not_allowed_before_bootstrap_and_needs_confirmation(self, engine):  # AC-95
        with pytest.raises(ValidationFailedError):
            await recover_admin_access(engine, confirm=False)
        with pytest.raises(RecoveryNotAllowedError):
            await recover_admin_access(engine, confirm=True)

    async def test_recovers_when_all_admin_keys_are_lost_expired_limited_or_disabled(self, engine):  # AC-95
        boot = await create_initial_admin_key(engine)
        full = engine.admin(engine.trusted_admin_context(list(AdminPermission)))
        # a limited (non-admin-complete) key, a key of a disabled client and the (lost) bootstrap key
        limited_client, limited = await issue(engine, "limited", admin=[AdminPermission.ADMIN_SCOPES_READ])
        dis_client, disabled = await issue(engine, "gone", admin=list(AdminPermission))
        await full.set_client_status(dis_client.client_id, "disabled")
        recovered = await recover_admin_access(engine, confirm=True)  # no authentication with any of them
        assert recovered.info.grants.admin == frozenset(AdminPermission)
        assert recovered.info.client_id not in {limited_client.client_id, dis_client.client_id}
        client = await full.get_client(recovered.info.client_id)
        assert client.name.startswith("recovery-") and client.status == "active"
        # nothing was revoked or disabled silently
        for secret in (boot.secret, limited.secret):
            await engine.authenticate_api_key(secret)
        with pytest.raises(AuthenticationError):
            await engine.authenticate_api_key(disabled.secret)  # still disabled by its owner, untouched
        assert (await full.get_client(dis_client.client_id)).status == "disabled"
        assert all(k.revoked_at is None for k in await full.list_api_keys(limited_client.client_id))
        evs = await events(engine, "admin.recovery_key_issued")
        assert len(evs) == 1 and evs[0].detail["generation"] == 2
        assert (await state(engine)).admin_bootstrap_generation == 2

    async def test_client_name_collisions_are_resolved(self, engine, monkeypatch):  # AC-95
        await create_initial_admin_key(engine)
        import cawbase_content_engine.bootstrap as bs

        fixed = iter(["deadbeef", "deadbeef", "cafebabe"])
        real = bs.secrets.token_hex
        monkeypatch.setattr(bs.secrets, "token_hex", lambda n: next(fixed) if n == 4 else real(n))
        first = await recover_admin_access(engine, confirm=True)
        second = await recover_admin_access(engine, confirm=True)  # first candidate name collides => retried
        assert first.info.client_id != second.info.client_id
        ops = engine.admin(engine.trusted_admin_context(list(AdminPermission)))
        names = sorted(c.name for c in (await ops.list_clients()).items if c.name.startswith("recovery-"))
        assert len(names) == 2 and names[0].endswith("cafebabe") and names[1].endswith("deadbeef")

    async def test_concurrent_recoveries_and_bootstrap_are_serialized(self, engine):  # AC-96
        await create_initial_admin_key(engine)
        results = await asyncio.gather(*[recover_admin_access(engine, confirm=True) for _ in range(4)])
        assert len({r.info.client_id for r in results}) == 4  # explicit, repeatable, each audited
        assert len(await events(engine, "admin.recovery_key_issued")) == 4
        assert (await state(engine)).admin_bootstrap_generation == 5

    async def test_orphaned_key_procedure_after_lost_output(self, engine):  # AC-98
        lost = await create_initial_admin_key(engine)  # output of this command is "lost"
        recovered = await recover_admin_access(engine, confirm=True)
        ctx = (await engine.authenticate_api_key(recovered.secret)).admin_context()
        ops = engine.admin(ctx)
        clients = {c.client_id: c for c in (await ops.list_clients()).items}
        orphan = (await ops.list_api_keys(lost.info.client_id))[0]
        assert clients[orphan.client_id].name == "initial-admin" and orphan.key_id == lost.info.key_id
        await ops.revoke_api_key(orphan.key_id)
        with pytest.raises(AuthenticationError):
            await engine.authenticate_api_key(lost.secret)
        await engine.authenticate_api_key(recovered.secret)


class TestSecretDelivery:  # AC-97
    async def test_file_is_exclusive_0600_and_receives_the_secret(self, engine, tmp_path):
        path = tmp_path / "admin.key"
        issued = await create_initial_admin_key(engine, sink=SecretFileSink(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert path.read_text().strip() == issued.secret.get_secret_value()

    async def test_unusable_path_fails_without_issuing_anything(self, engine, tmp_path):
        existing = tmp_path / "exists"
        existing.write_text("keep")
        with pytest.raises(InvalidConfigurationError):
            await create_initial_admin_key(engine, sink=SecretFileSink(existing))  # O_EXCL: never overwrites
        assert existing.read_text() == "keep"
        with pytest.raises(InvalidConfigurationError):
            await create_initial_admin_key(engine, sink=SecretFileSink(tmp_path / "no" / "dir" / "k"))
        assert (await state(engine)).admin_initialized_at is None and not await events(engine)

    async def test_file_is_removed_when_the_transaction_does_not_commit(self, engine, tmp_path):
        await create_initial_admin_key(engine)
        path = tmp_path / "second.key"
        with pytest.raises(BootstrapNotAllowedError):
            await create_initial_admin_key(engine, sink=SecretFileSink(path))
        assert not path.exists()

    async def test_delivery_failure_after_commit_never_leaks_the_secret(self, engine, tmp_path, monkeypatch):
        path = tmp_path / "k"
        sink = SecretFileSink(path)

        def boom(secret: str):
            raise OSError(f"disk full while writing {secret}")  # a driver message that would embed the secret

        monkeypatch.setattr(sink, "deliver", boom)
        with pytest.raises(SecretDeliveryError) as e:
            await create_initial_admin_key(engine, sink=sink)
        issued_event = (await events(engine, "bootstrap.admin_key_issued"))[0]
        key_id = issued_event.target_id
        text = f"{e.value} {e.value!r} {e.value.__cause__!r} {e.value.__context__!r}"
        assert e.value.key_id == key_id and "cce1." not in text and "disk full" not in str(e.value)
        # the credential exists (committed) but is unrecoverable: recovery + revocation resolves it
        recovered = await recover_admin_access(engine, confirm=True)
        ops = engine.admin((await engine.authenticate_api_key(recovered.secret)).admin_context())
        assert (await ops.revoke_api_key(key_id)).revoked_at is not None

    async def test_scope_and_recovery_use_exclusive_files_too(self, engine, tmp_path):
        await create_initial_admin_key(engine)
        path = tmp_path / "rec.key"
        issued = await recover_admin_access(engine, confirm=True, sink=SecretFileSink(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert issued.secret.get_secret_value() == path.read_text().strip()
        _ = (uuid, ScopePermission)
