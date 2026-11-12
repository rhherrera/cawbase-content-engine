"""A02 over HTTP (in-memory ASGI), same operations as Python: AC-A02-02, 06, 11, 13, 23, 25, 29, 50, 53."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest
from fastapi import FastAPI

from cawbase_content_engine import AdminPermission
from cawbase_content_engine.http import create_router

from .helpers import issue, make_scope

PR, PW = "profiles:read", "profiles:write"


@pytest.fixture
async def api(engine):
    app = FastAPI()
    app.include_router(create_router(engine))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client


def h(secret, **extra) -> dict:
    return {"Authorization": f"Bearer {secret.get_secret_value()}", **extra}


def shape(r: httpx.Response):
    body = r.json()
    body["error"].pop("request_id", None)
    return r.status_code, body, {k: v for k, v in r.headers.items() if k not in ("x-request-id", "content-length")}


@pytest.fixture
async def keys(engine):
    await make_scope(engine, "a")
    await make_scope(engine, "b")
    _, rw = await issue(engine, "rw", scopes={"a": [PR, PW]})
    _, ro = await issue(engine, "ro", scopes={"a": [PR]})
    _, wo = await issue(engine, "wo", scopes={"a": [PW]})
    _, legacy = await issue(engine, "legacy", scopes={"a": ["scope:read", "scope:write"]})
    _, admin = await issue(engine, "adm", admin=list(AdminPermission))
    _, other = await issue(engine, "other", scopes={"b": [PR, PW]})
    return dict(rw=rw.secret, ro=ro.secret, wo=wo.secret, legacy=legacy.secret, admin=admin.secret, other=other.secret)


S = "/v1/scopes/a"


class TestJourney:
    async def test_full_http_journey(self, api, keys):  # AC-A02-02, 53
        rw = h(keys["rw"])
        r = await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "gen", "display_name": "General"})
        assert r.status_code == 201 and r.headers["etag"] == '"1"' and r.headers["x-request-id"]
        prof = r.json()
        assert prof["status"] == "active" and prof["catalog_provenance"]["catalog_version"] == 1
        assert (
            await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "gen", "display_name": "General"})
        ).status_code == 200
        assert (
            await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "gen", "display_name": "X"})
        ).status_code == 409
        # categories: create / get / patch / list
        c = await api.post(
            f"{S}/profiles/gen/categories",
            headers=rw,
            json={
                "category_id": "billing",
                "name": "Facturación",
                "definition": "Facturas",
                "priority": 1,
                "examples": {"positive": ["uno"]},
                "translations": {"en": {"name": "Billing", "definition": "Invoices"}},
            },
        )
        assert (
            c.status_code == 201 and c.json()["priority"] == 1 and c.json()["translations"]["en"]["name"] == "Billing"
        )
        got = await api.get(f"{S}/profiles/gen/categories/billing", headers=rw)
        assert got.status_code == 200 and got.headers["etag"] == '"1"'
        p = await api.patch(
            f"{S}/profiles/gen/categories/billing",
            headers={**rw, "If-Match": '"1"'},
            json={"definition": "Nueva", "priority": None},
        )
        assert p.status_code == 200 and p.json()["revision"] == 2 and p.json()["priority"] is None
        types = await api.get(f"{S}/profiles/gen/sensitive-types", headers=rw)
        assert [t["type_id"] for t in types.json()["items"]][:2] == ["api_key_secret", "financial_information"]
        tp = await api.patch(
            f"{S}/profiles/gen/sensitive-types/password", headers={**rw, "If-Match": '"1"'}, json={"name": "Claves"}
        )
        assert tp.status_code == 200 and tp.json()["name"] == "Claves"
        assert (await api.post(f"{S}/profiles/gen/sensitive-types/government_id/disable", headers=rw)).json()[
            "state"
        ] == "disabled"
        # ranking
        rk = await api.get(f"{S}/profiles/gen/ranking", headers=rw)
        assert rk.json() == {"mode": "ranking", "eligibility_threshold": 0.0, "tie_tolerance": 0.0, "revision": 1}
        rk2 = await api.patch(
            f"{S}/profiles/gen/ranking", headers={**rw, "If-Match": '"1"'}, json={"tie_tolerance": 0.25}
        )
        assert rk2.status_code == 200 and rk2.json()["tie_tolerance"] == 0.25 and rk2.headers["etag"] == '"2"'
        # publish
        draft = (await api.get(f"{S}/profiles/gen", headers=rw)).json()["draft_revision"]
        pub = await api.post(f"{S}/profiles/gen/publish", headers=rw, json={"expected_draft_revision": draft})
        assert pub.status_code == 201 and pub.json()["created"] is True
        version = pub.json()["version"]
        assert version["version_number"] == 1 and version["snapshot"]["ranking"]["tie_tolerance"] == 0.25
        again = await api.post(f"{S}/profiles/gen/publish", headers=rw, json={"expected_draft_revision": draft})
        assert (
            again.status_code == 200
            and again.headers["idempotent-replay"] == "true"
            and again.json()["version"] == version
        )
        stale = await api.post(f"{S}/profiles/gen/publish", headers=rw, json={"expected_draft_revision": draft - 1})
        assert stale.status_code == 412 and stale.json()["error"]["code"] == "revision_conflict"
        lst = await api.get(f"{S}/profiles/gen/versions", headers=rw)
        assert [v["version_number"] for v in lst.json()["items"]] == [1] and "snapshot" not in lst.json()["items"][0]
        assert (await api.get(f"{S}/profiles/gen/versions/1", headers=rw)).json() == version
        # default + resolution
        d = await api.get(f"{S}/default-profile", headers=rw)
        assert d.status_code == 200 and d.json()["profile_id"] is None and d.headers["etag"] == '"0"'
        assert (await api.get(f"{S}/resolved-profile", headers=rw)).json()["error"]["code"] == "default_profile_not_set"
        put = await api.put(f"{S}/default-profile", headers={**rw, "If-Match": '"0"'}, json={"profile_id": "gen"})
        assert put.status_code == 200 and put.json()["revision"] == 1 and put.headers["etag"] == '"1"'
        res = await api.get(f"{S}/resolved-profile", headers=rw)
        assert res.json()["selection"] == "default" and res.json()["version"]["version_id"] == version["version_id"]
        assert (await api.get(f"{S}/resolved-profile?profile_id=gen&version=1", headers=rw)).json()[
            "selection"
        ] == "explicit"
        assert (await api.get(f"{S}/resolved-profile?version=1", headers=rw)).status_code == 422
        dis = await api.post(f"{S}/profiles/gen/disable", headers=rw)
        assert dis.status_code == 409 and dis.json()["error"]["code"] == "profile_is_default"
        clr = await api.delete(f"{S}/default-profile", headers={**rw, "If-Match": '"1"'})
        assert clr.status_code == 200 and clr.json() == {**clr.json(), "profile_id": None, "revision": 2}
        assert (await api.post(f"{S}/profiles/gen/disable", headers=rw)).json()["status"] == "disabled"
        assert (await api.get(f"{S}/resolved-profile?profile_id=gen&version=1", headers=rw)).json()["error"][
            "code"
        ] == "profile_not_selectable"
        assert (await api.get(f"{S}/profiles/gen/versions/1", headers=rw)).status_code == 200  # history stays readable
        dele = await api.post(f"{S}/profiles/gen/delete", headers=rw)
        assert dele.json()["status"] == "deleted"
        assert (await api.get(f"{S}/profiles/gen", headers=rw)).status_code == 404
        assert (await api.get(f"{S}/profiles/gen?include_deleted=true", headers=rw)).json()["status"] == "deleted"
        assert (await api.get(f"{S}/profiles", headers=rw)).json()["items"] == []
        assert [
            p["profile_id"] for p in (await api.get(f"{S}/profiles?status=deleted", headers=rw)).json()["items"]
        ] == ["gen"]
        assert (await api.get(f"{S}/profiles?status=active,disabled", headers=rw)).json()["items"] == []

    async def test_filters_pagination_and_query_validation(self, api, keys):  # AC-A02-13
        rw = h(keys["rw"])
        for n in range(3):
            await api.post(
                f"{S}/profiles",
                headers=rw,
                json={"profile_id": f"p{n}", "display_name": "P", "initial_catalog": "empty"},
            )
        await api.post(f"{S}/profiles/p1/disable", headers=rw)
        ids = lambda r: [p["profile_id"] for p in r.json()["items"]]  # noqa: E731
        assert ids(await api.get(f"{S}/profiles", headers=rw)) == ["p0", "p1", "p2"]
        assert ids(await api.get(f"{S}/profiles?status=disabled", headers=rw)) == ["p1"]
        assert ids(await api.get(f"{S}/profiles?status=active&status=disabled", headers=rw)) == ["p0", "p1", "p2"]
        page = await api.get(f"{S}/profiles?limit=2", headers=rw)
        assert ids(page) == ["p0", "p1"] and page.json()["next_cursor"]
        assert ids(await api.get(f"{S}/profiles?limit=2&cursor={page.json()['next_cursor']}", headers=rw)) == ["p2"]
        for bad in ("status=nope", "limit=0", "limit=x", "cursor=@@@"):
            assert (await api.get(f"{S}/profiles?{bad}", headers=rw)).status_code == 422, bad
        assert (await api.get(f"{S}/profiles/p0?include_deleted=maybe", headers=rw)).status_code == 422


class TestAuthorizationAndErrors:
    async def test_matrix_401_403_404(self, api, keys):  # AC-A02-06
        rw = h(keys["rw"])
        await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "p", "display_name": "P"})
        await api.post(f"{S}/profiles/p/publish", headers=rw, json={"expected_draft_revision": 1})
        reads = [
            ("GET", f"{S}/profiles"),
            ("GET", f"{S}/profiles/p"),
            ("GET", f"{S}/profiles/p/ranking"),
            ("GET", f"{S}/profiles/p/categories"),
            ("GET", f"{S}/profiles/p/sensitive-types"),
            ("GET", f"{S}/profiles/p/versions"),
            ("GET", f"{S}/profiles/p/versions/1"),
            ("GET", f"{S}/default-profile"),
            ("GET", f"{S}/resolved-profile?profile_id=p"),
        ]
        writes = [
            ("POST", f"{S}/profiles", {"profile_id": "x", "display_name": "X"}),
            ("PATCH", f"{S}/profiles/p", {"description": "x"}),
            ("POST", f"{S}/profiles/p/disable", None),
            ("PATCH", f"{S}/profiles/p/ranking", {"tie_tolerance": 0.1}),
            ("POST", f"{S}/profiles/p/categories", {"category_id": "c", "name": "C", "definition": "d"}),
            ("POST", f"{S}/profiles/p/sensitive-types/password/delete", None),
            ("POST", f"{S}/profiles/p/publish", {"expected_draft_revision": 1}),
            ("PUT", f"{S}/default-profile", {"profile_id": "p"}),
            ("DELETE", f"{S}/default-profile", None),
        ]
        for method, path in reads:
            assert (await api.request(method, path, headers=h(keys["ro"]))).status_code == 200, path
            for who in ("wo", "legacy"):  # profiles:write alone and scope:* do not read
                assert (await api.request(method, path, headers=h(keys[who]))).status_code == 403, (who, path)
            assert (await api.request(method, path, headers=h(keys["admin"]))).status_code in (403, 404), path
            assert (await api.request(method, path)).status_code == 401
        for method, path, body in writes:
            r = await api.request(method, path, headers=h(keys["ro"], **{"If-Match": '"1"'}), json=body)
            assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied", (method, path)
            r = await api.request(method, path, headers=h(keys["legacy"], **{"If-Match": '"1"'}), json=body)
            assert r.status_code == 403, (method, path)
        # no grant at all in the scope -> 404, byte-identical for an existing and a non-existing scope
        for method, path in reads:
            other = await api.request(method, path.replace("/scopes/a/", "/scopes/b/"), headers=h(keys["rw"]))
            ghost = await api.request(method, path.replace("/scopes/a/", "/scopes/zzz/"), headers=h(keys["rw"]))
            assert other.status_code == 404 and shape(other) == shape(ghost), path
        # a key of scope b cannot touch scope a, and a missing/foreign child looks the same as an absent one
        assert (await api.get(f"{S}/profiles/p", headers=h(keys["other"]))).status_code == 404
        assert (await api.get(f"{S}/profiles/missing", headers=rw)).status_code == 404
        r1 = await api.get(f"{S}/profiles/p/categories/none", headers=rw)
        r2 = await api.get(f"{S}/profiles/missing/categories/none", headers=rw)
        assert r1.status_code == r2.status_code == 404 and shape(r1) == shape(r2)

    async def test_write_preconditions_bodies_and_error_envelope(self, api, keys):  # AC-A02-11, 53
        rw = h(keys["rw"])
        await api.post(
            f"{S}/profiles", headers=rw, json={"profile_id": "p", "display_name": "P", "initial_catalog": "empty"}
        )
        await api.post(
            f"{S}/profiles/p/categories", headers=rw, json={"category_id": "c", "name": "C", "definition": "d"}
        )
        cat = f"{S}/profiles/p/categories/c"
        for path, body in (
            (f"{S}/profiles/p", {"description": "x"}),
            (cat, {"name": "N"}),
            (f"{S}/profiles/p/ranking", {"mode": "ranking"}),
        ):
            r = await api.patch(path, headers=rw, json=body)  # no If-Match
            assert r.status_code == 428 and r.json()["error"]["code"] == "precondition_required"
            assert (await api.patch(path, headers={**rw, "If-Match": "abc"}, json=body)).status_code == 422
            assert (await api.patch(path, headers={**rw, "If-Match": '"0"'}, json=body)).status_code == 422
        im = {**rw, "If-Match": '"1"'}
        for path, body in (
            (f"{S}/profiles/p", {}),
            (f"{S}/profiles/p", {"status": "deleted"}),
            (f"{S}/profiles/p", {"profile_id": "z"}),
            (f"{S}/profiles/p", {"display_name": None}),
            (cat, {"state": "disabled"}),
            (cat, {"category_id": "z"}),
            (cat, {"name": None}),
            (cat, {"priority": 0}),
            (cat, {"examples": {"positive": "x"}}),
            (f"{S}/profiles/p/ranking", {"tie_tolerance": 2}),
            (f"{S}/profiles/p/ranking", {"mode": "x"}),
            (f"{S}/profiles/p/sensitive-types/password", {"priority": 1}),
        ):
            r = await api.patch(path, headers=im, json=body)
            assert r.status_code in (404, 422), (path, body, r.status_code)
            if "sensitive-types" not in path:
                assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed", (path, body)
        # NaN / Infinity are not JSON: rejected at the boundary
        for raw in ('{"tie_tolerance": NaN}', '{"eligibility_threshold": Infinity}', '{"tie_tolerance": -Infinity}'):
            r = await api.patch(
                f"{S}/profiles/p/ranking", headers={**im, "Content-Type": "application/json"}, content=raw
            )
            assert r.status_code == 422, raw
        assert (await api.patch(f"{S}/profiles/p/ranking", headers=im, json={"tie_tolerance": 1})).json()[
            "tie_tolerance"
        ] == 1.0
        # stale revision with an already-applied value -> 412; same revision and same values -> 200 without change
        r = await api.patch(f"{S}/profiles/p/ranking", headers=im, json={"tie_tolerance": 1})
        assert r.status_code == 412 and r.json()["error"]["code"] == "revision_conflict"
        same = await api.patch(f"{S}/profiles/p/ranking", headers={**rw, "If-Match": '"2"'}, json={"tie_tolerance": 1})
        assert same.status_code == 200 and same.json()["revision"] == 2
        # unknown creation fields, bad JSON and oversized bodies
        assert (
            await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "q", "display_name": "Q", "x": 1})
        ).status_code == 422
        assert (await api.post(f"{S}/profiles", headers=rw, content=b"{nope")).status_code == 422
        big = await api.post(
            f"{S}/profiles/p/categories",
            headers=rw,
            json={"category_id": "big", "name": "B", "definition": "d" * (70 * 1024)},
        )
        assert big.status_code == 413 and big.json()["error"]["code"] == "limit_exceeded"
        # profile_invalid carries problems with codes and ids only
        await api.post(
            f"{S}/profiles/p/categories",
            headers=rw,
            json={"category_id": "x", "name": "Secret name", "definition": "Secret def"},
        )
        r = await api.patch(f"{S}/profiles/p/ranking", headers={**rw, "If-Match": '"2"'}, json={"mode": "resolution"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "profile_invalid"
        assert {"code": "priority_missing", "category_id": "c"} in r.json()["error"]["problems"]
        assert "Secret" not in r.text
        # state-transition and conflict codes
        assert (await api.post(f"{S}/profiles/p/categories/c/delete", headers=rw)).json()["state"] == "deleted"
        r = await api.post(f"{S}/profiles/p/categories/c/enable", headers=rw)
        assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state_transition"
        r = await api.post(
            f"{S}/profiles/p/categories",
            headers=rw,
            json={"category_id": "y", "name": "SECRET Name", "definition": "d"},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "name_conflict"
        await api.post(
            f"{S}/profiles/p/categories",
            headers=rw,
            json={"category_id": "p1", "name": "P1", "definition": "d", "priority": 5},
        )
        r = await api.post(
            f"{S}/profiles/p/categories",
            headers=rw,
            json={"category_id": "p2", "name": "P2", "definition": "d", "priority": 5},
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "priority_conflict"
        await api.post(f"{S}/profiles/p/disable", headers=rw)
        r = await api.post(
            f"{S}/profiles/p/categories", headers=rw, json={"category_id": "z", "name": "Z", "definition": "d"}
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == "profile_disabled"
        r = await api.post(f"{S}/profiles/p/publish", headers=rw, json={"expected_draft_revision": 1})
        assert r.status_code == 409 and r.json()["error"]["code"] == "profile_disabled"

    async def test_empty_configuration_cannot_be_published_and_detection_only_can(self, api, keys):  # AC-A02-22
        rw = h(keys["rw"])
        await api.post(
            f"{S}/profiles", headers=rw, json={"profile_id": "e", "display_name": "E", "initial_catalog": "empty"}
        )
        r = await api.post(f"{S}/profiles/e/publish", headers=rw, json={"expected_draft_revision": 1})
        assert r.status_code == 422 and r.json()["error"]["problems"] == [{"code": "no_enabled_elements"}]
        await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "d", "display_name": "D"})
        r = await api.post(f"{S}/profiles/d/publish", headers=rw, json={"expected_draft_revision": 1})
        assert r.status_code == 201 and r.json()["version"]["capabilities"] == {
            "classification": False,
            "detection": True,
        }

    async def test_scope_disabled_over_http(self, api, keys, engine):  # AC-A02-08
        rw = h(keys["rw"])
        await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "p", "display_name": "P"})
        await api.post(f"{S}/profiles/p/publish", headers=rw, json={"expected_draft_revision": 1})
        await engine.admin(engine.trusted_admin_context(list(AdminPermission))).set_scope_status("a", "disabled")
        assert (await api.get(f"{S}/profiles/p/versions/1", headers=rw)).status_code == 200
        assert (await api.get(f"{S}/profiles", headers=rw)).status_code == 200
        for r in (
            await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "n", "display_name": "N"}),
            await api.get(f"{S}/resolved-profile?profile_id=p", headers=rw),
            await api.post(f"{S}/profiles/p/disable", headers=rw),
        ):
            assert r.status_code == 409 and r.json()["error"]["code"] == "scope_disabled"

    async def test_admin_keys_get_no_profile_content_and_pre_existing_keys_are_not_widened(
        self, api, keys
    ):  # AC-A02-05, 07
        rw = h(keys["rw"])
        await api.post(f"{S}/profiles", headers=rw, json={"profile_id": "p", "display_name": "P"})
        adm = await api.get(f"{S}/profiles", headers=h(keys["admin"]))
        assert adm.status_code in (403, 404) and "p" not in json.dumps(adm.json().get("items", []))
        assert (await api.get("/v1/admin/profiles", headers=h(keys["admin"]))).status_code == 404
        me = (await api.get("/v1/auth/self", headers=h(keys["legacy"]))).json()
        assert me["grants"]["scopes"] == [{"scope_id": "a", "permissions": ["scope:read", "scope:write"]}]


class TestObservabilityOverHttp:
    async def test_no_content_in_responses_logs_or_events(self, api, keys, engine, caplog):  # AC-A02-50
        caplog.set_level(logging.DEBUG, logger="cawbase_content_engine")  # our own loggers, at their most verbose
        canary = "CANARY" + uuid.uuid4().hex
        rw = h(keys["rw"])
        await api.post(
            f"{S}/profiles", headers=rw, json={"profile_id": "p", "display_name": canary, "initial_catalog": "empty"}
        )
        await api.post(
            f"{S}/profiles/p/categories",
            headers=rw,
            json={"category_id": "c", "name": canary, "definition": canary, "examples": {"positive": [canary]}},
        )
        errors = [
            await api.post(
                f"{S}/profiles/p/categories", headers=rw, json={"category_id": "d", "name": canary, "definition": "x"}
            ),
            await api.patch(
                f"{S}/profiles/p/categories/c", headers={**rw, "If-Match": '"9"'}, json={"name": canary + "2"}
            ),
            await api.patch(
                f"{S}/profiles/p/categories/c",
                headers={**rw, "If-Match": '"1"'},
                json={"definition": canary + "x" * 2000},
            ),
        ]
        assert all(canary not in r.text for r in errors)
        from .a02_helpers import events

        dump = json.dumps([[e.action, e.detail, e.actor_label] for e in await events(engine)])
        assert canary not in dump and canary not in caplog.text
        assert keys["rw"].get_secret_value() not in caplog.text
