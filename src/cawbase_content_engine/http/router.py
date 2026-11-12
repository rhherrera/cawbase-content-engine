"""Routers over the very same operations the Python API uses (no separate business logic)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .. import validation as v
from ..contexts import AuthenticatedPrincipal
from ..engine import ContentEngine
from ..errors import (
    AuthenticationError,
    EngineError,
    PayloadTooLargeError,
    PreconditionRequiredError,
    ValidationFailedError,
)
from ..models import ApiKeyGrants, ApiKeyInfo, IssuedApiKey
from ..operations.scopes import UNSET

log = logging.getLogger("cawbase_content_engine.http")


def _envelope(exc: EngineError, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if exc.http_status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    error: dict[str, Any] = {"code": exc.code, "message": exc.message, "request_id": request_id}
    problems = getattr(exc, "problems", None)
    if problems:  # profile_invalid: codes and ids only, never content
        error["problems"] = problems
    return JSONResponse({"error": error}, status_code=exc.http_status or 500, headers=headers)


class _EngineRoute(APIRoute):
    """Per-route error mapping, so a router mounted into a host app needs no app-level handlers."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            request_id = uuid.uuid4().hex
            request.state.request_id = request_id
            try:
                response = await original(request)
            except EngineError as exc:
                if exc.http_status is None:
                    exc = ValidationFailedError()
                if exc.code == "authentication_failed":
                    log.info("auth_failed", extra={"request_id": request_id})
                return _envelope(exc, request_id)
            except RequestValidationError:
                return _envelope(ValidationFailedError(), request_id)
            except Exception as exc:  # sanitized: class name only, never the message
                log.error("internal_error", extra={"request_id": request_id, "error_class": type(exc).__name__})
                return JSONResponse(
                    {"error": {"code": "internal_error", "message": "Internal error.", "request_id": request_id}},
                    status_code=500,
                    headers={"X-Request-ID": request_id},
                )
            response.headers["X-Request-ID"] = request_id
            return response

        return handler


def _json(model: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(model, status_code=status, headers=headers)


def _grants_json(g: ApiKeyGrants) -> dict[str, Any]:
    return {
        "admin": sorted(p.value for p in g.admin),
        "scopes": [{"scope_id": s, "permissions": sorted(p.value for p in ps)} for s, ps in sorted(g.scopes.items())],
    }


def _key_json(info: ApiKeyInfo) -> dict[str, Any]:
    d = info.model_dump(mode="json", exclude={"grants"})
    d["grants"] = _grants_json(info.grants)
    return d


def _issued_json(i: IssuedApiKey) -> dict[str, Any]:
    return {"key": _key_json(i.info), "secret": i.secret.get_secret_value() if i.secret else None}


async def _read_body(request: Request, limit: int) -> bytes:
    """Counts the bytes actually received; does not trust ``Content-Length``."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError()
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_object(raw: bytes) -> dict[str, Any]:
    def _reject(_constant: str) -> Any:
        raise ValueError("non-finite number")

    try:
        data = json.loads(raw or b"{}", parse_constant=_reject)
    except (ValueError, UnicodeDecodeError):
        raise ValidationFailedError("The body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ValidationFailedError("The body must be a JSON object.")
    return data


def _parse_grants(raw: Any, engine: ContentEngine) -> ApiKeyGrants:
    from ..operations import api_keys as ak

    if not isinstance(raw, dict):
        raise ValidationFailedError("grants must be an object.", field="grants")
    admin = raw.get("admin", [])
    scopes_raw = raw.get("scopes", [])
    if not isinstance(admin, list) or not isinstance(scopes_raw, list):
        raise ValidationFailedError("Invalid grants.", field="grants")
    scopes: dict[str, set[str]] = {}
    for item in scopes_raw:
        if not isinstance(item, dict) or not isinstance(item.get("permissions"), list):
            raise ValidationFailedError("Invalid grants.", field="grants")
        scopes.setdefault(v.scope_id(item.get("scope_id"), "grants"), set()).update(item["permissions"])
    return ak.normalize_grants(admin, scopes, engine.config.limits)


def _expected_revision(request: Request, *, low: int = 1) -> int:
    header = request.headers.get("if-match")
    if header is None:
        raise PreconditionRequiredError("The If-Match header is required.", field="If-Match")
    raw = header.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    if not raw.isdigit() or int(raw) < low:
        raise ValidationFailedError("Invalid If-Match.", field="If-Match")
    return int(raw)


def create_router(engine: ContentEngine) -> APIRouter:
    """Business routes. Mount explicitly; registers nothing by itself and never migrates."""
    router = APIRouter(route_class=_EngineRoute)
    max_body = engine.config.limits.max_body_bytes

    async def principal(request: Request) -> AuthenticatedPrincipal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise AuthenticationError()
        return await engine.authenticate_api_key(token.strip())

    @router.get("/v1/auth/self")
    async def auth_self(request: Request) -> Response:
        p = await principal(request)
        return _json(
            {
                "key_id": p.key_id,
                "client_id": p.client_id,
                "grants": _grants_json(ApiKeyGrants(admin=p.admin_permissions, scopes=p.scope_grants)),
            }
        )

    @router.get("/v1/scopes/{scope_id}")
    async def get_scope(scope_id: str, request: Request) -> Response:
        ctx = (await principal(request)).scope_context(scope_id)
        scope = await engine.scoped(ctx).get_scope(scope_id)
        return _json(scope.model_dump(mode="json"), headers={"ETag": f'"{scope.revision}"'})

    @router.patch("/v1/scopes/{scope_id}")
    async def patch_scope(scope_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        ctx = (await principal(request)).scope_context(scope_id)
        expected = _expected_revision(request)
        data = _parse_object(raw)
        unknown = set(data) - {"display_name", "description", "metadata"}
        if unknown:
            raise ValidationFailedError("Unknown fields.")
        scope = await engine.scoped(ctx).update_scope(
            scope_id,
            expected_revision=expected,
            display_name=data.get("display_name", UNSET),
            description=data.get("description", UNSET),
            metadata=data.get("metadata", UNSET),
        )
        return _json(scope.model_dump(mode="json"), headers={"ETag": f'"{scope.revision}"'})

    async def admin(request: Request) -> Any:
        return engine.admin((await principal(request)).admin_context())

    @router.post("/v1/admin/scopes")
    async def create_scope(request: Request) -> Response:
        raw = await _read_body(request, max_body)
        ops = await admin(request)
        d = _parse_object(raw)
        res = await ops.create_scope(
            d.get("scope_id"),
            d.get("display_name"),
            description=d.get("description"),
            metadata=d.get("metadata", {}),
        )
        return _json(res.value.model_dump(mode="json"), 201 if res.created else 200)

    @router.get("/v1/admin/scopes")
    async def list_scopes(
        request: Request, status: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> Response:
        page = await (await admin(request)).list_scopes(status=status, limit=limit, cursor=cursor)
        return _json({"items": [s.model_dump(mode="json") for s in page.items], "next_cursor": page.next_cursor})

    @router.get("/v1/admin/scopes/{scope_id}")
    async def admin_get_scope(scope_id: str, request: Request) -> Response:
        return _json((await (await admin(request)).get_scope(scope_id)).model_dump(mode="json"))

    @router.post("/v1/admin/scopes/{scope_id}/disable")
    async def disable_scope(scope_id: str, request: Request) -> Response:
        return _json((await (await admin(request)).set_scope_status(scope_id, "disabled")).model_dump(mode="json"))

    @router.post("/v1/admin/scopes/{scope_id}/enable")
    async def enable_scope(scope_id: str, request: Request) -> Response:
        return _json((await (await admin(request)).set_scope_status(scope_id, "active")).model_dump(mode="json"))

    @router.post("/v1/admin/clients")
    async def create_client(request: Request) -> Response:
        raw = await _read_body(request, max_body)
        ops = await admin(request)
        d = _parse_object(raw)
        res = await ops.create_client(d.get("name"), d.get("display_name"))
        return _json(res.value.model_dump(mode="json"), 201 if res.created else 200)

    @router.get("/v1/admin/clients")
    async def list_clients(request: Request, limit: int | None = None, cursor: str | None = None) -> Response:
        page = await (await admin(request)).list_clients(limit=limit, cursor=cursor)
        return _json({"items": [c.model_dump(mode="json") for c in page.items], "next_cursor": page.next_cursor})

    @router.get("/v1/admin/clients/{client_id}")
    async def get_client(client_id: str, request: Request) -> Response:
        return _json((await (await admin(request)).get_client(client_id)).model_dump(mode="json"))

    @router.post("/v1/admin/clients/{client_id}/disable")
    async def disable_client(client_id: str, request: Request) -> Response:
        return _json((await (await admin(request)).set_client_status(client_id, "disabled")).model_dump(mode="json"))

    @router.post("/v1/admin/clients/{client_id}/enable")
    async def enable_client(client_id: str, request: Request) -> Response:
        return _json((await (await admin(request)).set_client_status(client_id, "active")).model_dump(mode="json"))

    @router.post("/v1/admin/clients/{client_id}/api-keys")
    async def issue_key(client_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        ops = await admin(request)
        rid = request.headers.get("idempotency-key")
        if rid is None:
            raise ValidationFailedError("The Idempotency-Key header is required.", field="Idempotency-Key")
        d = _parse_object(raw)
        exp = d.get("expires_at")
        if exp is not None:
            from datetime import datetime

            try:
                exp = datetime.fromisoformat(exp)
            except (TypeError, ValueError):
                raise ValidationFailedError("Invalid expires_at.", field="expires_at") from None
        issued = await ops.issue_api_key(
            client_id,
            request_id=rid,
            grants=_parse_grants(d.get("grants"), engine),
            label=d.get("label"),
            expires_at=exp,
        )
        headers = {"Idempotent-Replay": "true"} if issued.replayed else None
        return _json(_issued_json(issued), 200 if issued.replayed else 201, headers)

    @router.get("/v1/admin/clients/{client_id}/api-keys")
    async def list_keys(client_id: str, request: Request) -> Response:
        return _json({"items": [_key_json(k) for k in await (await admin(request)).list_api_keys(client_id)]})

    @router.post("/v1/admin/api-keys/{key_id}/revoke")
    async def revoke_key(key_id: str, request: Request) -> Response:
        return _json(_key_json(await (await admin(request)).revoke_api_key(key_id)))

    @router.get("/v1/admin/diagnostics")
    async def diagnostics(request: Request) -> Response:
        return _json((await (await admin(request)).diagnostics()).model_dump(mode="json"))

    # ---- A02: profiles, categories, sensitive types, versions, default and resolution ----------------
    def etag(revision: int) -> dict[str, str]:
        return {"ETag": f'"{revision}"'}

    def dump(model: Any) -> dict[str, Any]:
        result: dict[str, Any] = model.model_dump(mode="json")
        return result

    def page_json(page: Any) -> dict[str, Any]:
        return {"items": [dump(i) for i in page.items], "next_cursor": page.next_cursor}

    def flag(value: str | None) -> bool:
        if value is None:
            return False
        if value.lower() not in ("true", "false"):
            raise ValidationFailedError("Invalid boolean.")
        return value.lower() == "true"

    def multi(values: list[str] | None) -> list[str] | None:
        if not values:
            return None
        return [part for raw in values for part in raw.split(",") if part]

    def only(data: dict[str, Any], allowed: set[str]) -> None:
        if set(data) - allowed:
            raise ValidationFailedError("Unknown fields.")

    async def ops(request: Request, scope_id: str) -> Any:
        return engine.scoped((await principal(request)).scope_context(scope_id))

    def intp(value: str, field: str) -> int:
        if not value.isdigit():
            raise ValidationFailedError(f"Invalid {field}.", field=field)
        return int(value)

    @router.post("/v1/scopes/{scope_id}/profiles")
    async def create_profile(scope_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        o = await ops(request, scope_id)
        d = _parse_object(raw)
        only(d, {"profile_id", "display_name", "description", "language", "initial_catalog"})
        res = await o.create_profile(
            scope_id,
            d.get("profile_id"),
            d.get("display_name"),
            description=d.get("description"),
            language=d.get("language", "es"),
            initial_catalog=d.get("initial_catalog", "starter"),
        )
        return _json(dump(res.value), 201 if res.created else 200, etag(res.value.revision))

    @router.get("/v1/scopes/{scope_id}/profiles")
    async def list_profiles(
        scope_id: str,
        request: Request,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Response:
        page = await (await ops(request, scope_id)).list_profiles(
            scope_id, status=multi(request.query_params.getlist("status")), limit=limit, cursor=cursor
        )
        return _json(page_json(page))

    @router.get("/v1/scopes/{scope_id}/profiles/{profile_id}")
    async def get_profile(
        scope_id: str, profile_id: str, request: Request, include_deleted: str | None = None
    ) -> Response:
        p = await (await ops(request, scope_id)).get_profile(
            scope_id, profile_id, include_deleted=flag(include_deleted)
        )
        return _json(dump(p), headers=etag(p.revision))

    @router.patch("/v1/scopes/{scope_id}/profiles/{profile_id}")
    async def patch_profile(scope_id: str, profile_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        o = await ops(request, scope_id)
        expected = _expected_revision(request)
        d = _parse_object(raw)
        only(d, {"display_name", "description"})
        p = await o.update_profile(
            scope_id,
            profile_id,
            expected_revision=expected,
            display_name=d.get("display_name", UNSET),
            description=d.get("description", UNSET),
        )
        return _json(dump(p), headers=etag(p.revision))

    for verb, method in (("disable", "disable_profile"), ("enable", "enable_profile"), ("delete", "delete_profile")):

        def _make_profile_transition(method: str = method) -> Any:
            async def handler(scope_id: str, profile_id: str, request: Request) -> Response:
                p = await getattr(await ops(request, scope_id), method)(scope_id, profile_id)
                return _json(dump(p), headers=etag(p.revision))

            return handler

        router.post(f"/v1/scopes/{{scope_id}}/profiles/{{profile_id}}/{verb}")(_make_profile_transition())

    @router.get("/v1/scopes/{scope_id}/profiles/{profile_id}/ranking")
    async def get_ranking(scope_id: str, profile_id: str, request: Request) -> Response:
        r = await (await ops(request, scope_id)).get_ranking_config(scope_id, profile_id)
        return _json(dump(r), headers=etag(r.revision))

    @router.patch("/v1/scopes/{scope_id}/profiles/{profile_id}/ranking")
    async def patch_ranking(scope_id: str, profile_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        o = await ops(request, scope_id)
        expected = _expected_revision(request)
        d = _parse_object(raw)
        only(d, {"mode", "eligibility_threshold", "tie_tolerance"})
        r = await o.update_ranking_config(
            scope_id,
            profile_id,
            expected_revision=expected,
            mode=d.get("mode", UNSET),
            eligibility_threshold=d.get("eligibility_threshold", UNSET),
            tie_tolerance=d.get("tie_tolerance", UNSET),
        )
        return _json(dump(r), headers=etag(r.revision))

    # categories and sensitive types share one route shape
    for segment, kind, id_field, extra in (
        ("categories", "category", "category_id", {"priority"}),
        ("sensitive-types", "sensitive_type", "type_id", set()),
    ):

        def _make_element_routes(
            segment: str = segment, kind: str = kind, id_field: str = id_field, extra: set[str] = extra
        ) -> None:
            base = f"/v1/scopes/{{scope_id}}/profiles/{{profile_id}}/{segment}"
            fields = {"name", "definition", "examples", "translations"} | extra

            async def create(scope_id: str, profile_id: str, request: Request) -> Response:
                raw = await _read_body(request, max_body)
                o = await ops(request, scope_id)
                d = _parse_object(raw)
                only(d, fields | {id_field})
                kwargs = {k: d.get(k) for k in ("examples", "translations") + tuple(extra)}
                res = await getattr(o, f"create_{kind}")(
                    scope_id, profile_id, d.get(id_field), d.get("name"), d.get("definition"), **kwargs
                )
                return _json(dump(res.value), 201 if res.created else 200, etag(res.value.revision))

            async def list_(
                scope_id: str,
                profile_id: str,
                request: Request,
                limit: int | None = None,
                cursor: str | None = None,
            ) -> Response:
                page = await getattr(
                    await ops(request, scope_id), f"list_{kind}s" if kind == "sensitive_type" else "list_categories"
                )(scope_id, profile_id, state=multi(request.query_params.getlist("state")), limit=limit, cursor=cursor)
                return _json(page_json(page))

            async def get_(
                scope_id: str, profile_id: str, element_id: str, request: Request, include_deleted: str | None = None
            ) -> Response:
                e = await getattr(await ops(request, scope_id), f"get_{kind}")(
                    scope_id, profile_id, element_id, include_deleted=flag(include_deleted)
                )
                return _json(dump(e), headers=etag(e.revision))

            async def patch(scope_id: str, profile_id: str, element_id: str, request: Request) -> Response:
                raw = await _read_body(request, max_body)
                o = await ops(request, scope_id)
                expected = _expected_revision(request)
                d = _parse_object(raw)
                only(d, fields)
                changes = {k: d.get(k, UNSET) for k in fields}
                e = await getattr(o, f"update_{kind}")(
                    scope_id, profile_id, element_id, expected_revision=expected, **changes
                )
                return _json(dump(e), headers=etag(e.revision))

            router.post(base)(create)
            router.get(base)(list_)
            router.get(base + "/{element_id}")(get_)
            router.patch(base + "/{element_id}")(patch)
            for verb in ("disable", "enable", "delete"):

                def _transition(verb: str = verb) -> Any:
                    async def handler(scope_id: str, profile_id: str, element_id: str, request: Request) -> Response:
                        e = await getattr(await ops(request, scope_id), f"{verb}_{kind}")(
                            scope_id, profile_id, element_id
                        )
                        return _json(dump(e), headers=etag(e.revision))

                    return handler

                router.post(base + "/{element_id}/" + verb)(_transition())

        _make_element_routes()

    @router.post("/v1/scopes/{scope_id}/profiles/{profile_id}/publish")
    async def publish(scope_id: str, profile_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        o = await ops(request, scope_id)
        d = _parse_object(raw)
        only(d, {"expected_draft_revision"})
        res = await o.publish_profile(scope_id, profile_id, expected_draft_revision=d.get("expected_draft_revision"))
        replay = None if res.created else {"Idempotent-Replay": "true"}
        return _json({"version": dump(res.value), "created": res.created}, 201 if res.created else 200, replay)

    @router.get("/v1/scopes/{scope_id}/profiles/{profile_id}/versions")
    async def list_versions(
        scope_id: str, profile_id: str, request: Request, limit: int | None = None, cursor: str | None = None
    ) -> Response:
        page = await (await ops(request, scope_id)).list_profile_versions(
            scope_id, profile_id, limit=limit, cursor=cursor
        )
        return _json(page_json(page))

    @router.get("/v1/scopes/{scope_id}/profiles/{profile_id}/versions/{version_number}")
    async def get_version(scope_id: str, profile_id: str, version_number: str, request: Request) -> Response:
        ver = await (await ops(request, scope_id)).get_profile_version(
            scope_id, profile_id, intp(version_number, "version_number")
        )
        return _json(dump(ver))

    @router.get("/v1/scopes/{scope_id}/default-profile")
    async def get_default(scope_id: str, request: Request) -> Response:
        d = await (await ops(request, scope_id)).get_default_profile(scope_id)
        return _json(dump(d), headers=etag(d.revision))

    @router.put("/v1/scopes/{scope_id}/default-profile")
    async def put_default(scope_id: str, request: Request) -> Response:
        raw = await _read_body(request, max_body)
        o = await ops(request, scope_id)
        expected = _expected_revision(request, low=0)
        d = _parse_object(raw)
        only(d, {"profile_id"})
        res = await o.set_default_profile(scope_id, d.get("profile_id"), expected_revision=expected)
        return _json(dump(res), headers=etag(res.revision))

    @router.delete("/v1/scopes/{scope_id}/default-profile")
    async def delete_default(scope_id: str, request: Request) -> Response:
        o = await ops(request, scope_id)
        res = await o.clear_default_profile(scope_id, expected_revision=_expected_revision(request, low=0))
        return _json(dump(res), headers=etag(res.revision))

    @router.get("/v1/scopes/{scope_id}/resolved-profile")
    async def resolved(
        scope_id: str, request: Request, profile_id: str | None = None, version: str | None = None
    ) -> Response:
        res = await (await ops(request, scope_id)).resolve_profile(
            scope_id,
            profile_id=profile_id,
            version_number=None if version is None else intp(version, "version"),
        )
        return _json({"selection": res.selection, "version": dump(res.version)})

    return router


def create_health_router(readiness: Callable[[], Coroutine[Any, Any, tuple[bool, str]]]) -> APIRouter:
    """``/livez`` never touches the database; ``/readyz`` reports coarse states only."""
    router = APIRouter()

    @router.get("/livez")
    async def livez() -> Response:
        return _json({"status": "alive"})

    @router.get("/readyz")
    async def readyz() -> Response:
        ready, state = await readiness()
        if ready:
            return _json({"status": "ready"})
        return _json({"status": "not_ready", "state": state}, 503)

    return router
