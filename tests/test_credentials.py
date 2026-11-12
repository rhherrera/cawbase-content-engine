"""API keys, delegation, idempotency, revocation: AC-45..49, AC-71, AC-90..94, AC-99."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid

import pytest
import sqlalchemy as sa

from cawbase_content_engine import (
    AdminPermission,
    ApiKeyGrants,
    AuthenticationError,
    ConflictError,
    IdempotencyConflictError,
    PermissionDeniedError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    ScopePermission,
    ValidationFailedError,
)
from cawbase_content_engine import auth as authmod

from .helpers import admin_ctx, issue, make_scope

R, W = ScopePermission.SCOPE_READ, ScopePermission.SCOPE_WRITE
A = AdminPermission


class TestKeyFormat:  # AC-99 (D-25)
    def test_canonical_vectors(self):
        raw = bytes(range(32))
        text = authmod.encode_secret(raw)
        assert text == base64.urlsafe_b64encode(raw).decode().rstrip("=") and len(text) == 43 and "=" not in text
        assert authmod.hash_secret(raw) == hashlib.sha256(raw).digest()  # raw bytes, not the text
        assert authmod.hash_secret(raw) != hashlib.sha256(text.encode()).digest()
        key = f"cce1.k_{'0' * 24}.{text}"
        parsed = authmod.parse_key(key)
        assert parsed and parsed.secret_hash == hashlib.sha256(raw).digest() and parsed.key_id == f"k_{'0' * 24}"

    def test_generated_keys_are_random_32_bytes(self):
        keys = {authmod.generate_key().full_key for _ in range(50)}
        assert len(keys) == 50
        k = authmod.generate_key()
        _, key_id, secret = k.full_key.split(".")
        assert authmod.decode_secret(secret) is not None and len(authmod.decode_secret(secret)) == 32
        assert authmod.parse_key(k.full_key).secret_hash == k.secret_hash

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda k: k.replace("cce1", "cce2"),
            lambda k: k + "x",
            lambda k: k[:-1],
            lambda k: "." + k,
            lambda k: k.replace(".k_", ".K_"),
            lambda k: k + "=",
            lambda k: k + ".extra",
            lambda k: "",
        ],
    )
    def test_malformed_keys_rejected(self, mutate):
        assert authmod.parse_key(mutate(authmod.generate_key().full_key)) is None

    def test_non_canonical_base64_variants_are_rejected(self):
        # The last base64 character of 32 bytes carries 2 unused bits; other characters decode to
        # the same bytes but are not the canonical text.
        raw = bytes(32)
        canonical = authmod.encode_secret(raw)
        assert canonical.endswith("A")
        alias = canonical[:-1] + "B"
        assert base64.urlsafe_b64decode(alias + "=") == raw  # same bytes...
        assert authmod.decode_secret(alias) is None  # ...but rejected
        assert authmod.decode_secret(canonical) == raw


class TestIssueAuthenticateRevoke:
    async def test_secret_presented_once_and_never_stored(self, engine):  # AC-47
        client, issued = await issue(engine, "svc", scopes={}, admin=[A.ADMIN_DIAGNOSTICS_READ])
        secret = issued.secret.get_secret_value()
        raw_secret_text = secret.split(".")[2]
        assert "cce1." in secret and raw_secret_text not in repr(issued) and secret not in repr(issued)
        # nothing recoverable in ANY table
        dump = []
        async with engine._engine.connect() as c:
            for t in engine.tables.metadata.sorted_tables:
                for row in (await c.execute(sa.select(t))).all():
                    dump.append(repr([bytes(x).hex() if isinstance(x, (bytes, memoryview)) else str(x) for x in row]))
        blob = "\n".join(dump)
        raw = authmod.decode_secret(raw_secret_text)
        assert raw_secret_text not in blob and raw.hex() not in blob
        assert authmod.hash_secret(raw).hex() in blob  # only the hash is stored
        # listings: no secret, no hash
        ops = engine.admin(await admin_ctx(engine))
        listing = await ops.list_api_keys(client.client_id)
        text = repr(listing)
        assert (
            raw_secret_text not in text and authmod.hash_secret(raw).hex() not in text and "secret" not in text.lower()
        )

    async def test_authenticate_returns_exact_grants(self, engine):
        await make_scope(engine, "a")
        _, issued = await issue(engine, "svc", scopes={"a": [R]}, admin=[A.ADMIN_SCOPES_READ])
        p = await engine.authenticate_api_key(issued.secret)
        assert p.admin_permissions == {A.ADMIN_SCOPES_READ} and dict(p.scope_grants) == {"a": frozenset({R})}
        assert p.scope_context("a").permissions == {R}
        with pytest.raises(ResourceNotAccessibleError):
            p.scope_context("b")  # no grant at all

    async def test_every_invalid_credential_gives_the_same_error(self, engine):  # AC-45
        await make_scope(engine, "a")
        client, issued = await issue(engine, "svc", scopes={"a": [R]})
        good = issued.secret.get_secret_value()
        key_id = good.split(".")[1]
        other_secret = authmod.generate_key().full_key.split(".")[2]
        ops = engine.admin(await admin_ctx(engine))
        revoked_client, revoked = await issue(engine, "svc2", scopes={"a": [R]})
        await ops.revoke_api_key(revoked.info.key_id)
        disabled_client, disabled = await issue(engine, "svc3", scopes={"a": [R]})
        await ops.set_client_status(disabled_client.client_id, "disabled")
        _, expired = await issue(engine, "svc4", scopes={"a": [R]})
        async with engine._engine.begin() as c:
            await c.execute(
                sa.update(engine.tables.api_keys)
                .where(engine.tables.api_keys.c.key_id == expired.info.key_id)
                .values(
                    expires_at=sa.func.now() - sa.text("INTERVAL '1 day'")
                    if False
                    else __import__("datetime").datetime(2020, 1, 1, tzinfo=__import__("datetime").UTC)
                )
            )
        bad = [
            "",
            "garbage",
            good.replace(key_id, "k_" + "f" * 24),
            f"cce1.{key_id}.{other_secret}",
            revoked.secret.get_secret_value(),
            disabled.secret.get_secret_value(),
            expired.secret.get_secret_value(),
        ]
        for value in bad:
            with pytest.raises(AuthenticationError) as e:
                await engine.authenticate_api_key(value)
            assert e.value.message == "Authentication failed." and e.value.code == "authentication_failed"
        assert (await engine.authenticate_api_key(good)).key_id == key_id

    async def test_unknown_key_still_goes_through_constant_time_comparison(self, monkeypatch):  # AC-45 (unit)
        calls = []
        real = authmod.hmac.compare_digest
        monkeypatch.setattr(authmod.hmac, "compare_digest", lambda a, b: calls.append((len(a), len(b))) or real(a, b))
        assert authmod.verify(None, bytes(32)) is False  # unknown key_id compares against a dummy
        assert authmod.verify(b"x" * 32, b"y" * 32) is False
        assert calls == [(32, 32), (32, 32)]

    async def test_revocation_applies_to_subsequent_authentications_only(self, engine):  # AC-48, AC-99
        await make_scope(engine, "a")
        _, issued = await issue(engine, "svc", scopes={"a": [R]})
        principal = await engine.authenticate_api_key(issued.secret)  # already authorized
        ops = engine.admin(await admin_ctx(engine))
        info = await ops.revoke_api_key(issued.info.key_id)
        assert info.revoked_at is not None
        again = await ops.revoke_api_key(issued.info.key_id)  # repeatable, first timestamp kept
        assert again.revoked_at == info.revoked_at
        # the previously authorized principal/context is not retroactively cancelled
        assert (await engine.scoped(principal.scope_context("a")).get_scope("a")).scope_id == "a"
        with pytest.raises(AuthenticationError):
            await engine.authenticate_api_key(issued.secret)

    async def test_disabling_a_client_invalidates_its_keys(self, engine):  # AC-48
        await make_scope(engine, "a")
        client, issued = await issue(engine, "svc", scopes={"a": [R]})
        ops = engine.admin(await admin_ctx(engine))
        await ops.set_client_status(client.client_id, "disabled")
        with pytest.raises(AuthenticationError):
            await engine.authenticate_api_key(issued.secret)
        await ops.set_client_status(client.client_id, "active")
        await engine.authenticate_api_key(issued.secret)
        with pytest.raises(ConflictError):  # no new keys for a disabled client
            await ops.set_client_status(client.client_id, "disabled")
            await ops.issue_api_key(
                client.client_id, request_id=str(uuid.uuid4()), label="x", grants={"scopes": {"a": [R]}}
            )

    async def test_client_creation_idempotent_and_conflicting(self, engine):  # AC-70
        ops = engine.admin(await admin_ctx(engine))
        one = await ops.create_client("svc", "Service")
        two = await ops.create_client("svc", "Service")
        assert one.created and not two.created and one.value == two.value
        with pytest.raises(ConflictError):
            await ops.create_client("svc", "Different")
        with pytest.raises(ValidationFailedError):
            await ops.create_client("Bad Name", "x")


class TestIdempotentIssuance:
    async def test_replay_returns_no_secret_and_creates_nothing(self, engine):  # AC-71
        await make_scope(engine, "a")
        ops = engine.admin(await admin_ctx(engine))
        client = (await ops.create_client("svc", "Svc")).value
        rid = str(uuid.uuid4())
        grants = {"scopes": {"a": [R]}}
        first = await ops.issue_api_key(client.client_id, request_id=rid, label="l", grants=grants)
        replay = await ops.issue_api_key(client.client_id, request_id=rid, label="l", grants=grants)
        assert first.secret is not None and not first.replayed
        assert replay.secret is None and replay.replayed and replay.info.key_id == first.info.key_id
        assert len(await ops.list_api_keys(client.client_id)) == 1
        with pytest.raises(IdempotencyConflictError):  # same request id, different parameters
            await ops.issue_api_key(client.client_id, request_id=rid, label="other", grants=grants)

    async def test_concurrent_identical_requests_issue_exactly_one_key(self, engine):  # AC-71
        await make_scope(engine, "a")
        ops = engine.admin(await admin_ctx(engine))
        client = (await ops.create_client("svc", "Svc")).value
        rid = str(uuid.uuid4())
        results = await asyncio.gather(
            *[
                ops.issue_api_key(client.client_id, request_id=rid, label="l", grants={"scopes": {"a": [R]}})
                for _ in range(6)
            ]
        )
        assert sum(1 for r in results if r.secret is not None) == 1
        assert len({r.info.key_id for r in results}) == 1
        assert len(await ops.list_api_keys(client.client_id)) == 1

    async def test_validation(self, engine):
        ops = engine.admin(await admin_ctx(engine))
        client = (await ops.create_client("svc", "Svc")).value
        for grants in ({}, {"admin": ["admin:nope"]}, {"scopes": {"a": ["scope:nope"]}}, {"scopes": {"bad id": [R]}}):
            with pytest.raises(ValidationFailedError):
                await ops.issue_api_key(client.client_id, request_id=str(uuid.uuid4()), label="l", grants=grants)
        with pytest.raises(ValidationFailedError):
            await ops.issue_api_key(
                client.client_id, request_id="not-a-uuid", label="l", grants={"admin": [A.ADMIN_SCOPES_READ]}
            )
        with pytest.raises(ResourceNotFoundError):
            await ops.issue_api_key(
                "cli_missing", request_id=str(uuid.uuid4()), label="l", grants={"admin": [A.ADMIN_SCOPES_READ]}
            )


class TestDelegation:
    """D-27: subset rule; ``admin:grants:write`` allows granting any *existing* scope; admin by subset."""

    async def issuer(self, engine, *, admin, scopes):
        client = (await engine.admin(await admin_ctx(engine)).create_client("issuer", "Issuer")).value
        return client, engine.trusted_admin_context(admin, scope_grants=scopes)

    async def test_delegation_within_own_grants_is_allowed(self, engine):  # AC-90
        await make_scope(engine, "a")
        _, ctx = await self.issuer(engine, admin=[A.ADMIN_CLIENTS_WRITE], scopes={"a": [R, W]})
        ops = engine.admin(ctx)
        target = (await engine.admin(await admin_ctx(engine)).create_client("target", "T")).value
        issued = await ops.issue_api_key(
            target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"scopes": {"a": [R]}}
        )
        assert issued.info.grants.scopes == {"a": frozenset({R})}

    async def test_scope_not_held_is_denied_without_grants_write(self, engine):  # AC-92
        await make_scope(engine, "a")
        await make_scope(engine, "b")
        _, ctx = await self.issuer(engine, admin=[A.ADMIN_CLIENTS_WRITE], scopes={"a": [R]})
        target = (await engine.admin(await admin_ctx(engine)).create_client("target", "T")).value
        ops = engine.admin(ctx)
        for scopes in ({"b": [R]}, {"a": [W]}, {"a": [R], "b": [R]}):  # other scope, escalated permission
            with pytest.raises(PermissionDeniedError):
                await ops.issue_api_key(
                    target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"scopes": scopes}
                )
        with pytest.raises(PermissionDeniedError) as nonexistent:  # indistinguishable from an existing scope
            await ops.issue_api_key(
                target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"scopes": {"nope": [R]}}
            )
        assert nonexistent.value.message == "Permission denied."

    async def test_grants_write_allows_any_existing_scope_only(self, engine):  # AC-90, AC-93
        await make_scope(engine, "b")
        _, ctx = await self.issuer(engine, admin=[A.ADMIN_CLIENTS_WRITE, A.ADMIN_GRANTS_WRITE], scopes={})
        target = (await engine.admin(await admin_ctx(engine)).create_client("target", "T")).value
        ops = engine.admin(ctx)
        ok = await ops.issue_api_key(
            target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"scopes": {"b": [R, W]}}
        )
        assert ok.info.grants.scopes == {"b": frozenset({R, W})}
        with pytest.raises(ValidationFailedError):  # nonexistent scope
            await ops.issue_api_key(
                target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"scopes": {"nope": [R]}}
            )

    async def test_administrative_escalation_is_denied(self, engine):  # AC-91
        _, ctx = await self.issuer(engine, admin=[A.ADMIN_CLIENTS_WRITE], scopes={})
        target = (await engine.admin(await admin_ctx(engine)).create_client("target", "T")).value
        ops = engine.admin(ctx)
        for perm in (A.ADMIN_DIAGNOSTICS_READ, A.ADMIN_GRANTS_WRITE, A.ADMIN_SCOPES_WRITE):
            with pytest.raises(PermissionDeniedError):
                await ops.issue_api_key(
                    target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"admin": [perm]}
                )
        # even WITH admin:grants:write, an admin permission not held cannot be conceded, nor can grants:write be delegated
        _, big = await self.issuer(engine, admin=[A.ADMIN_CLIENTS_WRITE, A.ADMIN_GRANTS_WRITE], scopes={})
        for perm in (A.ADMIN_DIAGNOSTICS_READ, A.ADMIN_SCOPES_WRITE):
            with pytest.raises(PermissionDeniedError):
                await engine.admin(big).issue_api_key(
                    target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"admin": [perm]}
                )
        ok = await engine.admin(big).issue_api_key(
            target.client_id, request_id=str(uuid.uuid4()), label="l", grants={"admin": [A.ADMIN_GRANTS_WRITE]}
        )  # held => delegable
        assert ok.info.grants.admin == {A.ADMIN_GRANTS_WRITE}

    async def test_issuing_requires_clients_write(self, engine):  # AC-90
        _, ctx = await self.issuer(engine, admin=[A.ADMIN_GRANTS_WRITE, A.ADMIN_CLIENTS_READ], scopes={})
        with pytest.raises(PermissionDeniedError):
            await engine.admin(ctx).issue_api_key(
                "cli_x", request_id=str(uuid.uuid4()), label="l", grants={"admin": [A.ADMIN_GRANTS_WRITE]}
            )

    def test_catalog_has_eleven_permissions_and_no_wildcards(self):  # AC-94, AC-A02-05
        # 8 from A01 (scope:read|write + six admin:*), 2 from A02 (profiles:read|write), 1 from A03 (analysis:execute)
        assert len(ScopePermission) + len(AdminPermission) == 11
        assert {p.value for p in ScopePermission} == {
            "scope:read",
            "scope:write",
            "profiles:read",
            "profiles:write",
            "analysis:execute",
        }
        assert not [p for p in [*ScopePermission, *AdminPermission] if "*" in p.value]
        with pytest.raises(ValueError):
            AdminPermission("admin:*")

    async def test_grants_are_immutable_per_key(self, engine):
        assert not hasattr(engine.admin(await admin_ctx(engine)), "update_api_key_grants")
        assert isinstance(ApiKeyGrants().admin, frozenset)
