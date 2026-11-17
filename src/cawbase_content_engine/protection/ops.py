"""Decision transactions of A06: contexts, protection and restoration (docs/text-protection.md, sections 5-11).

Lock order (docs 11.2, extends A05): 1 scopes, 8 jobs, 11 protection_scope_usage, 12 protection_contexts,
13 protection_mappings (by token). Every hour that decides is read from the database AFTER the locks that can make
the transaction wait. Nothing is returned before COMMIT: the caller gets the text only when ``_run`` returns.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    ContextKindMismatchError,
    ContextUnavailableError,
    DatabaseUnavailableError,
    EngineError,
    LimitExceededError,
    MappingIntegrityError,
    MarkerCollisionError,
    MarkerUnknownError,
    QuotaExceededError,
    ResourceNotFoundError,
    ScopeDisabledError,
)
from ..jobs import db as jdb
from ..operations.common import decode_cursor, encode_cursor, insert_ignore
from . import crypto
from .grouping import Group
from .markers import REDACTED, marker_of, new_token, scan, token_of
from .sources import JobBinding, lock_and_recheck_job
from .types import (
    ContextInfo,
    Kind,
    ProtectionSetup,
)

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.protection")


class _UniqueRace(EngineError):
    """Internal control flow (never reaches a caller): the equality-index uniqueness fired unexpectedly."""

    code = "internal_error"
    http_status = None


def info_from_row(row: Any, now: datetime) -> ContextInfo:
    """``state`` is EFFECTIVE: an active context whose time has come is already expired, cleaned or not."""
    state = row.state
    if state == "active" and now >= row.expires_at:
        state = "expired"
    return ContextInfo(
        scope_id=row.scope_id,
        context_id=row.context_id,
        kind=row.kind,
        state=state,
        created_at=row.created_at,
        expires_at=row.expires_at,
        deleted_at=row.deleted_at,
        mapping_count=int(row.mapping_count),
    )


def _clock(engine: ContentEngine) -> Any:
    async def now(conn: AsyncConnection) -> datetime:
        return await jdb.db_now(conn, engine.backend)

    return now


async def _ensure_usage(conn: AsyncConnection, engine: ContentEngine, scope_id: str, now: datetime) -> Any:
    t = engine.tables
    await insert_ignore(
        conn, t, t.protection_scope_usage, {"scope_id": scope_id, "active_contexts": 0, "updated_at": now}
    )
    return (
        await conn.execute(
            sa.select(t.protection_scope_usage)
            .where(t.protection_scope_usage.c.scope_id == scope_id)
            .with_for_update()  # rank 11
        )
    ).one()


async def _active_scope(conn: AsyncConnection, engine: ContentEngine, scope_id: str) -> None:
    scope = await jdb.lock_scope(conn, engine.tables, scope_id, exclusive=False)  # rank 1, FOR SHARE
    if scope.status != "active":
        raise ScopeDisabledError()


def _check_usable(row: Any, kind: str, now: datetime) -> None:
    if row is None:
        raise ResourceNotFoundError()  # another scope's context is indistinguishable from a missing one
    if row.state != "active" or now >= row.expires_at:
        raise ContextUnavailableError()
    if row.kind != kind:
        raise ContextKindMismatchError()


# ---- contexts -------------------------------------------------------------------------------------------
async def create_context(
    engine: ContentEngine, setup: ProtectionSetup, scope_id: str, kind: Kind, ttl_s: int
) -> ContextInfo:
    t = engine.tables
    now_of = _clock(engine)

    async def fn(conn: AsyncConnection) -> ContextInfo:
        await _active_scope(conn, engine, scope_id)
        t0 = await now_of(conn)
        usage = await _ensure_usage(conn, engine, scope_id, t0)
        now = await now_of(conn)  # after the lock waits
        live = (
            await conn.execute(
                sa.select(sa.func.count()).where(
                    t.protection_contexts.c.scope_id == scope_id,
                    t.protection_contexts.c.state == "active",
                    t.protection_contexts.c.expires_at > now,
                )
            )
        ).scalar_one()
        if int(live) >= setup.limits.max_active_contexts_per_scope:
            raise QuotaExceededError("Too many active contexts in this scope.")
        version, master = crypto.active_version(setup.keys)
        context_id = "ctx_" + secrets.token_hex(16)
        wrapped = crypto.wrap_index_key(
            master, scope_id=scope_id, context_id=context_id, version=version, raw=crypto.new_index_key()
        )
        await conn.execute(
            sa.insert(t.protection_contexts).values(
                scope_id=scope_id,
                context_id=context_id,
                kind=kind,
                state="active",
                ttl_s=ttl_s,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_s),
                index_key_version=version,
                index_key_wrapped=wrapped,
                mapping_count=0,
                mapping_bytes=0,
                updated_at=now,
            )
        )
        await conn.execute(
            sa.update(t.protection_scope_usage)
            .where(t.protection_scope_usage.c.scope_id == scope_id)
            .values(active_contexts=int(usage.active_contexts) + 1, updated_at=now)
        )
        row = await _context_row(conn, engine, scope_id, context_id)
        return info_from_row(row, now)

    return await engine._run(fn, write=True, tx=None)


async def _context_row(
    conn: AsyncConnection, engine: ContentEngine, scope_id: str, context_id: str, lock: Any = None
) -> Any:
    c = engine.tables.protection_contexts
    q = sa.select(c).where(c.c.scope_id == scope_id, c.c.context_id == context_id)
    if lock == "update":
        q = q.with_for_update()
    elif lock == "no_key_update":
        q = q.with_for_update(key_share=True)
    elif lock == "share":
        q = q.with_for_update(read=True)
    return (await conn.execute(q)).first()


async def get_context(engine: ContentEngine, scope_id: str, context_id: str) -> ContextInfo:
    async def fn(conn: AsyncConnection) -> ContextInfo:
        now = await jdb.db_now(conn, engine.backend)
        row = await _context_row(conn, engine, scope_id, context_id)
        if row is None:
            raise ResourceNotFoundError()
        return info_from_row(row, now)

    return await engine._run(fn, write=False, tx=None)


async def list_contexts(
    engine: ContentEngine,
    scope_id: str,
    *,
    kind: str | None,
    state: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[ContextInfo], str | None]:
    c = engine.tables.protection_contexts

    async def fn(conn: AsyncConnection) -> tuple[list[ContextInfo], str | None]:
        now = await jdb.db_now(conn, engine.backend)
        q = sa.select(c).where(c.c.scope_id == scope_id)
        if kind is not None:
            q = q.where(c.c.kind == kind)
        if state == "active":
            q = q.where(c.c.state == "active", c.c.expires_at > now)
        elif state == "expired":
            q = q.where(sa.or_(c.c.state == "expired", sa.and_(c.c.state == "active", c.c.expires_at <= now)))
        elif state == "deleted":
            q = q.where(c.c.state == "deleted")
        after = decode_cursor(cursor)
        if after is not None:
            q = q.where(c.c.context_id > after)
        rows = (await conn.execute(q.order_by(c.c.context_id).limit(limit + 1))).all()
        items = [info_from_row(r, now) for r in rows[:limit]]
        return items, (encode_cursor(rows[limit - 1].context_id) if len(rows) > limit else None)

    return await engine._run(fn, write=False, tx=None)


async def _drop_context_data(conn: AsyncConnection, engine: ContentEngine, scope_id: str, context_id: str) -> int:
    """Rank 13: every mapping of the context and the wrapped index key go away in the same transaction."""
    m = engine.tables.protection_mappings
    res = await conn.execute(sa.delete(m).where(m.c.scope_id == scope_id, m.c.context_id == context_id))
    return int(res.rowcount or 0)


async def delete_context(engine: ContentEngine, setup: ProtectionSetup, scope_id: str, context_id: str) -> ContextInfo:
    t = engine.tables
    now_of = _clock(engine)

    async def fn(conn: AsyncConnection) -> ContextInfo:
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)  # allowed on a disabled scope
        t0 = await now_of(conn)
        usage = await _ensure_usage(conn, engine, scope_id, t0)  # rank 11
        row = await _context_row(conn, engine, scope_id, context_id, "update")  # rank 12
        now = await now_of(conn)
        if row is None:
            raise ResourceNotFoundError()
        if row.state == "deleted":
            return info_from_row(row, now)  # idempotent: unchanged
        await _drop_context_data(conn, engine, scope_id, context_id)
        c = t.protection_contexts
        await conn.execute(
            sa.update(c)
            .where(c.c.scope_id == scope_id, c.c.context_id == context_id)
            .values(
                state="deleted",
                deleted_at=now,
                index_key_wrapped=None,
                index_key_version=None,
                mapping_count=0,
                mapping_bytes=0,
                metadata_expires_at=now + timedelta(seconds=setup.metadata_retention_s),
                updated_at=now,
            )
        )
        if row.state == "active":  # an expired, already-cleaned context was counted out at cleanup
            await conn.execute(
                sa.update(t.protection_scope_usage)
                .where(t.protection_scope_usage.c.scope_id == scope_id)
                .values(active_contexts=max(0, int(usage.active_contexts) - 1), updated_at=now)
            )
        return info_from_row(await _context_row(conn, engine, scope_id, context_id), now)

    return await engine._run(fn, write=True, tx=None)


# ---- protection -------------------------------------------------------------------------------------------
async def protect_decide(
    engine: ContentEngine,
    setup: ProtectionSetup,
    scope_id: str,
    context_id: str,
    kind: Kind,
    text: str,
    groups: Sequence[Group],
    job: JobBinding | None,
) -> tuple[str, list[dict[str, Any]], tuple[str, ...], dict[str, int], datetime]:
    """One transaction: everything or nothing. Returns (text, group details, markers, counts, expires_at)."""
    for attempt in (1, 2):
        try:
            return await _protect_once(engine, setup, scope_id, context_id, kind, text, groups, job)
        except _UniqueRace:
            if attempt == 2:
                raise DatabaseUnavailableError() from None
    raise AssertionError("unreachable")


async def _protect_once(
    engine: ContentEngine,
    setup: ProtectionSetup,
    scope_id: str,
    context_id: str,
    kind: Kind,
    text: str,
    groups: Sequence[Group],
    job: JobBinding | None,
) -> tuple[str, list[dict[str, Any]], tuple[str, ...], dict[str, int], datetime]:
    t = engine.tables
    now_of = _clock(engine)
    m = t.protection_mappings

    async def fn(conn: AsyncConnection) -> tuple[str, list[dict[str, Any]], tuple[str, ...], dict[str, int], datetime]:
        await _active_scope(conn, engine, scope_id)  # rank 1
        if job is not None:
            await lock_and_recheck_job(conn, t, scope_id, job, now_of)  # rank 8, then the time
        row = await _context_row(conn, engine, scope_id, context_id, "no_key_update")  # rank 12
        now = await now_of(conn)  # AFTER every lock wait
        _check_usable(row, kind, now)

        fragments: dict[str, None] = {}
        for g in groups:
            if g.action == "pseudonymize":
                fragments.setdefault(text[g.start : g.end], None)
        token_of_fragment: dict[str, str] = {}
        reused = created = 0
        if fragments:
            index_master = crypto.master_key(setup.keys, row.index_key_version or "")
            index_key = crypto.unwrap_index_key(
                index_master,
                scope_id=scope_id,
                context_id=context_id,
                version=row.index_key_version,
                wrapped=row.index_key_wrapped or b"",
            )
            eq_of = {frag: crypto.equality_index(index_key, frag) for frag in fragments}
            found = (
                await conn.execute(
                    sa.select(m)
                    .where(
                        m.c.scope_id == scope_id,
                        m.c.context_id == context_id,
                        m.c.equality_index.in_(list(eq_of.values())),
                    )
                    .order_by(m.c.token)  # rank 13, ascending
                )
            ).all()
            by_eq = {bytes(r.equality_index): r for r in found}
            pending: list[str] = []
            for frag, eq in eq_of.items():
                existing = by_eq.get(eq)
                if existing is None:
                    pending.append(frag)
                    continue
                # docs 9.3.1: the index only FINDS a candidate; authenticate it and compare it with the exact
                # fragment before the marker is reused. Any failure aborts the whole operation.
                master = crypto.master_key(setup.keys, existing.key_version)
                original = crypto.open_fragment(
                    master,
                    scope_id=scope_id,
                    context_id=context_id,
                    kind=kind,
                    token=existing.token,
                    version=existing.key_version,
                    eq_index=bytes(existing.equality_index),
                    salt=existing.salt,
                    nonce=existing.nonce,
                    ciphertext=existing.ciphertext,
                )
                if not crypto.same_fragment(original, frag):
                    raise MappingIntegrityError()
                token_of_fragment[frag] = existing.token
                reused += 1
            if pending:
                added_bytes = sum(len(f.encode("utf-8")) for f in pending)
                if (
                    int(row.mapping_count) + len(pending) > setup.limits.max_mappings_per_context
                    or int(row.mapping_bytes) + added_bytes > setup.limits.max_context_bytes
                ):
                    raise QuotaExceededError("The context mapping quota was exceeded.")
                version, master = crypto.active_version(setup.keys)
                for frag in pending:
                    for _ in range(8):  # a token clash is astronomically unlikely; a fresh token fixes it
                        token = new_token()
                        salt, nonce, ct = crypto.seal_fragment(
                            master,
                            scope_id=scope_id,
                            context_id=context_id,
                            kind=kind,
                            token=token,
                            version=version,
                            eq_index=eq_of[frag],
                            fragment=frag,
                        )
                        if await insert_ignore(
                            conn,
                            t,
                            m,
                            {
                                "scope_id": scope_id,
                                "context_id": context_id,
                                "token": token,
                                "key_version": version,
                                "salt": salt,
                                "nonce": nonce,
                                "ciphertext": ct,
                                "equality_index": eq_of[frag],
                                "created_at": now,
                            },
                        ):
                            break
                        clash = (
                            await conn.execute(
                                sa.select(sa.func.count()).where(
                                    m.c.scope_id == scope_id,
                                    m.c.context_id == context_id,
                                    m.c.equality_index == eq_of[frag],
                                )
                            )
                        ).scalar_one()
                        if clash:  # the uniqueness safety net fired: roll back and retry once
                            raise _UniqueRace()
                    else:
                        raise DatabaseUnavailableError()
                    token_of_fragment[frag] = token
                    created += 1
                c = t.protection_contexts
                await conn.execute(
                    sa.update(c)
                    .where(c.c.scope_id == scope_id, c.c.context_id == context_id)
                    .values(
                        mapping_count=int(row.mapping_count) + len(pending),
                        mapping_bytes=int(row.mapping_bytes) + added_bytes,
                        updated_at=now,
                    )
                )

        out: list[str] = []
        details: list[dict[str, Any]] = []
        cursor_in = cursor_out = 0
        for g in groups:
            out.append(text[cursor_in : g.start])
            cursor_out += g.start - cursor_in
            marker: str | None = None
            if g.action == "pseudonymize":
                marker = marker_of(token_of_fragment[text[g.start : g.end]])
                piece = marker
            else:
                piece = REDACTED
            details.append(
                {
                    "action": g.action,
                    "input_start": g.start,
                    "input_end": g.end,
                    "output_start": cursor_out,
                    "output_end": cursor_out + len(piece),
                    "marker": marker,
                }
            )
            out.append(piece)
            cursor_out += len(piece)
            cursor_in = g.end
        out.append(text[cursor_in:])
        result = "".join(out)
        markers = tuple(dict.fromkeys(d["marker"] for d in details if d["marker"]))
        # safety net: the output may hold exactly the markers we put there and nothing marker-like besides
        check = scan(result)
        if check.altered or {s[2] for s in check.strict} != set(markers):
            raise MarkerCollisionError()
        counts = {
            "findings": sum(g.findings for g in groups),
            "groups": len(groups),
            "pseudonymized": sum(1 for g in groups if g.action == "pseudonymize"),
            "redacted": sum(1 for g in groups if g.action == "redact"),
            "new_markers": created,
            "reused_markers": reused,
        }
        return result, details, markers, counts, row.expires_at

    return await engine._run(fn, write=True, tx=None)


# ---- restoration -------------------------------------------------------------------------------------------
async def restore_decide(
    engine: ContentEngine,
    setup: ProtectionSetup,
    scope_id: str,
    context_id: str,
    kind: Kind,
    tokens: Sequence[str],
    job: JobBinding | None,
) -> dict[str, str]:
    """Decrypts ONLY the markers to restore, all inside one transaction, before anything is assembled."""
    t = engine.tables
    now_of = _clock(engine)
    m = t.protection_mappings

    async def fn(conn: AsyncConnection) -> dict[str, str]:
        await _active_scope(conn, engine, scope_id)  # rank 1
        if job is not None:
            await lock_and_recheck_job(conn, t, scope_id, job, now_of)  # rank 8
        row = await _context_row(conn, engine, scope_id, context_id, "share")  # rank 12
        now = await now_of(conn)
        _check_usable(row, kind, now)
        if not tokens:
            return {}
        rows = (
            await conn.execute(
                sa.select(m)
                .where(m.c.scope_id == scope_id, m.c.context_id == context_id, m.c.token.in_(list(tokens)))
                .order_by(m.c.token)  # rank 13
            )
        ).all()
        by_token = {r.token: r for r in rows}
        if set(by_token) != set(tokens):
            raise MarkerUnknownError()
        originals: dict[str, str] = {}
        for token in sorted(tokens):
            r = by_token[token]
            master = crypto.master_key(setup.keys, r.key_version)
            raw = crypto.open_fragment(
                master,
                scope_id=scope_id,
                context_id=context_id,
                kind=kind,
                token=token,
                version=r.key_version,
                eq_index=bytes(r.equality_index),
                salt=r.salt,
                nonce=r.nonce,
                ciphertext=r.ciphertext,
            )
            try:
                originals[token] = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise MappingIntegrityError() from None
        return originals

    return await engine._run(fn, write=False, tx=None)


def assemble_restored(
    text: str,
    markers: Sequence[tuple[int, int, str]],
    consumed: frozenset[int],
    groups: Sequence[tuple[int, int]],
    originals: dict[str, str],
) -> tuple[str, int]:
    """One left-to-right pass over the received response: literal runs, ``[REDACTED]`` per group and the original
    per restored marker. Inserted originals are never re-examined (docs 7.4). Returns (text, restored occurrences)."""
    events: list[tuple[int, int, str]] = [(s, e, "R") for s, e in groups]
    restored = 0
    for i, (s, e, marker) in enumerate(markers):
        if i not in consumed:
            events.append((s, e, "M" + token_of(marker)))
    events.sort()
    out: list[str] = []
    pos = 0
    for s, e, kind in events:
        if s < pos:
            raise MarkerCollisionError()  # defensive: the classification guarantees disjoint events
        out.append(text[pos:s])
        if kind == "R":
            out.append(REDACTED)
        else:
            out.append(originals[kind[1:]])
            restored += 1
        pos = e
    out.append(text[pos:])
    return "".join(out), restored


_ = LimitExceededError
