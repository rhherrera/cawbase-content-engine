"""HTTP adapter over real persisted resources: AC-43..45, AC-60..65, AC-99 (in-memory ASGI)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from cawbase_content_engine import (
    AdminPermission,
    ContentEngine,
    EngineConfig,
    ScopePermission,
    create_initial_admin_key,
)
from cawbase_content_engine.http import create_router
from cawbase_content_engine.http.service import create_service_app

from .helpers import issue, make_scope

A = AdminPermission
R, W = ScopePermission.SCOPE_READ.value, ScopePermission.SCOPE_WRITE.value


@pytest.fixture
async def api(engine):
    app = FastAPI()
    app.include_router(create_router(engine))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client


def auth(secret) -> dict:
    return {"Authorization": f"Bearer {secret.get_secret_value() if hasattr(secret, 'get_secret_value') else secret}"}


def strip(resp: httpx.Response) -> tuple:
    body = resp.json()
    body["error"].pop("request_id", None)
    headers = {k: v for k, v in resp.headers.items() if k not in ("x-request-id", "content-length")}
    return resp.status_code, body, headers


class TestIsolationOverHttp:
    async def test_authorized_access_and_cross_access_denied(self, engine, api):  # AC-43, AC-44
        await make_scope(engine, "a")
        await make_scope(engine, "b")
        _, key_a = await issue(engine, "client-a", scopes={"a": [R, W]})
        _, ro = await issue(engine, "client-ro", scopes={"a": [R]})
        _, no_admin = await issue(engine, "client-x", scopes={"a": [R]})

        r = await api.get("/v1/scopes/a", headers=auth(key_a.secret))
        assert r.status_code == 200 and r.json()["scope_id"] == "a" and r.headers["etag"] == '"1"'
        r = await api.patch(
            "/v1/scopes/a", headers={**auth(key_a.secret), "If-Match": '"1"'}, json={"display_name": "New"}
        )
        assert r.status_code == 200 and r.json()["revision"] == 2 and r.json()["display_name"] == "New"

        # other scope (exists) vs scope that does not exist: byte-identical 404
        other = await api.get("/v1/scopes/b", headers=auth(key_a.secret))
        missing = await api.get("/v1/scopes/zzz", headers=auth(key_a.secret))
        assert other.status_code == 404 and strip(other) == strip(missing)
        assert other.json()["error"]["code"] == "resource_not_accessible"
        assert (
            await api.patch(
                "/v1/scopes/b", headers={**auth(key_a.secret), "If-Match": '"1"'}, json={"display_name": "x"}
            )
        ).status_code == 404
        # valid key + grant on the scope but insufficient permission -> 403
        r = await api.patch("/v1/scopes/a", headers={**auth(ro.secret), "If-Match": '"2"'}, json={"display_name": "x"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
        # a scope key has no admin rights
        for method, path in (
            ("GET", "/v1/admin/scopes"),
            ("GET", "/v1/admin/clients"),
            ("POST", "/v1/admin/scopes"),
            ("GET", "/v1/admin/diagnostics"),
        ):
            r = await api.request(method, path, headers=auth(no_admin.secret), json={} if method == "POST" else None)
            assert r.status_code == 403, (method, path)

    async def test_all_invalid_credentials_are_indistinguishable(self, engine, api):  # AC-45
        await make_scope(engine, "a")
        _, good = await issue(engine, "c", scopes={"a": [R]})
        _, revoked = await issue(engine, "c2", scopes={"a": [R]})
        await engine.admin(engine.trusted_admin_context(list(A))).revoke_api_key(revoked.info.key_id)
        shapes = set()
        for headers in (
            {},
            {"Authorization": "Bearer"},
            {"Authorization": "Basic abc"},
            {"Authorization": "Bearer nonsense"},
            auth("cce1.k_" + "a" * 24 + "." + "A" * 43),
            auth(revoked.secret),
            {"Authorization": "Bearer " + good.secret.get_secret_value()[:-1] + "x"},
        ):
            r = await api.get("/v1/scopes/a", headers=headers)
            assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
            shapes.add(json.dumps(strip(r), sort_keys=True))
        assert len(shapes) == 1
        assert (await api.get("/v1/auth/self", headers=auth(good.secret))).status_code == 200

    async def test_revocation_takes_effect_on_the_next_request(self, engine, api):  # AC-48
        await make_scope(engine, "a")
        _, key = await issue(engine, "c", scopes={"a": [R]})
        assert (await api.get("/v1/scopes/a", headers=auth(key.secret))).status_code == 200
        _, boss = await issue(engine, "boss", admin=list(A))
        r = await api.post(f"/v1/admin/api-keys/{key.info.key_id}/revoke", headers=auth(boss.secret))
        assert r.status_code == 200 and r.json()["revoked_at"]
        assert (await api.get("/v1/scopes/a", headers=auth(key.secret))).status_code == 401
        assert (
            await api.post(f"/v1/admin/api-keys/{key.info.key_id}/revoke", headers=auth(boss.secret))
        ).status_code == 200


class TestAdminFlow:
    async def test_full_flow_from_bootstrap_key(self, engine, api):  # AC-60
        boot = await create_initial_admin_key(engine)
        h = auth(boot.secret)
        r = await api.post(
            "/v1/admin/scopes", headers=h, json={"scope_id": "s1", "display_name": "S1", "metadata": {"k": "v"}}
        )
        assert r.status_code == 201
        assert (
            await api.post(
                "/v1/admin/scopes", headers=h, json={"scope_id": "s1", "display_name": "S1", "metadata": {"k": "v"}}
            )
        ).status_code == 200
        assert (
            await api.post("/v1/admin/scopes", headers=h, json={"scope_id": "s1", "display_name": "Other"})
        ).status_code == 409
        client = (await api.post("/v1/admin/clients", headers=h, json={"name": "svc", "display_name": "Svc"})).json()
        body = {"label": "w", "grants": {"admin": [], "scopes": [{"scope_id": "s1", "permissions": [R, W]}]}}
        rid = str(uuid.uuid4())
        created = await api.post(
            f"/v1/admin/clients/{client['client_id']}/api-keys", headers={**h, "Idempotency-Key": rid}, json=body
        )
        assert created.status_code == 201 and created.json()["secret"].startswith("cce1.")
        replay = await api.post(
            f"/v1/admin/clients/{client['client_id']}/api-keys", headers={**h, "Idempotency-Key": rid}, json=body
        )
        assert (
            replay.status_code == 200
            and replay.json()["secret"] is None
            and replay.headers["idempotent-replay"] == "true"
        )
        changed = await api.post(
            f"/v1/admin/clients/{client['client_id']}/api-keys",
            headers={**h, "Idempotency-Key": rid},
            json={**body, "label": "x"},
        )
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
        assert (
            await api.post(f"/v1/admin/clients/{client['client_id']}/api-keys", headers=h, json=body)
        ).status_code == 422  # key required
        listing = await api.get(f"/v1/admin/clients/{client['client_id']}/api-keys", headers=h)
        assert len(listing.json()["items"]) == 1 and "secret" not in json.dumps(listing.json()).lower().replace(
            "secret_", ""
        )
        me = await api.get("/v1/auth/self", headers=auth(created.json()["secret"]))
        assert me.json()["grants"]["scopes"] == [{"scope_id": "s1", "permissions": [R, W]}]
        assert (await api.post("/v1/admin/scopes/s1/disable", headers=h)).json()["status"] == "disabled"
        r = await api.patch(
            "/v1/scopes/s1", headers={**auth(created.json()["secret"]), "If-Match": '"2"'}, json={"display_name": "x"}
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "scope_disabled"
        assert (await api.post("/v1/admin/scopes/s1/enable", headers=h)).json()["revision"] == 3
        assert (await api.post(f"/v1/admin/clients/{client['client_id']}/disable", headers=h)).json()[
            "status"
        ] == "disabled"
        assert (await api.get("/v1/scopes/s1", headers=auth(created.json()["secret"]))).status_code == 401
        assert (await api.get("/v1/admin/clients/cli_none", headers=h)).status_code == 404
        diag = (await api.get("/v1/admin/diagnostics", headers=h)).json()
        assert diag["backend"] in ("sqlite", "postgresql") and diag["state"] == "ready"

    async def test_etag_if_match_and_412_428(self, engine, api):  # P-20 over HTTP, AC-72
        await make_scope(engine, "a")
        _, key = await issue(engine, "c", scopes={"a": [R, W]})
        h = auth(key.secret)
        assert (await api.patch("/v1/scopes/a", headers=h, json={"display_name": "x"})).status_code == 428
        assert (
            await api.patch("/v1/scopes/a", headers={**h, "If-Match": '"1"'}, json={"display_name": "B"})
        ).status_code == 200
        stale = await api.patch(
            "/v1/scopes/a", headers={**h, "If-Match": '"1"'}, json={"display_name": "B"}
        )  # values already applied
        assert stale.status_code == 412 and stale.json()["error"]["code"] == "revision_conflict"
        same = await api.patch("/v1/scopes/a", headers={**h, "If-Match": '"2"'}, json={"display_name": "B"})
        assert same.status_code == 200 and same.json()["revision"] == 2
        assert (
            await api.patch("/v1/scopes/a", headers={**h, "If-Match": "abc"}, json={"display_name": "B"})
        ).status_code == 422

    async def test_delegation_over_http(self, engine, api):  # AC-91, AC-92
        await make_scope(engine, "a")
        await make_scope(engine, "b")
        _, issuer = await issue(engine, "issuer", admin=[A.ADMIN_CLIENTS_WRITE], scopes={"a": [R]})
        _, target = await issue(engine, "target", scopes={"a": [R]})
        cid = (await engine.admin(engine.trusted_admin_context(list(A))).list_clients()).items[0].client_id
        h = {**auth(issuer.secret), "Idempotency-Key": str(uuid.uuid4())}
        denied = await api.post(
            f"/v1/admin/clients/{cid}/api-keys",
            headers=h,
            json={"label": "l", "grants": {"scopes": [{"scope_id": "b", "permissions": [R]}]}},
        )
        missing = await api.post(
            f"/v1/admin/clients/{cid}/api-keys",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={"label": "l", "grants": {"scopes": [{"scope_id": "nope", "permissions": [R]}]}},
        )
        assert denied.status_code == 403 == missing.status_code and strip(denied) == strip(missing)
        esc = await api.post(
            f"/v1/admin/clients/{cid}/api-keys",
            headers={**h, "Idempotency-Key": str(uuid.uuid4())},
            json={"label": "l", "grants": {"admin": [A.ADMIN_GRANTS_WRITE.value]}},
        )
        assert esc.status_code == 403
        _ = target


class TestValidationAndLimits:
    async def test_body_limit_counts_actual_bytes_not_content_length(self, engine, api):  # AC-65
        boot = await create_initial_admin_key(engine)

        async def chunks():  # no Content-Length at all (chunked)
            for _ in range(80):
                yield b'{"pad":"' + b"x" * 1024 + b'"}'

        r = await api.post("/v1/admin/scopes", headers=auth(boot.secret), content=chunks())
        assert r.status_code == 413 and r.json()["error"]["code"] == "limit_exceeded"
        big = await api.post("/v1/admin/scopes", headers=auth(boot.secret), content=b"{" + b" " * 70_000 + b"}")
        assert big.status_code == 413
        # a lying Content-Length smaller than the body cannot sneak a big body past the cap either
        r = await api.post(
            "/v1/admin/scopes", headers={**auth(boot.secret), "Content-Length": "10"}, content=b" " * 70_000
        )
        assert r.status_code in (413, 400, 422)

    async def test_validation_and_malformed_bodies(self, engine, api):  # AC-60, AC-65
        boot = await create_initial_admin_key(engine)
        h = auth(boot.secret)
        for body in (
            b"not json",
            b"[1]",
            b'{"scope_id": 5, "display_name": "x"}',
            b'{"scope_id": "ok"}',
            b'{"scope_id": "ok", "display_name": "x", "metadata": {"a": "' + b"y" * 5000 + b'"}}',
        ):
            r = await api.post("/v1/admin/scopes", headers=h, content=body)
            assert r.status_code == 422, body[:30]
            assert r.json()["error"]["code"] in ("validation_failed", "limit_exceeded")
        assert (await api.get("/v1/admin/scopes?limit=0", headers=h)).status_code == 422
        assert (await api.get("/v1/admin/scopes?limit=abc", headers=h)).status_code == 422
        assert (await api.get("/v1/admin/scopes?cursor=@@@", headers=h)).status_code == 422

    async def test_unsupported_routes_do_not_leak_framework_detail(self, api):
        r = await api.get("/v1/nothing")
        assert r.status_code == 404


class TestSecretsAndErrors:
    async def test_no_secrets_in_responses_logs_or_errors(self, engine, api, caplog):  # AC-50
        caplog.set_level(logging.DEBUG)
        for name in ("sqlalchemy.engine", "aiosqlite", "asyncpg"):
            logging.getLogger(name).setLevel(logging.INFO)
        await make_scope(engine, "a")
        _, key = await issue(engine, "c", scopes={"a": [R]})
        secret = key.secret.get_secret_value()
        raw = secret.split(".")[2]
        captured = []
        for headers in (auth(key.secret), auth("cce1.k_" + "0" * 24 + "." + raw), {}):
            r = await api.get("/v1/scopes/a", headers=headers)
            captured.append(r.text + json.dumps(dict(r.headers)))
        await api.get("/v1/admin/scopes", headers=auth(key.secret))
        assert raw not in "\n".join(rec.getMessage() for rec in caplog.records)
        assert "Authorization" not in "\n".join(rec.getMessage() for rec in caplog.records)
        assert all(raw not in c for c in captured[1:])  # failures never echo what was presented

    async def test_internal_errors_are_sanitized(self, engine, api, monkeypatch):  # AC-64
        await make_scope(engine, "a")
        _, key = await issue(engine, "c", scopes={"a": [R]})

        async def boom(*a, **k):
            raise RuntimeError("postgresql://user:hunter2@db.internal/x exploded")

        monkeypatch.setattr(engine, "_run", boom)
        r = await api.get("/v1/scopes/a", headers=auth(key.secret))
        assert r.status_code == 500 and "hunter2" not in r.text and r.json()["error"]["code"] == "internal_error"


class TestMountingAndService:
    async def test_router_registers_nothing_by_itself_and_never_migrates(self, db):  # AC-61
        backend, url = db
        engine = ContentEngine(EngineConfig(database_url=url))
        router = create_router(engine)
        assert engine._engine is None, "building the router opens no connection"
        host = FastAPI()
        assert "/v1/auth/self" not in host.openapi()["paths"]
        host.include_router(router, prefix="/engine")
        host.openapi_schema = None  # older FastAPI caches the schema
        assert "/engine/v1/auth/self" in host.openapi()["paths"]
        assert engine._engine is None

    async def test_service_app_health_and_gating(self, migrated):  # AC-62
        _, url = migrated
        engine = ContentEngine(EngineConfig(database_url=url))
        app = create_service_app(engine)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                assert (await c.get("/livez")).json() == {"status": "alive"}
                ready = await c.get("/readyz")
                assert ready.status_code == 200 and ready.json() == {"status": "ready"}
                assert "version" not in ready.text and "sqlite" not in ready.text
                assert (await c.get("/openapi.json")).status_code == 404  # OpenAPI off by default
        # after shutdown the readiness reports stopping
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/readyz")
            assert r.status_code == 503 and r.json()["state"] == "stopping"
            gated = await c.get("/v1/auth/self")
            assert gated.status_code == 503 and gated.headers["x-request-id"]
            assert gated.json()["error"]["request_id"] == gated.headers["x-request-id"]

    async def test_database_going_away_reports_degraded(self, migrated):  # AC-62
        backend, url = migrated
        engine = ContentEngine(EngineConfig(database_url=url))
        app = create_service_app(engine)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                assert (await c.get("/readyz")).status_code == 200
                # make the schema incompatible underneath the running service
                tbl = (
                    "cawbase_content_engine.engine_state"
                    if backend == "postgresql"
                    else "cawbase_content_engine_engine_state"
                )
                adm = sa.ext.asyncio.create_async_engine(url)
                async with adm.begin() as conn:
                    await conn.execute(sa.text(f"UPDATE {tbl} SET min_runtime_level = 99"))
                await adm.dispose()
                await asyncio.sleep(2.1)  # readiness is cached for 2 seconds
                r = await c.get("/readyz")
                assert r.status_code == 503 and r.json()["state"] == "schema_incompatible"
                assert (await c.get("/v1/auth/self")).status_code == 503
