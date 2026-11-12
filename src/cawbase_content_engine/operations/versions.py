"""Published versions: immutable, self-contained snapshots (A02, D-A02-02).

``content_hash`` is the SHA-256 of the canonical serialization of the *complete stored snapshot*
(integrity only). Whether there is anything new to publish is decided by comparing the *effective
configuration projection* (docs/profiles-and-catalog.md, 7.3.1), never by this hash.

The code only ever INSERTs and SELECTs ``profile_versions``: there is no UPDATE or DELETE.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import Limits
from ..errors import (
    LimitExceededError,
    ProfileInvalidError,
    ResourceNotFoundError,
    RevisionConflictError,
)
from ..models import Page, ProfileVersion, ProfileVersionSummary, UpsertResult
from ..persistence.tables import Tables
from .common import decode_cursor, encode_cursor, now
from .profile_common import (
    Actor,
    begin_profile_write,
    enabled_category_priorities,
    event,
    read_profile_row,
    resolution_problems,
)

SNAPSHOT_SCHEMA_VERSION = 1


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def content_hash(snapshot: dict[str, Any]) -> bytes:
    return hashlib.sha256(canonical(snapshot)).digest()


def effective_projection(snapshot: dict[str, Any]) -> bytes:
    """Functional configuration only: no revisions, timestamps, actors or provenance (7.3.1)."""
    prof = snapshot["profile"]
    rk = snapshot["ranking"]
    projection = {
        "profile": {k: prof[k] for k in ("profile_id", "display_name", "description", "language")},
        "ranking": {
            "mode": rk["mode"],
            "eligibility_threshold": float(rk["eligibility_threshold"]),
            "tie_tolerance": float(rk["tie_tolerance"]),
        },
        "capabilities": snapshot["capabilities"],
        "categories": [
            {k: c[k] for k in ("category_id", "name", "definition", "examples", "translations", "priority")}
            for c in snapshot["categories"]
        ],
        "sensitive_types": [
            {k: s[k] for k in ("type_id", "name", "definition", "examples", "translations")}
            for s in snapshot["sensitive_types"]
        ],
    }
    return canonical(projection)


async def build_snapshot(conn: AsyncConnection, t: Tables, profile_row: Any) -> dict[str, Any]:
    """Consistent read of the enabled elements; the caller holds the profile lock."""
    m = profile_row._mapping
    scope_id, profile_id = m["scope_id"], m["profile_id"]
    cats = (
        await conn.execute(
            sa.select(t.categories)
            .where(
                t.categories.c.scope_id == scope_id,
                t.categories.c.profile_id == profile_id,
                t.categories.c.state == "enabled",
            )
            .order_by(t.categories.c.category_id)
        )
    ).all()
    types = (
        await conn.execute(
            sa.select(t.sensitive_types)
            .where(
                t.sensitive_types.c.scope_id == scope_id,
                t.sensitive_types.c.profile_id == profile_id,
                t.sensitive_types.c.state == "enabled",
            )
            .order_by(t.sensitive_types.c.type_id)
        )
    ).all()

    def element(row: Any, id_col: str, extra: str) -> dict[str, Any]:
        e = row._mapping
        return {
            id_col: e[id_col],
            "name": e["name"],
            "definition": e["definition"],
            "examples": {
                "positive": list(e["examples"].get("positive", [])),
                "negative": list(e["examples"].get("negative", [])),
            },
            "translations": e["translations"] or None,
            extra: e[extra],
            "source_revision": e["revision"],
        }

    provenance = None
    if m["catalog_id"] is not None:
        copied = m["catalog_copied_at"]
        provenance = {
            "catalog_id": m["catalog_id"],
            "catalog_version": m["catalog_version"],
            "copied_at": copied.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        }
    return {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "profile": {
            "profile_id": profile_id,
            "display_name": m["display_name"],
            "description": m["description"],
            "language": m["language"],
        },
        "catalog_provenance": provenance,
        "ranking": {
            "mode": m["ranking_mode"],
            "eligibility_threshold": float(m["eligibility_threshold"]),
            "tie_tolerance": float(m["tie_tolerance"]),
        },
        "capabilities": {"classification": bool(cats), "detection": bool(types)},
        "categories": [element(r, "category_id", "priority") for r in cats],
        "sensitive_types": [element(r, "type_id", "origin_entry") for r in types],
    }


def summary_from_row(row: Any) -> ProfileVersionSummary:
    m = row._mapping
    return ProfileVersionSummary(
        scope_id=m["scope_id"],
        profile_id=m["profile_id"],
        version_id=m["version_id"],
        version_number=m["version_number"],
        source_draft_revision=m["source_draft_revision"],
        content_hash=bytes(m["content_hash"]).hex(),
        capabilities=dict(m["capabilities"]),
        snapshot_bytes=m["snapshot_bytes"],
        published_at=m["published_at"],
        published_by=m["published_by"],
    )


def version_from_row(row: Any) -> ProfileVersion:
    return ProfileVersion(**summary_from_row(row).model_dump(), snapshot=row._mapping["snapshot"])


async def get_version_row(conn: AsyncConnection, t: Tables, scope_id: str, profile_id: str, number: int) -> Any:
    v = t.profile_versions
    return (
        await conn.execute(
            sa.select(v).where(v.c.scope_id == scope_id, v.c.profile_id == profile_id, v.c.version_number == number)
        )
    ).first()


async def get_profile_version(
    conn: AsyncConnection, t: Tables, *, scope_id: str, profile_id: str, version_number: int
) -> ProfileVersion:
    # Historical versions stay readable for disabled and deleted profiles.
    if await read_profile_row(conn, t, scope_id, profile_id) is None:
        raise ResourceNotFoundError()
    row = await get_version_row(conn, t, scope_id, profile_id, version_number)
    if row is None:
        raise ResourceNotFoundError()
    return version_from_row(row)


async def list_profile_versions(
    conn: AsyncConnection, t: Tables, *, scope_id: str, profile_id: str, limit: int, cursor: str | None
) -> Page[ProfileVersionSummary]:
    if await read_profile_row(conn, t, scope_id, profile_id) is None:
        raise ResourceNotFoundError()
    v = t.profile_versions
    cols = [c for c in v.c if c.name != "snapshot"]
    q = (
        sa.select(*cols)
        .where(v.c.scope_id == scope_id, v.c.profile_id == profile_id)
        .order_by(v.c.version_number.desc())
        .limit(limit + 1)
    )
    after = decode_cursor(cursor)
    if after is not None:
        if not after.isdigit():
            from ..errors import ValidationFailedError

            raise ValidationFailedError("Invalid cursor.", field="cursor")
        q = q.where(v.c.version_number < int(after))
    rows = (await conn.execute(q)).all()
    items = [summary_from_row(r) for r in rows[:limit]]
    return Page(items=items, next_cursor=encode_cursor(str(items[-1].version_number)) if len(rows) > limit else None)


async def publish_profile(
    conn: AsyncConnection,
    t: Tables,
    limits: Limits,
    actor: Actor,
    *,
    scope_id: str,
    profile_id: str,
    expected_draft_revision: int,
) -> UpsertResult[ProfileVersion]:
    # 1. lock + state + strict revision, on current data (a stale revision is a 412 even if nothing changed)
    row = await begin_profile_write(conn, t, scope_id, profile_id)
    m = row._mapping
    if m["draft_revision"] != expected_draft_revision:
        raise RevisionConflictError()
    snapshot = await build_snapshot(conn, t, row)
    # 2. same effective configuration as the current version => return it untouched
    current = m["current_version_number"]
    if current is not None:
        cur_row = await get_version_row(conn, t, scope_id, profile_id, current)
        if cur_row is not None and effective_projection(cur_row._mapping["snapshot"]) == effective_projection(snapshot):
            return UpsertResult(value=version_from_row(cur_row), created=False)
    # 3. validate, enforce limits, publish a new complete snapshot
    problems: list[dict[str, str]] = []
    if not snapshot["categories"] and not snapshot["sensitive_types"]:
        problems.append({"code": "no_enabled_elements"})
    if snapshot["ranking"]["mode"] == "resolution":
        problems += resolution_problems(await enabled_category_priorities(conn, t, scope_id, profile_id))
    if problems:
        raise ProfileInvalidError(problems)
    size = len(canonical(snapshot))
    if size > limits.max_snapshot_bytes:
        raise LimitExceededError("The snapshot is too large.", field="snapshot")
    number = (current or 0) + 1
    if number > limits.max_versions_per_profile:
        raise LimitExceededError("Too many versions in the profile.", field="version")
    published_by = actor.label
    ts = now()
    await conn.execute(
        sa.insert(t.profile_versions).values(
            scope_id=scope_id,
            profile_id=profile_id,
            version_number=number,
            version_id="pvr_" + uuid.uuid4().hex,
            source_draft_revision=expected_draft_revision,
            snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot=snapshot,
            content_hash=content_hash(snapshot),
            snapshot_bytes=size,
            capabilities=snapshot["capabilities"],
            published_at=ts,
            published_by=published_by,
        )
    )
    await conn.execute(
        sa.update(t.profiles)
        .where(t.profiles.c.scope_id == scope_id, t.profiles.c.profile_id == profile_id)
        .values(current_version_number=number)
    )
    await event(
        conn,
        t,
        actor,
        "profile.published",
        "profile_version",
        f"{profile_id}/{number}",
        scope_id,
        {"profile_id": profile_id, "version_number": number, "source_draft_revision": expected_draft_revision},
    )
    created = await get_version_row(conn, t, scope_id, profile_id, number)
    return UpsertResult(value=version_from_row(created), created=True)
