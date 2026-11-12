"""A03 over HTTP (in-memory ASGI), same operations as Python (AC-A03-01..06, 41..45, 55, 56)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from cawbase_content_engine import AdminPermission
from cawbase_content_engine.http import create_router

from .a03_helpers import (
    TEXT,
    CustomAdapter,
    analysis_ops,
    build_profile,
    fixture_for,
    inference_engine,
    mock,
    ok_output,
    row_counts,
)
from .helpers import admin_ctx, issue, make_scope

URL = "/v1/scopes/a/analyze-text"


def h(secret) -> dict:
    return {"Authorization": f"Bearer {secret.get_secret_value()}"}


@asynccontextmanager
async def serve(base, adapter, *, timeout_s=5.0):
    async with inference_engine(base, adapter, timeout_s=timeout_s) as eng:
        app = FastAPI()
        app.include_router(create_router(eng))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            yield client, eng, app


@pytest.fixture
async def keys(engine):
    full = await build_profile(engine)
    await make_scope(engine, "b")
    _, ae = await issue(engine, "ae", scopes={"a": ["analysis:execute"]})
    _, pr = await issue(engine, "pr", scopes={"a": ["profiles:read", "profiles:write", "scope:read"]})
    _, both = await issue(engine, "both", scopes={"a": ["analysis:execute", "profiles:read"]})
    _, other = await issue(engine, "other", scopes={"b": ["analysis:execute"]})
    _, admin = await issue(engine, "adm", admin=list(AdminPermission))
    return dict(full=full, ae=ae.secret, pr=pr.secret, both=both.secret, other=other.secret, admin=admin.secret)


def body(**kw) -> dict:
    return {"text": TEXT, **kw}


class TestJourney:
    async def test_http_equals_python(self, engine, keys):  # AC-A03-01
        fx = await fixture_for(keys["full"], "a", {"kind": "output", "output": ok_output()})
        async with serve(engine, mock(fx)) as (api, eng, _):
            r = await api.post(URL, headers=h(keys["ae"]), json=body())
            py = await analysis_ops(eng).analyze_text("a", text=TEXT)
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store" and r.headers["content-type"].startswith("application/json")
        data = r.json()
        assert data["request_id"] == r.headers["x-request-id"]
        expect = py.model_dump(mode="json")
        data.pop("request_id"), expect.pop("request_id")
        assert data == expect  # same core, same result (request_id aside)
        assert data["status"] == "completed" and data["provenance"]["simulated"] is True
        assert data["classification"]["resolution"]["selected_category_id"] == "finance"
        assert "definition" not in json.dumps(data).lower().replace("definitions_digest", "")  # no definitions leak

    async def test_inconclusive_is_200_and_ranking_null_resolution(self, engine, keys):  # AC-A03-35
        out = ok_output()
        out["classification"]["status"], out["classification"]["reason"] = "inconclusive", "abstained"
        fx = await fixture_for(keys["full"], "a", {"kind": "output", "output": out})
        async with serve(engine, mock(fx)) as (api, _, _):
            r = await api.post(URL, headers=h(keys["ae"]), json=body())
        assert r.status_code == 200 and r.json()["status"] == "inconclusive"
        assert r.json()["classification"]["resolution"]["reason"] == "classification_inconclusive"

    async def test_options_in_body(self, engine, keys):
        adapter = CustomAdapter(ok_output(classification=False))
        async with serve(engine, adapter) as (api, _, _):
            r = await api.post(
                URL,
                headers=h(keys["ae"]),
                json=body(profile_id="contratos", version_number=1, capabilities=["detection"], context="aux"),
            )
        assert r.status_code == 200 and r.json()["classification"] == {"status": "not_requested"}
        assert adapter.requests[0].auxiliary_context == "aux"

    async def test_no_resource_no_get_no_202(self, engine, keys):  # D-A03-01
        async with serve(engine, CustomAdapter(ok_output())) as (api, _, _):
            assert (await api.get(URL, headers=h(keys["ae"]))).status_code == 405
            r = await api.post(URL, headers=h(keys["ae"]), json=body())
            assert r.status_code == 200 and "location" not in r.headers


class TestAuthorizationOrder:
    async def test_matrix(self, engine, keys):  # AC-A03-02, 03, 05, 06
        adapter = CustomAdapter(ok_output())
        async with serve(engine, adapter) as (api, _, _):
            assert (await api.post(URL, json=body())).status_code == 401
            assert (await api.post(URL, headers={"Authorization": "Bearer nope"}, json=body())).status_code == 401
            assert (await api.post(URL, headers=h(keys["pr"]), json=body())).status_code == 403  # other permissions
            assert (await api.post(URL, headers=h(keys["admin"]), json=body())).status_code == 404  # admin: no grants
            other = await api.post(URL, headers=h(keys["other"]), json=body())
            ghost = await api.post("/v1/scopes/zzz/analyze-text", headers=h(keys["other"]), json=body())
            assert other.status_code == ghost.status_code == 404
            assert other.json()["error"]["code"] == ghost.json()["error"]["code"] == "resource_not_accessible"
            assert adapter.calls == 0
            # analysis:execute alone cannot read profiles; adding profiles:read does
            assert (await api.get("/v1/scopes/a/resolved-profile", headers=h(keys["ae"]))).status_code == 403
            assert (await api.get("/v1/scopes/a/profiles/contratos", headers=h(keys["ae"]))).status_code == 403
            assert (await api.get("/v1/scopes/a", headers=h(keys["ae"]))).status_code == 403
            assert (await api.post(URL, headers=h(keys["ae"]), json=body())).status_code == 200
            assert (await api.get("/v1/scopes/a/resolved-profile", headers=h(keys["both"]))).status_code == 200
            assert (await api.post(URL, headers=h(keys["both"]), json=body())).status_code == 200

    async def test_validation_errors(self, engine, keys):  # AC-A03-42
        adapter = CustomAdapter(ok_output())
        async with serve(engine, adapter) as (api, _, _):
            ae = h(keys["ae"])
            for payload in (
                {"text": ""},
                {},
                {"text": 5},
                {"text": TEXT, "extra": 1},
                {"text": TEXT, "capabilities": []},
                {"text": TEXT, "capabilities": "detection"},
                {"text": TEXT, "version_number": 1},
                {"text": TEXT, "version_number": True, "profile_id": "contratos"},
                {"text": TEXT, "context": ""},
                {"text": TEXT, "profile_id": "Bad Id"},
                {"text": TEXT, "overrides": {"mode": "ranking"}},
            ):
                r = await api.post(URL, headers=ae, json=payload)
                assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed", payload
            r = await api.post(URL, headers=ae, content=b"not json")
            assert r.status_code == 422
            r = await api.post(URL, headers=ae, content=b'{"text": "\\ud800"}')  # lone surrogate escape
            assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed"
            r = await api.post(URL, headers=ae, content=b'{"text": "x", "profile_id": NaN}')
            assert r.status_code == 422
        assert adapter.calls == 0

    async def test_limits_and_existing_413_code(self, engine, keys):  # AC-A03-41, precision A
        adapter = CustomAdapter(ok_output())
        async with serve(engine, adapter) as (api, eng, _):
            ae = h(keys["ae"])
            r = await api.post(URL, headers=ae, json={"text": "a" * 8193})
            assert r.status_code == 422 and r.json()["error"]["code"] == "limit_exceeded"
            r = await api.post(URL, headers=ae, json={"text": "a" * 70_000})
            assert r.status_code == 413 and r.json()["error"]["code"] == "limit_exceeded"  # the existing code
            # an ASCII-escaped body reaches the HTTP limit before the logical one
            escaped = json.dumps({"text": "😀" * 6000}, ensure_ascii=True).encode()
            assert len(escaped) > eng.config.limits.max_body_bytes
            r = await api.post(URL, headers=ae, content=escaped)
            assert r.status_code == 413 and r.json()["error"]["code"] == "limit_exceeded"
            assert eng.config.limits.max_body_bytes == 65536
        assert adapter.calls == 0


class TestErrorMapping:
    async def test_adapter_errors(self, engine, keys):  # AC-A03-43, 45
        full = keys["full"]
        fxs = [
            await fixture_for(full, "a", {"kind": "unavailable", "reason": "loading"}, text="loading", fid="l"),
            await fixture_for(full, "a", {"kind": "hang"}, text="hang", fid="h"),
            await fixture_for(full, "a", {"kind": "output", "output": {"nonsense": 1}}, text="bad", fid="b"),
            await fixture_for(full, "a", {"kind": "crash"}, text="crash", fid="c"),
        ]
        async with serve(engine, mock(*fxs), timeout_s=0.3) as (api, _, _):
            ae = h(keys["ae"])

            async def call(text):
                return await api.post(URL, headers=ae, json={"text": text})

            r = await call("loading")
            assert (r.status_code, r.json()["error"]["code"]) == (503, "inference_unavailable")
            r = await call("hang")
            assert (r.status_code, r.json()["error"]["code"]) == (504, "inference_timeout")
            r = await call("bad")
            assert (r.status_code, r.json()["error"]["code"]) == (502, "inference_output_invalid")
            assert {p["code"] for p in r.json()["error"]["problems"]} >= {"unknown_field"}
            r = await call("crash")
            assert (r.status_code, r.json()["error"]["code"]) == (500, "internal_error")
            r = await call("unknown text")
            assert (r.status_code, r.json()["error"]["code"]) == (501, "inference_fixture_not_found")
            assert "unknown text" not in r.text

    async def test_profile_and_capability_errors(self, engine, keys):  # AC-A03-10, 11, 36
        full = keys["full"]
        await build_profile(engine, "a", "detonly", categories=False, default=False, create_scope=False, mode="ranking")
        adapter = CustomAdapter(ok_output())
        async with serve(engine, adapter) as (api, _, _):
            ae = h(keys["ae"])
            r = await api.post(URL, headers=ae, json=body(profile_id="detonly", capabilities=["classification"]))
            assert (r.status_code, r.json()["error"]["code"]) == (409, "capabilities_not_configured")
            r = await api.post(URL, headers=ae, json=body(profile_id="nope"))
            assert (r.status_code, r.json()["error"]["code"]) == (404, "resource_not_found")
            await full.disable_profile("a", "detonly")
            r = await api.post(URL, headers=ae, json=body(profile_id="detonly", version_number=1))
            assert (r.status_code, r.json()["error"]["code"]) == (409, "profile_not_selectable")
            await engine.admin(await admin_ctx(engine)).set_scope_status("a", "disabled")
            r = await api.post(URL, headers=ae, json=body())
            assert (r.status_code, r.json()["error"]["code"]) == (409, "scope_disabled")
        assert adapter.calls == 0

    async def test_not_configured_service(self, engine, keys):  # AC-A03-50, 58
        app = FastAPI()
        app.include_router(create_router(engine))  # no inference
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as api:
            assert (await api.post(URL, json=body())).status_code == 401  # authenticate first
            r = await api.post(URL, headers=h(keys["ae"]), json=body())
            assert (r.status_code, r.json()["error"]["code"]) == (503, "inference_not_configured")
            assert (await api.get("/v1/scopes/a/resolved-profile", headers=h(keys["pr"]))).status_code == 200


class TestObservabilityAndPersistence:
    async def test_no_content_logged_nothing_persisted(self, engine, keys, caplog):  # AC-A03-55, 56
        out = ok_output()
        fx = await fixture_for(keys["full"], "a", {"kind": "output", "output": out})
        before = await row_counts(engine)
        caplog.set_level(logging.DEBUG)
        async with serve(engine, mock(fx)) as (api, _, _):
            await api.post(URL, headers=h(keys["ae"]), json=body())
            await api.post(URL, headers=h(keys["ae"]), json={"text": "CANARY-TEXT-9d2e"})
        blob = caplog.text
        for canary in ("CANARY-TEXT", "Ana Ejemplo", "ana.ejemplo", "EjemploNoReal"):
            assert canary not in blob
        counts = await row_counts(engine)
        assert counts == before


class TestDisconnect:
    async def test_client_disconnect_cancels_the_adapter(self, engine, keys):  # AC-A03-44
        entered = asyncio.Event()

        async def hang(request):
            entered.set()
            await asyncio.sleep(3600)

        adapter = CustomAdapter(fn=hang)
        payload = json.dumps(body()).encode()
        async with serve(engine, adapter, timeout_s=60) as (_, eng, app):
            loop = asyncio.get_running_loop()
            disconnect_at = loop.time() + 0.4
            sent_body = False
            responses: list[dict] = []

            async def receive():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                if loop.time() >= disconnect_at:
                    return {"type": "http.disconnect"}
                await asyncio.sleep(3600)  # cancelled by the non-blocking poll in is_disconnected()
                return {}

            async def send(message):
                responses.append(message)

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": URL,
                "raw_path": URL.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [
                    (b"authorization", f"Bearer {keys['ae'].get_secret_value()}".encode()),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
                "server": ("t", 80),
                "client": ("c", 1),
            }
            before = len(asyncio.all_tasks())
            started = loop.time()
            await asyncio.wait_for(app(scope, receive, send), 10)
            assert loop.time() - started < 5  # did not wait for the 60 s timeout nor for the adapter
            assert adapter.calls == 1 and adapter.cancelled is True
            assert len(asyncio.all_tasks()) <= before + 1  # no orphan task left behind (this test's own task)
        assert entered.is_set()


class TestStandaloneServiceComposition:
    def test_settings_validation(self, tmp_path):  # E-A03-12
        from cawbase_content_engine.errors import InvalidConfigurationError
        from cawbase_content_engine.http.service import ServiceSettings, build_inference_setup

        base = {"CAWBASE_CONTENT_ENGINE_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"}
        s = ServiceSettings.from_env(base)
        assert s.inference_adapter == "none" and build_inference_setup(s) is None
        with pytest.raises(InvalidConfigurationError):
            ServiceSettings.from_env({**base, "CAWBASE_CONTENT_ENGINE_INFERENCE_ADAPTER": "mock"})  # needs fixtures
        with pytest.raises(InvalidConfigurationError):
            ServiceSettings.from_env({**base, "CAWBASE_CONTENT_ENGINE_INFERENCE_MOCK_FIXTURES": "x.json"})
        with pytest.raises(InvalidConfigurationError):
            ServiceSettings.from_env({**base, "CAWBASE_CONTENT_ENGINE_INFERENCE_ADAPTER": "gpt"})
        with pytest.raises(InvalidConfigurationError):
            ServiceSettings.from_env({**base, "CAWBASE_CONTENT_ENGINE_INFERENCE_TIMEOUT_S": "0"})
        fixtures = tmp_path / "fx.json"
        fixtures.write_text(json.dumps({"fixtures": []}))
        env = {
            **base,
            "CAWBASE_CONTENT_ENGINE_INFERENCE_ADAPTER": "mock",
            "CAWBASE_CONTENT_ENGINE_INFERENCE_MOCK_FIXTURES": str(fixtures),
        }
        setup = build_inference_setup(ServiceSettings.from_env(env))
        assert setup.descriptor.kind == "mock" and setup.timeout_s == 30.0
        fixtures.write_text("{")
        with pytest.raises(InvalidConfigurationError):
            build_inference_setup(ServiceSettings.from_env(env))


class TestHttpErrorBoundary:
    MARK = "SYNTHETIC_CONTENT_MARKER"

    async def call(self, engine, keys, exc_or_out, caplog):
        async def boom(request):
            if isinstance(exc_or_out, BaseException):
                raise exc_or_out
            return exc_or_out

        caplog.set_level(logging.DEBUG)
        async with serve(engine, CustomAdapter(fn=boom)) as (api, _, _):
            return await api.post(URL, headers=h(keys["ae"]), json=body())

    async def test_adapter_validation_error_is_500_not_422(self, engine, keys, caplog):
        from cawbase_content_engine import PermissionDeniedError, ValidationFailedError

        for exc in (ValidationFailedError(self.MARK, field=self.MARK), PermissionDeniedError(self.MARK)):
            r = await self.call(engine, keys, exc, caplog)
            assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
            assert self.MARK not in r.text and self.MARK not in caplog.text

    async def test_allowed_errors_do_not_leak_manipulated_text(self, engine, keys, caplog):
        from cawbase_content_engine import (
            InferenceFixtureNotFoundError,
            InferenceOutputInvalidError,
            InferenceTimeoutError,
            InferenceUnavailableError,
        )

        bad = InferenceOutputInvalidError([], self.MARK)
        bad.problems = [{"code": self.MARK}]  # type: ignore[list-item]
        cases = [
            (InferenceUnavailableError("stopped", self.MARK), 503, "inference_unavailable"),
            (InferenceTimeoutError(self.MARK), 504, "inference_timeout"),
            (InferenceFixtureNotFoundError(self.MARK), 501, "inference_fixture_not_found"),
            (bad, 502, "inference_output_invalid"),
        ]
        for exc, status, code in cases:
            r = await self.call(engine, keys, exc, caplog)
            assert (r.status_code, r.json()["error"]["code"]) == (status, code)
            assert self.MARK not in r.text and self.MARK not in caplog.text
        assert r.json()["error"]["problems"] == [{"code": "adapter_diagnostic_invalid"}]

    async def test_huge_integer_score_is_502(self, engine, keys, caplog):
        out = ok_output()
        out["classification"]["scores"][0]["score"] = 10**400
        r = await self.call(engine, keys, out, caplog)
        assert (r.status_code, r.json()["error"]["code"]) == (502, "inference_output_invalid")
        assert r.json()["error"]["problems"] == [
            {"code": "invalid_score", "capability": "classification", "index": "0"}
        ]
