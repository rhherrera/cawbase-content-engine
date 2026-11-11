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
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message, "request_id": request_id}},
        status_code=exc.http_status or 500,
        headers=headers,
    )


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
    try:
        data = json.loads(raw or b"{}")
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


def _expected_revision(request: Request) -> int:
    header = request.headers.get("if-match")
    if header is None:
        raise PreconditionRequiredError("The If-Match header is required.", field="If-Match")
    raw = header.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    if not raw.isdigit() or int(raw) < 1:
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
