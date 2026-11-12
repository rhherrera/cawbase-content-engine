"""Shared selection of the published profile version a new execution should pin (A02 section 8).

Internal and *authorization-free*: every public operation that uses it checks its own permission
first (``resolve_profile``: ``profiles:read``; ``analyze_text``: ``analysis:execute``). It is a
sequence of successive reads, not an atomic snapshot (see docs/inference-and-analysis.md, 3.2).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    DefaultProfileNotSetError,
    ProfileNotPublishedError,
    ProfileNotSelectableError,
    ResourceNotFoundError,
    ScopeDisabledError,
    ValidationFailedError,
)
from ..models import ResolvedProfile
from ..persistence.tables import Tables
from .profile_common import default_profile_id, read_profile_row
from .versions import get_version_row, version_from_row


async def select_published_version(
    conn: AsyncConnection, t: Tables, *, scope_id: str, profile_id: str | None, version_number: int | None
) -> ResolvedProfile:
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
