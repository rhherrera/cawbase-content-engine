"""Shared pieces of the A02 operations: locks, row mapping and event helpers.

Lock order (docs/persistence-and-migrations.md, 7.4.1): scope row -> profile row(s, ascending id) ->
child rows only through ``UPDATE ... WHERE revision = ?``. PostgreSQL uses ``SELECT ... FOR UPDATE`` /
``FOR NO KEY UPDATE`` (never a dummy UPDATE); SQLite ignores ``FOR UPDATE`` and relies on the write
transaction A01 already opens (own mode: ``BEGIN IMMEDIATE``; host mode: the host's transaction).
Acquiring a lock never modifies the resource: all checks run afterwards on current data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    ProfileDisabledError,
    ResourceNotAccessibleError,
    ResourceNotFoundError,
    ScopeDisabledError,
)
from ..models import CatalogProvenance, Category, Examples, Profile, RankingConfig, SensitiveType, Translation
from ..persistence.tables import Tables
from .common import add_event, now


@dataclass(frozen=True)
class Actor:
    key_id: str | None
    label: str | None


async def event(
    conn: AsyncConnection,
    t: Tables,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: str,
    scope_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Ids, field names and revisions only; never names, definitions, examples or snapshots."""
    await add_event(
        conn,
        t,
        action=action,
        target_type=target_type,
        target_id=target_id,
        actor_key_id=actor.key_id,
        actor_label=actor.label,
        scope_id=scope_id,
        detail=detail,
    )


async def require_scope_active(conn: AsyncConnection, t: Tables, scope_id: str, *, lock: bool = False) -> None:
    q = sa.select(t.scopes.c.status).where(t.scopes.c.scope_id == scope_id)
    if lock:
        q = q.with_for_update(key_share=True)  # PostgreSQL: FOR NO KEY UPDATE
    status = (await conn.execute(q)).scalar_one_or_none()
    if status is None:
        raise ResourceNotAccessibleError()
    if status != "active":
        raise ScopeDisabledError()


async def lock_profile(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str) -> Any:
    q = sa.select(t.profiles).where(t.profiles.c.scope_id == scope_id, t.profiles.c.profile_id == profile_id)
    return (await conn.execute(q.with_for_update())).first()


async def begin_profile_write(
    conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str, *, require_active: bool = True
) -> Any:
    """Scope active -> lock the profile -> check its state on current data."""
    await require_scope_active(conn, t, scope_id)
    row = await lock_profile(conn, t, scope_id, profile_id)
    if row is None or row._mapping["status"] == "deleted":
        raise ResourceNotFoundError()
    if require_active and row._mapping["status"] != "active":
        raise ProfileDisabledError()
    return row


async def read_profile_row(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str) -> Any:
    q = sa.select(t.profiles).where(t.profiles.c.scope_id == scope_id, t.profiles.c.profile_id == profile_id)
    return (await conn.execute(q)).first()


async def visible_profile_row(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str) -> Any:
    """Read path: a deleted profile behaves as non-existent."""
    row = await read_profile_row(conn, t, scope_id, profile_id)
    if row is None or row._mapping["status"] == "deleted":
        raise ResourceNotFoundError()
    return row


async def bump_draft(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str) -> None:
    """Called only when the editable set really changed (a lock alone never bumps anything)."""
    await conn.execute(
        sa.update(t.profiles)
        .where(t.profiles.c.scope_id == scope_id, t.profiles.c.profile_id == profile_id)
        .values(draft_revision=t.profiles.c.draft_revision + 1, updated_at=now())
    )


def ranking_from_row(m: Any) -> RankingConfig:
    return RankingConfig(
        mode=m["ranking_mode"],
        eligibility_threshold=float(m["eligibility_threshold"]),
        tie_tolerance=float(m["tie_tolerance"]),
        revision=m["ranking_revision"],
    )


def profile_from_row(row: Any, is_default: bool) -> Profile:
    m = row._mapping
    provenance = None
    if m["catalog_id"] is not None:
        provenance = CatalogProvenance(
            catalog_id=m["catalog_id"], catalog_version=m["catalog_version"], copied_at=m["catalog_copied_at"]
        )
    return Profile(
        scope_id=m["scope_id"],
        profile_id=m["profile_id"],
        display_name=m["display_name"],
        description=m["description"],
        language=m["language"],
        status=m["status"],
        revision=m["revision"],
        draft_revision=m["draft_revision"],
        current_version_number=m["current_version_number"],
        is_default=is_default,
        ranking=ranking_from_row(m),
        catalog_provenance=provenance,
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        deleted_at=m["deleted_at"],
    )


async def default_profile_id(conn: AsyncConnection, t: Tables, scope_id: str) -> str | None:
    q = sa.select(t.scope_profile_defaults.c.profile_id).where(t.scope_profile_defaults.c.scope_id == scope_id)
    return (await conn.execute(q)).scalar_one_or_none()


def _examples(raw: Any) -> Examples:
    return Examples(positive=tuple(raw.get("positive", ())), negative=tuple(raw.get("negative", ())))


def _translations(raw: Any) -> dict[str, Translation] | None:
    if not raw:
        return None
    return {lang: Translation(**entry) for lang, entry in raw.items()}


def category_from_row(row: Any) -> Category:
    m = row._mapping
    return Category(
        scope_id=m["scope_id"],
        profile_id=m["profile_id"],
        category_id=m["category_id"],
        name=m["name"],
        definition=m["definition"],
        examples=_examples(m["examples"]),
        translations=_translations(m["translations"]),
        priority=m["priority"],
        state=m["state"],
        revision=m["revision"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        deleted_at=m["deleted_at"],
    )


def type_from_row(row: Any) -> SensitiveType:
    m = row._mapping
    return SensitiveType(
        scope_id=m["scope_id"],
        profile_id=m["profile_id"],
        type_id=m["type_id"],
        name=m["name"],
        definition=m["definition"],
        examples=_examples(m["examples"]),
        translations=_translations(m["translations"]),
        origin_entry=m["origin_entry"],
        state=m["state"],
        revision=m["revision"],
        created_at=m["created_at"],
        updated_at=m["updated_at"],
        deleted_at=m["deleted_at"],
    )


async def enabled_category_priorities(
    conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str
) -> list[tuple[str, int | None]]:
    q = (
        sa.select(t.categories.c.category_id, t.categories.c.priority)
        .where(
            t.categories.c.scope_id == scope_id,
            t.categories.c.profile_id == profile_id,
            t.categories.c.state == "enabled",
        )
        .order_by(t.categories.c.category_id)
    )
    return [(r[0], r[1]) for r in (await conn.execute(q)).all()]


def resolution_problems(categories: list[tuple[str, int | None]]) -> list[dict[str, str]]:
    """Resolution is valid iff there is >= 1 enabled category, all with a unique priority (codes and ids only)."""
    if not categories:
        return [{"code": "resolution_requires_categories"}]
    problems: list[dict[str, str]] = [
        {"code": "priority_missing", "category_id": cid} for cid, prio in categories if prio is None
    ]
    seen: dict[int, str] = {}
    for cid, prio in categories:
        if prio is None:
            continue
        if prio in seen:
            problems.append({"code": "priority_duplicate", "category_id": cid})
        seen[prio] = cid
    return problems
