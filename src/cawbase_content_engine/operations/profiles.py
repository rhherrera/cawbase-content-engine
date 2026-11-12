"""Profiles, ranking configuration, the per-scope default and profile resolution (A02)."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from .. import validation as v
from ..config import Limits
from ..errors import (
    ConflictError,
    DefaultProfileNotSetError,
    InvalidStateTransitionError,
    LimitExceededError,
    ProfileDisabledError,
    ProfileInvalidError,
    ProfileIsDefaultError,
    ProfileNotPublishedError,
    ProfileNotSelectableError,
    ResourceNotFoundError,
    RevisionConflictError,
    ScopeDisabledError,
    ValidationFailedError,
)
from ..models import DefaultProfile, Page, Profile, RankingConfig, ResolvedProfile, UpsertResult
from ..persistence.tables import Tables
from .common import decode_cursor, encode_cursor, now
from .profile_common import (
    Actor,
    begin_profile_write,
    default_profile_id,
    enabled_category_priorities,
    event,
    lock_profile,
    profile_from_row,
    ranking_from_row,
    read_profile_row,
    require_scope_active,
    resolution_problems,
    visible_profile_row,
)
from .scopes import UNSET  # one "omitted field" sentinel for the whole package
from .versions import get_version_row, version_from_row


async def _is_default(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str) -> bool:
    return (await default_profile_id(conn, t, scope_id)) == profile_id


async def _profile(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str) -> Profile:
    row = await read_profile_row(conn, t, scope_id, profile_id)
    assert row is not None
    return profile_from_row(row, await _is_default(conn, t, scope_id, profile_id))


# ---- create / read / update ------------------------------------------------------------------
async def create_profile(
    conn: AsyncConnection,
    t: Tables,
    limits: Limits,
    actor: Actor,
    *,
    scope_id: str,
    profile_id: str,
    display_name: str,
    description: str | None,
    language: str,
    initial_catalog: str,
) -> UpsertResult[Profile]:
    from .. import catalog as starter  # lazy: the catalog is not loaded on import

    # Common lock of the scope: the limit check and the insert are serialized across profiles.
    await require_scope_active(conn, t, scope_id, lock=True)
    existing = await read_profile_row(conn, t, scope_id, profile_id)
    if existing is not None:  # a valid retry must not be blocked by the limits
        m = existing._mapping
        same = (
            m["status"] != "deleted"
            and m["display_name"] == display_name
            and m["description"] == description
            and m["language"] == language
            and (m["catalog_id"] is not None) == (initial_catalog == "starter")
        )
        if not same:
            raise ConflictError("The profile already exists with different attributes.")
        return UpsertResult(
            value=profile_from_row(existing, await _is_default(conn, t, scope_id, profile_id)), created=False
        )
    live = (
        await conn.execute(
            sa.select(sa.func.count()).where(t.profiles.c.scope_id == scope_id, t.profiles.c.status != "deleted")
        )
    ).scalar_one()
    total = (await conn.execute(sa.select(sa.func.count()).where(t.profiles.c.scope_id == scope_id))).scalar_one()
    if live >= limits.max_profiles_per_scope or total >= limits.max_profile_rows_per_scope:
        raise LimitExceededError("Too many profiles in the scope.", field="profile_id")
    ts = now()
    provenance: dict[str, Any] = dict(catalog_id=None, catalog_version=None, catalog_copied_at=None)
    if initial_catalog == "starter":
        provenance = dict(catalog_id=starter.CATALOG_ID, catalog_version=starter.CATALOG_VERSION, catalog_copied_at=ts)
    await conn.execute(
        sa.insert(t.profiles).values(
            scope_id=scope_id,
            profile_id=profile_id,
            display_name=display_name,
            description=description,
            language=language,
            status="active",
            deleted_at=None,
            revision=1,
            draft_revision=1,
            ranking_mode="ranking",
            eligibility_threshold=0.0,
            tie_tolerance=0.0,
            ranking_revision=1,
            current_version_number=None,
            created_at=ts,
            updated_at=ts,
            **provenance,
        )
    )
    if initial_catalog == "starter":
        other = "en" if language == "es" else "es"
        rows = []
        for entry in starter.starter_catalog().entries:
            ex = entry.examples[language]
            rows.append(
                dict(
                    scope_id=scope_id,
                    profile_id=profile_id,
                    type_id=entry.entry,
                    name=entry.names[language],
                    name_key=v.name_key(entry.names[language]),
                    definition=entry.definitions[language],
                    examples={"positive": list(ex.positive), "negative": list(ex.negative)},
                    translations={other: {"name": entry.names[other], "definition": entry.definitions[other]}},
                    origin_entry=entry.entry,
                    state="enabled",
                    deleted_at=None,
                    revision=1,
                    created_at=ts,
                    updated_at=ts,
                )
            )
        await conn.execute(sa.insert(t.sensitive_types), rows)
    await event(
        conn,
        t,
        actor,
        "profile.created",
        "profile",
        profile_id,
        scope_id,
        {"profile_id": profile_id, "initial_catalog": initial_catalog, "catalog_version": starter.CATALOG_VERSION}
        if initial_catalog == "starter"
        else {"profile_id": profile_id, "initial_catalog": initial_catalog},
    )
    return UpsertResult(value=await _profile(conn, t, scope_id, profile_id), created=True)


async def get_profile(
    conn: AsyncConnection, t: Tables, *, scope_id: str, profile_id: str, include_deleted: bool
) -> Profile:
    row = await read_profile_row(conn, t, scope_id, profile_id)
    if row is None or (row._mapping["status"] == "deleted" and not include_deleted):
        raise ResourceNotFoundError()
    return profile_from_row(row, await _is_default(conn, t, scope_id, profile_id))


async def list_profiles(
    conn: AsyncConnection, t: Tables, *, scope_id: str, statuses: tuple[str, ...], limit: int, cursor: str | None
) -> Page[Profile]:
    q = (
        sa.select(t.profiles)
        .where(t.profiles.c.scope_id == scope_id, t.profiles.c.status.in_(statuses))
        .order_by(t.profiles.c.profile_id)
        .limit(limit + 1)
    )
    after = decode_cursor(cursor)
    if after is not None:
        q = q.where(t.profiles.c.profile_id > after)
    rows = (await conn.execute(q)).all()
    default = await default_profile_id(conn, t, scope_id)
    items = [profile_from_row(r, r._mapping["profile_id"] == default) for r in rows[:limit]]
    return Page(items=items, next_cursor=encode_cursor(items[-1].profile_id) if len(rows) > limit else None)


async def update_profile(
    conn: AsyncConnection,
    t: Tables,
    actor: Actor,
    *,
    scope_id: str,
    profile_id: str,
    expected_revision: int,
    changes: dict[str, Any],
) -> Profile:
    row = await begin_profile_write(conn, t, scope_id, profile_id)
    m = row._mapping
    if m["revision"] != expected_revision:
        raise RevisionConflictError()
    effective = {k: val for k, val in changes.items() if m[k] != val}
    if not effective:
        return profile_from_row(row, await _is_default(conn, t, scope_id, profile_id))
    res = await conn.execute(
        sa.update(t.profiles)
        .where(
            t.profiles.c.scope_id == scope_id,
            t.profiles.c.profile_id == profile_id,
            t.profiles.c.revision == expected_revision,
        )
        .values(
            **effective,
            revision=t.profiles.c.revision + 1,
            draft_revision=t.profiles.c.draft_revision + 1,
            updated_at=now(),
        )
    )
    if res.rowcount != 1:
        raise RevisionConflictError()
    await event(
        conn,
        t,
        actor,
        "profile.updated",
        "profile",
        profile_id,
        scope_id,
        {
            "profile_id": profile_id,
            "changed_fields": sorted(effective),
            "revision": [expected_revision, expected_revision + 1],
            "draft_revision": [m["draft_revision"], m["draft_revision"] + 1],
        },
    )
    return await _profile(conn, t, scope_id, profile_id)


async def set_profile_status(
    conn: AsyncConnection, t: Tables, actor: Actor, *, scope_id: str, profile_id: str, target: str
) -> Profile:
    """Target-state, repeatable. ``deleted`` is terminal (logical; versions are kept)."""
    await require_scope_active(conn, t, scope_id)
    row = await lock_profile(conn, t, scope_id, profile_id)
    if row is None:
        raise ResourceNotFoundError()
    m = row._mapping
    if m["status"] == target:
        return profile_from_row(row, await _is_default(conn, t, scope_id, profile_id))
    if m["status"] == "deleted":
        raise InvalidStateTransitionError()
    if target in ("disabled", "deleted") and await _is_default(conn, t, scope_id, profile_id):
        raise ProfileIsDefaultError()
    ts = now()
    await conn.execute(
        sa.update(t.profiles)
        .where(t.profiles.c.scope_id == scope_id, t.profiles.c.profile_id == profile_id)
        .values(
            status=target,
            deleted_at=ts if target == "deleted" else None,
            revision=t.profiles.c.revision + 1,
            updated_at=ts,
        )
    )
    action = {"active": "profile.enabled", "disabled": "profile.disabled", "deleted": "profile.deleted"}[target]
    await event(
        conn,
        t,
        actor,
        action,
        "profile",
        profile_id,
        scope_id,
        {"profile_id": profile_id, "revision": [m["revision"], m["revision"] + 1]},
    )
    return await _profile(conn, t, scope_id, profile_id)


# ---- ranking / resolution configuration ------------------------------------------------------
async def get_ranking_config(conn: AsyncConnection, t: Tables, *, scope_id: str, profile_id: str) -> RankingConfig:
    row = await visible_profile_row(conn, t, scope_id, profile_id)
    return ranking_from_row(row._mapping)


async def update_ranking_config(
    conn: AsyncConnection,
    t: Tables,
    actor: Actor,
    *,
    scope_id: str,
    profile_id: str,
    expected_revision: int,
    changes: dict[str, Any],
) -> RankingConfig:
    row = await begin_profile_write(conn, t, scope_id, profile_id)
    m = row._mapping
    if m["ranking_revision"] != expected_revision:
        raise RevisionConflictError()
    columns = {
        "mode": "ranking_mode",
        "eligibility_threshold": "eligibility_threshold",
        "tie_tolerance": "tie_tolerance",
    }
    effective = {columns[k]: val for k, val in changes.items() if m[columns[k]] != val}
    if not effective:
        return ranking_from_row(m)
    if effective.get("ranking_mode") == "resolution":  # enabling resolution requires an unambiguous precedence
        problems = resolution_problems(await enabled_category_priorities(conn, t, scope_id, profile_id))
        if problems:
            raise ProfileInvalidError(problems)
    res = await conn.execute(
        sa.update(t.profiles)
        .where(
            t.profiles.c.scope_id == scope_id,
            t.profiles.c.profile_id == profile_id,
            t.profiles.c.ranking_revision == expected_revision,
        )
        .values(
            **effective,
            ranking_revision=t.profiles.c.ranking_revision + 1,
            draft_revision=t.profiles.c.draft_revision + 1,
            updated_at=now(),
        )
    )
    if res.rowcount != 1:
        raise RevisionConflictError()
    await event(
        conn,
        t,
        actor,
        "profile.ranking_updated",
        "profile",
        profile_id,
        scope_id,
        {
            "profile_id": profile_id,
            "changed_fields": sorted(k for k, c in columns.items() if c in effective),
            "ranking_revision": [expected_revision, expected_revision + 1],
            "draft_revision": [m["draft_revision"], m["draft_revision"] + 1],
        },
    )
    return ranking_from_row((await read_profile_row(conn, t, scope_id, profile_id))._mapping)


# ---- default profile --------------------------------------------------------------------------
async def _defaults_row(conn: AsyncConnection, t: Tables, scope_id: str) -> Any:
    return (
        await conn.execute(sa.select(t.scope_profile_defaults).where(t.scope_profile_defaults.c.scope_id == scope_id))
    ).first()


def _default_from_row(scope_id: str, row: Any) -> DefaultProfile:
    if row is None:
        return DefaultProfile(scope_id=scope_id, profile_id=None, revision=0, updated_at=None)
    m = row._mapping
    return DefaultProfile(
        scope_id=scope_id, profile_id=m["profile_id"], revision=m["revision"], updated_at=m["updated_at"]
    )


async def get_default_profile(conn: AsyncConnection, t: Tables, *, scope_id: str) -> DefaultProfile:
    return _default_from_row(scope_id, await _defaults_row(conn, t, scope_id))


async def set_default_profile(
    conn: AsyncConnection, t: Tables, actor: Actor, *, scope_id: str, profile_id: str, expected_revision: int
) -> DefaultProfile:
    # Lock order: scope row (serializes every designation, including the initial insert of a missing
    # row), then the chosen profile. Disabling/deleting locks only the profile and then reads the default.
    await require_scope_active(conn, t, scope_id, lock=True)
    prow = await lock_profile(conn, t, scope_id, profile_id)
    if prow is None:
        raise ResourceNotFoundError()
    pm = prow._mapping
    if pm["status"] != "active":
        raise ProfileNotSelectableError()
    if pm["current_version_number"] is None:
        raise ProfileNotPublishedError()
    row = await _defaults_row(conn, t, scope_id)
    current = row._mapping["revision"] if row is not None else 0
    if current != expected_revision:
        raise RevisionConflictError()
    if row is not None and row._mapping["profile_id"] == profile_id:
        return _default_from_row(scope_id, row)
    ts = now()
    if row is None:
        await conn.execute(
            sa.insert(t.scope_profile_defaults).values(
                scope_id=scope_id, profile_id=profile_id, revision=1, updated_at=ts
            )
        )
    else:
        res = await conn.execute(
            sa.update(t.scope_profile_defaults)
            .where(t.scope_profile_defaults.c.scope_id == scope_id, t.scope_profile_defaults.c.revision == current)
            .values(profile_id=profile_id, revision=current + 1, updated_at=ts)
        )
        if res.rowcount != 1:
            raise RevisionConflictError()
    await event(
        conn,
        t,
        actor,
        "profile.default_set",
        "default_profile",
        scope_id,
        scope_id,
        {"profile_id": profile_id, "revision": [current, current + 1]},
    )
    return _default_from_row(scope_id, await _defaults_row(conn, t, scope_id))


async def clear_default_profile(
    conn: AsyncConnection, t: Tables, actor: Actor, *, scope_id: str, expected_revision: int
) -> DefaultProfile:
    await require_scope_active(conn, t, scope_id, lock=True)
    row = await _defaults_row(conn, t, scope_id)
    current = row._mapping["revision"] if row is not None else 0
    if current != expected_revision:
        raise RevisionConflictError()
    if row is None or row._mapping["profile_id"] is None:
        return _default_from_row(scope_id, row)
    res = await conn.execute(
        sa.update(t.scope_profile_defaults)
        .where(t.scope_profile_defaults.c.scope_id == scope_id, t.scope_profile_defaults.c.revision == current)
        .values(profile_id=None, revision=current + 1, updated_at=now())
    )
    if res.rowcount != 1:
        raise RevisionConflictError()
    await event(
        conn,
        t,
        actor,
        "profile.default_cleared",
        "default_profile",
        scope_id,
        scope_id,
        {"previous_profile_id": row._mapping["profile_id"], "revision": [current, current + 1]},
    )
    return _default_from_row(scope_id, await _defaults_row(conn, t, scope_id))


# ---- resolution -------------------------------------------------------------------------------
async def resolve_profile(
    conn: AsyncConnection, t: Tables, *, scope_id: str, profile_id: str | None, version_number: int | None
) -> ResolvedProfile:
    """Pure read: returns the concrete published version a new execution should pin. Runs no analysis."""
    status = (
        await conn.execute(sa.select(t.scopes.c.status).where(t.scopes.c.scope_id == scope_id))
    ).scalar_one_or_none()
    if status is None:
        raise ResourceNotFoundError()
    if status != "active":
        raise ScopeDisabledError()
    if profile_id is None:
        if version_number is not None:
            raise ValidationFailedError("version_number needs a profile_id.", field="version_number")
        profile_id = await default_profile_id(conn, t, scope_id)
        if profile_id is None:
            raise DefaultProfileNotSetError()
        selection = "default"
    else:
        selection = "explicit"
    row = await read_profile_row(conn, t, scope_id, profile_id)
    if row is None:
        raise ResourceNotFoundError()
    m = row._mapping
    if m["status"] != "active":  # disabled / deleted: not selectable, not even with an explicit version
        raise ProfileNotSelectableError()
    number = version_number if version_number is not None else m["current_version_number"]
    if number is None:
        raise ProfileNotPublishedError()
    vrow = await get_version_row(conn, t, scope_id, profile_id, number)
    if vrow is None:
        raise ResourceNotFoundError()
    return ResolvedProfile(selection=selection, version=version_from_row(vrow))


__all__ = ["ProfileDisabledError", "UNSET"]
