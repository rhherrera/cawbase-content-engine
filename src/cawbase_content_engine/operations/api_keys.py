from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection

from .. import auth
from ..config import Limits
from ..contexts import AuthenticatedPrincipal
from ..errors import (
    AuthenticationError,
    ConflictError,
    IdempotencyConflictError,
    InternalError,
    LimitExceededError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationFailedError,
)
from ..models import ApiKeyGrants, ApiKeyInfo, IssuedApiKey
from ..permissions import AdminPermission, ScopePermission
from ..persistence.tables import Tables
from .clients import get_client_row
from .common import add_event, insert_ignore, now


def fingerprint(client_id: str, label: str, expires_at: datetime | None, grants: ApiKeyGrants) -> bytes:
    canonical = json.dumps(
        {
            "c": client_id,
            "l": label,
            "e": expires_at.isoformat() if expires_at else None,
            "a": sorted(p.value for p in grants.admin),
            "s": sorted([sid, sorted(p.value for p in perms)] for sid, perms in grants.scopes.items()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).digest()


def normalize_grants(admin: Iterable[Any], scopes: Mapping[str, Iterable[Any]], limits: Limits) -> ApiKeyGrants:
    try:
        admin_set = frozenset(AdminPermission(p) for p in admin)
        scope_map = {sid: frozenset(ScopePermission(p) for p in perms) for sid, perms in scopes.items()}
    except ValueError:
        raise ValidationFailedError("Unknown permission in grants.", field="grants") from None
    scope_map = {sid: perms for sid, perms in scope_map.items() if perms}
    if not admin_set and not scope_map:
        raise ValidationFailedError("A key needs at least one grant.", field="grants")
    if sum(len(p) for p in scope_map.values()) > limits.max_scope_grants_per_key:
        raise LimitExceededError("Too many scope grants.", field="grants")
    return ApiKeyGrants(admin=admin_set, scopes=scope_map)


async def check_delegation(
    conn: AsyncConnection,
    t: Tables,
    *,
    issuer_admin: frozenset[AdminPermission],
    issuer_scopes: Mapping[str, frozenset[ScopePermission]],
    grants: ApiKeyGrants,
) -> None:
    """D-27. Admin grants: always a subset of the issuer's. Scope grants: subset unless
    ``admin:grants:write``, in which case any *existing* scope may be granted."""
    if not grants.admin <= issuer_admin:
        raise PermissionDeniedError()
    if AdminPermission.ADMIN_GRANTS_WRITE in issuer_admin:
        wanted = set(grants.scopes)
        if wanted:
            found = set(
                (await conn.execute(sa.select(t.scopes.c.scope_id).where(t.scopes.c.scope_id.in_(wanted)))).scalars()
            )
            if found != wanted:
                raise ValidationFailedError("Unknown scope in grants.", field="grants")
        return
    for sid, perms in grants.scopes.items():
        if not perms <= issuer_scopes.get(sid, frozenset()):
            raise PermissionDeniedError()


async def load_grants(conn: AsyncConnection, t: Tables, key_ids: list[str]) -> dict[str, ApiKeyGrants]:
    if not key_ids:
        return {}
    admin: dict[str, set[AdminPermission]] = defaultdict(set)
    scopes: dict[str, dict[str, set[ScopePermission]]] = defaultdict(lambda: defaultdict(set))
    for r in (
        await conn.execute(
            sa.select(t.api_key_admin_permissions).where(t.api_key_admin_permissions.c.key_id.in_(key_ids))
        )
    ).all():
        admin[r.key_id].add(AdminPermission(r.permission))
    for r in (
        await conn.execute(sa.select(t.api_key_scope_grants).where(t.api_key_scope_grants.c.key_id.in_(key_ids)))
    ).all():
        scopes[r.key_id][r.scope_id].add(ScopePermission(r.permission))
    return {
        k: ApiKeyGrants(admin=frozenset(admin[k]), scopes={s: frozenset(p) for s, p in scopes[k].items()})
        for k in key_ids
    }


def info_from_row(row: Any, grants: ApiKeyGrants) -> ApiKeyInfo:
    m = row._mapping
    return ApiKeyInfo(
        key_id=m["key_id"],
        client_id=m["client_id"],
        label=m["label"],
        grants=grants,
        created_at=m["created_at"],
        expires_at=m["expires_at"],
        revoked_at=m["revoked_at"],
    )


async def _info(conn: AsyncConnection, t: Tables, key_id: str) -> ApiKeyInfo:
    row = (await conn.execute(sa.select(t.api_keys).where(t.api_keys.c.key_id == key_id))).first()
    if row is None:
        raise ResourceNotFoundError()
    return info_from_row(row, (await load_grants(conn, t, [key_id]))[key_id])


async def issue_key(
    conn: AsyncConnection,
    t: Tables,
    limits: Limits,
    *,
    client_id: str,
    request_id: str,
    label: str,
    expires_at: datetime | None,
    grants: ApiKeyGrants,
    created_by_key_id: str | None,
    actor_label: str | None,
    event_action: str = "api_key.issued",
    skip_active_limit: bool = False,
    extra_detail: dict[str, Any] | None = None,
) -> IssuedApiKey:
    """Idempotent on (client_id, request_id): a repeat never creates another credential and never
    returns the secret again."""
    client = await get_client_row(conn, t, client_id)
    if client is None:
        raise ResourceNotFoundError()
    fp = fingerprint(client_id, label, expires_at, grants)
    existing = (
        await conn.execute(
            sa.select(t.api_keys).where(
                t.api_keys.c.client_id == client_id, t.api_keys.c.issuance_request_id == request_id
            )
        )
    ).first()
    if existing is not None:
        return await _replay(conn, t, existing, fp)
    if client._mapping["status"] != "active":
        raise ConflictError("The client is disabled.")
    if not skip_active_limit:
        ts = now()
        active = (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(t.api_keys)
                .where(
                    t.api_keys.c.client_id == client_id,
                    t.api_keys.c.revoked_at.is_(None),
                    sa.or_(t.api_keys.c.expires_at.is_(None), t.api_keys.c.expires_at > ts),
                )
            )
        ).scalar_one()
        if active >= limits.max_active_keys_per_client:
            raise LimitExceededError("Too many active keys for this client.")
    for _ in range(3):
        key = auth.generate_key()
        inserted = await insert_ignore(
            conn,
            t,
            t.api_keys,
            dict(
                key_id=key.key_id,
                client_id=client_id,
                secret_hash=key.secret_hash,
                hash_algorithm=auth.HASH_ALGORITHM,
                label=label,
                issuance_request_id=request_id,
                issuance_fingerprint=fp,
                expires_at=expires_at,
                revoked_at=None,
                created_at=now(),
                created_by_key_id=created_by_key_id,
            ),
        )
        if inserted:
            break
        existing = (
            await conn.execute(  # lost a race on (client_id, request_id)
                sa.select(t.api_keys).where(
                    t.api_keys.c.client_id == client_id, t.api_keys.c.issuance_request_id == request_id
                )
            )
        ).first()
        if existing is not None:
            return await _replay(conn, t, existing, fp)
    else:
        raise InternalError()
    for p in grants.admin:
        await conn.execute(sa.insert(t.api_key_admin_permissions).values(key_id=key.key_id, permission=p.value))
    for sid, perms in grants.scopes.items():
        for p2 in perms:
            await conn.execute(
                sa.insert(t.api_key_scope_grants).values(key_id=key.key_id, scope_id=sid, permission=p2.value)
            )
    await add_event(
        conn,
        t,
        action=event_action,
        target_type="api_key",
        target_id=key.key_id,
        actor_key_id=created_by_key_id,
        actor_label=actor_label,
        detail={
            "client_id": client_id,
            "admin_permissions": len(grants.admin),
            "scope_grants": sum(len(p) for p in grants.scopes.values()),
            **(extra_detail or {}),
        },
    )
    return IssuedApiKey(info=await _info(conn, t, key.key_id), secret=SecretStr(key.full_key))


async def _replay(conn: AsyncConnection, t: Tables, existing: Any, fp: bytes) -> IssuedApiKey:
    if bytes(existing._mapping["issuance_fingerprint"]) != fp:
        raise IdempotencyConflictError()
    return IssuedApiKey(info=await _info(conn, t, existing._mapping["key_id"]), secret=None, replayed=True)


async def list_keys(conn: AsyncConnection, t: Tables, client_id: str) -> list[ApiKeyInfo]:
    if await get_client_row(conn, t, client_id) is None:
        raise ResourceNotFoundError()
    rows = (
        await conn.execute(
            sa.select(t.api_keys)
            .where(t.api_keys.c.client_id == client_id)
            .order_by(t.api_keys.c.created_at, t.api_keys.c.key_id)
        )
    ).all()
    grants = await load_grants(conn, t, [r._mapping["key_id"] for r in rows])
    return [info_from_row(r, grants[r._mapping["key_id"]]) for r in rows]


async def revoke_key(
    conn: AsyncConnection, t: Tables, *, key_id: str, actor_key_id: str | None, actor_label: str | None
) -> ApiKeyInfo:
    row = (await conn.execute(sa.select(t.api_keys).where(t.api_keys.c.key_id == key_id))).first()
    if row is None:
        raise ResourceNotFoundError()
    if row._mapping["revoked_at"] is None:
        res = await conn.execute(
            sa.update(t.api_keys)
            .where(t.api_keys.c.key_id == key_id, t.api_keys.c.revoked_at.is_(None))
            .values(revoked_at=now())
        )
        if res.rowcount == 1:
            await add_event(
                conn,
                t,
                action="api_key.revoked",
                target_type="api_key",
                target_id=key_id,
                actor_key_id=actor_key_id,
                actor_label=actor_label,
                detail={"client_id": row._mapping["client_id"]},
            )
    return await _info(conn, t, key_id)


async def authenticate(conn: AsyncConnection, t: Tables, raw: str, token: Any) -> AuthenticatedPrincipal:
    """Every failure mode raises the same generic AuthenticationError."""
    parsed = auth.parse_key(raw)
    row = None
    if parsed is not None:
        row = (
            await conn.execute(
                sa.select(t.api_keys, t.clients.c.status.label("client_status"))
                .join(t.clients, t.clients.c.client_id == t.api_keys.c.client_id)
                .where(t.api_keys.c.key_id == parsed.key_id)
            )
        ).first()
    presented = parsed.secret_hash if parsed is not None else bytes(32)
    stored = bytes(row._mapping["secret_hash"]) if row is not None else None
    ok = auth.verify(stored, presented) and parsed is not None and row is not None
    if not ok:
        raise AuthenticationError()
    assert row is not None
    m = row._mapping
    expires = m["expires_at"]
    if m["revoked_at"] is not None or m["client_status"] != "active" or (expires is not None and expires <= now()):
        raise AuthenticationError()
    grants = (await load_grants(conn, t, [m["key_id"]]))[m["key_id"]]
    return AuthenticatedPrincipal(
        key_id=m["key_id"],
        client_id=m["client_id"],
        admin_permissions=grants.admin,
        scope_grants=dict(grants.scopes),
        _token=token,
    )


def new_request_id() -> str:
    return str(uuid.uuid4())
