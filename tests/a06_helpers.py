"""Helpers for the A06 tests: REAL cryptography, persistence, locks and permissions; only inference is simulated
(a scripted detection adapter in the role of the A03 mock: it returns exactly the findings a test declares)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event

from cawbase_content_engine import ContentEngine, EngineConfig, ScopePermission
from cawbase_content_engine.inference import (
    AdapterDescriptor,
    AdapterState,
    InferenceSetup,
    full_coverage,
)
from cawbase_content_engine.protection.types import (
    InlineAnalysis,
    KeyRing,
    ProtectionPolicy,
    ProtectionSetup,
)

from .a03_helpers import AE, PR, PW, TYPES, build_profile

SP = ScopePermission
TP, TR, CR, CD, RR = SP.TEXT_PROTECT, SP.TEXT_RESTORE, SP.CONTEXTS_READ, SP.CONTEXTS_DELETE, SP.RESULTS_READ
A06_ALL = [TP, TR, CR, CD, AE, RR]
PSEUDO = ProtectionPolicy("pseudonymize")
REDACT = ProtectionPolicy("redact")
INLINE = InlineAnalysis()
NAME = "Ana Ejemplo"
MAIL = "ana.ejemplo@example.test"
PASSWORD = "EjemploNoReal-123"
TEXT = f"Contactar a {NAME} ({MAIL}). Mi contraseña es {PASSWORD}."


def span(text: str, fragment: str, occurrence: int = 0) -> tuple[int, int]:
    pos = -1
    for _ in range(occurrence + 1):
        pos = text.index(fragment, pos + 1)
    return pos, pos + len(fragment)


def f(text: str, fragment: str, type_id: str = "personal_data", occurrence: int = 0) -> tuple[str, int, int]:
    s, e = span(text, fragment, occurrence)
    return (type_id, s, e)


class ScriptedAdapter:
    """Returns the findings registered for the EXACT text (and nothing else). ``mode`` can make detection
    inconclusive. It stands for the model: it is told what to answer, it never decides anything."""

    def __init__(self) -> None:
        self.script: dict[str, list[tuple[str, int, int]]] = {}
        self.mode: dict[str, tuple[str, str | None]] = {}  # text -> (status, reason)
        self.default: Callable[[str], list[tuple[str, int, int]]] | None = None
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self._d = AdapterDescriptor(
            kind="mock", adapter_id="cce-a06-test", adapter_version="1", configuration_id="a06-1"
        )

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._d

    async def probe(self) -> AdapterState:
        return AdapterState.READY

    def on(self, text: str, *findings: tuple[str, int, int]) -> str:
        self.script[text] = list(findings)
        return text

    async def infer(self, request: Any) -> dict[str, Any]:
        text = request.text
        self.calls.append(text)
        if self.gate is not None:
            await self.gate.wait()
        out: dict[str, Any] = {"contract_version": 1}
        if "classification" in request.capabilities:
            ids = [c.element_id for c in request.categories]
            out["classification"] = {
                "status": "completed",
                "reason": None,
                "coverage": full_coverage(ids, text),
                "scores": [{"category_id": i, "score": 0.5} for i in ids],
            }
        if "detection" in request.capabilities:
            ids = [t.element_id for t in request.sensitive_types]
            status, reason = self.mode.get(text, ("completed", None))
            if text in self.script:
                found = self.script[text]
            elif self.default is not None:
                found = self.default(text)
            else:
                found = []
            cov = full_coverage(ids, text)
            if reason == "partial_coverage":
                cov = {"groups": [{"ids": sorted(ids), "regions": [{"start": 0, "end": max(1, len(text) // 2)}]}]}
                found = [x for x in found if x[2] <= max(1, len(text) // 2)]
            out["detection"] = {
                "status": status,
                "reason": reason,
                "coverage": cov,
                "findings": [{"type_id": t, "start": s, "end": e, "text": text[s:e]} for t, s, e in found],
            }
        return out


class Keys:
    """A mutable in-memory key provider that counts how often key material was requested."""

    def __init__(self, *versions: str, active: str | None = None) -> None:
        names = versions or ("v1",)
        self.ring: dict[str, bytes] = {v: os.urandom(32) for v in names}
        self.active = active or names[0]
        self.key_calls = 0
        self.broken_active = False

    def active_version(self) -> str:
        if self.broken_active:
            raise RuntimeError("no active key")
        return self.active

    def key(self, version: str) -> bytes | None:
        self.key_calls += 1
        return self.ring.get(version)

    def add(self, version: str, *, activate: bool = True) -> None:
        self.ring[version] = os.urandom(32)
        if activate:
            self.active = version

    def retire(self, version: str) -> None:
        self.ring.pop(version, None)


@dataclass
class Harness:
    engine: ContentEngine
    adapter: ScriptedAdapter
    keys: Keys
    url: str
    backend: str
    extra: dict[str, Any] = field(default_factory=dict)

    def ops(self, scope_id: str = "a", perms: Any = None, tx: Any = None) -> Any:
        ctx = self.engine.trusted_scope_context(scope_id, A06_ALL if perms is None else perms, actor="test-host")
        return self.engine.scoped(ctx, tx)

    async def context(self, scope_id: str = "a", kind: str = "conversation", **kw: Any) -> str:
        return (await self.ops(scope_id).create_context(scope_id, kind=kind, **kw)).context_id

    async def protect(self, text: str, *findings: tuple[str, int, int], ctx: str | None = None, policy=PSEUDO, **kw):
        self.adapter.on(text, *findings)
        cid = ctx or await self.context(kind=kw.get("kind", "conversation"))
        kw.setdefault("kind", "conversation")
        res = await self.ops().protect_text("a", context_id=cid, text=text, policy=policy, analysis=INLINE, **kw)
        return cid, res

    async def restore(self, cid: str, response: str, *findings: tuple[str, int, int], material=None, **kw):
        self.adapter.on(response, *findings)
        kw.setdefault("kind", "conversation")
        material = kw.pop("delivered_material", material)
        if material is None and "delivered_markers" not in kw:
            from cawbase_content_engine.protection.markers import find_markers

            kw["delivered_markers"] = find_markers(response)
        if material is not None:
            kw.pop("delivered_markers", None)
        return await self.ops().restore_text(
            "a", context_id=cid, text=response, analysis=INLINE, delivered_material=material, **kw
        )

    async def rows(self, table: str, where: str = "") -> list[Any]:
        t = getattr(self.engine.tables, table)
        q = sa.select(t)
        async with self.engine._engine.connect() as c:  # type: ignore[union-attr]
            return list((await c.execute(q)).all())

    async def sql(self, statement: str, **params: Any) -> Any:
        async with self.engine._engine.begin() as c:  # type: ignore[union-attr]
            return await c.execute(sa.text(statement), params)


async def make_harness(
    backend: str,
    url: str,
    *,
    keys: Keys | None = None,
    adapter: ScriptedAdapter | None = None,
    setup: dict[str, Any] | None = None,
    scopes: tuple[str, ...] = ("a",),
    engine_kwargs: dict[str, Any] | None = None,
) -> Harness:
    keys = keys or Keys()
    adapter = adapter or ScriptedAdapter()
    ps = ProtectionSetup(keys=keys, **(setup or {}))
    eng = ContentEngine(
        EngineConfig(database_url=url),
        inference=InferenceSetup(adapter, timeout_s=10.0),
        protection=ps,
        **(engine_kwargs or {}),
    )
    await eng.start()
    for i, sid in enumerate(scopes):
        await build_profile(eng, sid, create_scope=True, default=True)
        _ = i
    return Harness(eng, adapter, keys, url, backend)


@contextmanager
def capture_sql(engine: ContentEngine) -> Iterator[list[str]]:
    """Every statement the engine's connections send while the block runs (lowercased text)."""
    stmts: list[str] = []
    sync = engine._engine.sync_engine  # type: ignore[union-attr]

    def before(conn: Any, cursor: Any, statement: str, params: Any, context: Any, many: bool) -> None:
        stmts.append(statement.lower())

    event.listen(sync, "before_cursor_execute", before)
    try:
        yield stmts
    finally:
        event.remove(sync, "before_cursor_execute", before)


def touches(stmts: list[str], table: str) -> list[str]:
    return [s for s in stmts if table in s]


async def wait_for_lock_wait(engine: ContentEngine, minimum: int = 1) -> None:
    """PostgreSQL only: poll until at least ``minimum`` backends wait for a row/transaction lock."""
    for _ in range(400):
        async with engine._engine.connect() as c:  # type: ignore[union-attr]
            n = (
                await c.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if n >= minimum:
            return
        await asyncio.sleep(0.025)
    raise AssertionError("no backend ever waited for a lock")


def tmp_dir(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return p


_ = (PR, PW, TYPES, KeyRing, datetime, timedelta)


async def inject_mapping(
    h: Harness, cid: str, fragment: str, *, token: str | None = None, key_version: str | None = None
) -> str:
    """Tests only: writes a VALID mapping row for ``fragment`` directly (with the host's real keys), the way a
    legitimate-but-unusual record would look. Returns its marker."""
    import datetime as dt

    from cawbase_content_engine.protection import crypto
    from cawbase_content_engine.protection.markers import marker_of, new_token

    t = h.engine.tables
    token = token or new_token()
    async with h.engine._engine.begin() as c:  # type: ignore[union-attr]
        ctx = (await c.execute(sa.select(t.protection_contexts).where(t.protection_contexts.c.context_id == cid))).one()
        ik = crypto.unwrap_index_key(
            h.keys.ring[ctx.index_key_version],
            scope_id=ctx.scope_id,
            context_id=cid,
            version=ctx.index_key_version,
            wrapped=ctx.index_key_wrapped,
        )
        version = key_version or h.keys.active
        eq = crypto.equality_index(ik, fragment)
        salt, nonce, ct = crypto.seal_fragment(
            h.keys.ring[version],
            scope_id=ctx.scope_id,
            context_id=cid,
            kind=ctx.kind,
            token=token,
            version=version,
            eq_index=eq,
            fragment=fragment,
        )
        await c.execute(
            sa.insert(t.protection_mappings).values(
                scope_id=ctx.scope_id,
                context_id=cid,
                token=token,
                key_version=version,
                salt=salt,
                nonce=nonce,
                ciphertext=ct,
                equality_index=eq,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
    return marker_of(token)


async def expire_context(h: Harness, cid: str, *, when: Any = None) -> None:
    """Tests only: moves ``expires_at`` of a context to the past (or to ``when``) directly in the table."""
    import datetime as dt

    t = h.engine.tables.protection_contexts
    at = when or (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    async with h.engine._engine.begin() as c:  # type: ignore[union-attr]
        await c.execute(sa.update(t).where(t.c.context_id == cid).values(expires_at=at))
