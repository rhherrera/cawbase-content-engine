"""A06 cryptography and persistence on both backends: no plaintext at rest, rotation, missing keys, tampering,
reuse verification, fault injection, host transactions, observability. AC-A06-39..45, 61, 62 (and 40)."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine import AdminPermission, ContentEngine, EngineConfig, HostTransaction
from cawbase_content_engine.errors import (
    ContextUnavailableError,
    DatabaseUnavailableError,
    IncompatibleTransactionError,
    MappingIntegrityError,
    MarkerNotAuthorizedError,
    ProtectionKeyUnavailableError,
    ProtectionNotConfiguredError,
)
from cawbase_content_engine.protection.markers import token_of
from cawbase_content_engine.protection.types import ProtectionPolicy

from .a06_helpers import (
    INLINE,
    PSEUDO,
    Harness,
    Keys,
    capture_sql,
    f,
    inject_mapping,
    make_harness,
)
from .helpers import make_host_engine

SENT1, SENT2, SENT3 = "SENTINEL-ALPHA-9931", "sentinel.beta@example.test", "ZETA-ZETA-000777"
SENT_TEXT = f"A {SENT1}, B {SENT2}, C {SENT3}."


async def sentinel_protect(h: Harness, **kw):
    fs = [f(SENT_TEXT, SENT1), f(SENT_TEXT, SENT2), f(SENT_TEXT, SENT3, "password")]
    return await h.protect(SENT_TEXT, *fs, **kw)


def all_cells(rows) -> list[bytes]:
    out: list[bytes] = []
    for r in rows:
        for v in r:
            if isinstance(v, (bytes, bytearray, memoryview)):
                out.append(bytes(v))
            elif isinstance(v, (str, int, dt.datetime)) or v is None:
                out.append(str(v).encode())
    return out


async def test_nothing_sensitive_is_stored_in_clear_or_as_an_unkeyed_hash(a06: Harness):  # AC-A06-39
    cid, res = await sentinel_protect(a06)
    cells: list[bytes] = []
    for table in ("protection_contexts", "protection_mappings", "protection_scope_usage"):
        cells += all_cells(await a06.rows(table))
    blob = b"\x00".join(cells)
    for s in (SENT1, SENT2, SENT3):
        raw = s.encode()
        assert raw not in blob and raw.hex().encode() not in blob.lower()
        for digest in (hashlib.sha256(raw), hashlib.sha1(raw), hashlib.md5(raw)):  # noqa: S324,S303
            assert digest.digest() not in blob and digest.hexdigest().encode() not in blob.lower()
    for row in await a06.rows("protection_mappings"):
        assert (len(row.ciphertext) - 16) % 64 == 0 and len(row.salt) == 16 and len(row.nonce) == 12
        assert (
            len(row.equality_index) == 32
            and row.key_version == "v1"
            and row.token == token_of(next(m for m in res.markers if token_of(m) == row.token))
        )
    assert {r.ciphertext for r in await a06.rows("protection_mappings")} and len(
        {bytes(r.ciphertext) for r in await a06.rows("protection_mappings")}
    ) == 3
    ctx = (await a06.rows("protection_contexts"))[0]
    assert ctx.index_key_wrapped is not None and a06.keys.ring["v1"] not in blob


async def test_two_contexts_never_share_equality_indexes(a06: Harness):  # AC-A06-39 / 04
    await sentinel_protect(a06)
    await sentinel_protect(a06)
    idx = [bytes(r.equality_index) for r in await a06.rows("protection_mappings")]
    assert len(idx) == 6 and len(set(idx)) == 6  # a per-context key: equal values do not correlate across contexts


async def test_rotation_keeps_old_records_readable_and_the_marker_stable(a06: Harness):  # AC-A06-40
    cid, res = await sentinel_protect(a06)
    old_markers = dict(zip((SENT1, SENT2, SENT3), res.markers, strict=True))
    a06.keys.add("v2")  # v2 becomes active, v1 stays available
    text2 = f"{SENT1} otra vez y nuevo: Zoe Nueva"
    again = await a06.ops().protect_text(
        "a",
        context_id=cid,
        kind="conversation",
        text=_set(a06, text2, f(text2, SENT1), f(text2, "Zoe Nueva")),
        policy=PSEUDO,
        analysis=INLINE,
    )
    assert again.markers[0] == old_markers[SENT1]  # the SAME marker after rotating (index key unchanged)
    assert again.counts["reused_markers"] == 1 and again.counts["new_markers"] == 1
    versions = {r.token: r.key_version for r in await a06.rows("protection_mappings")}
    assert sorted(versions.values()) == ["v1", "v1", "v1", "v2"]
    assert versions[token_of(again.markers[1])] == "v2"  # new records use the active version
    ctx = (await a06.rows("protection_contexts"))[0]
    assert ctx.index_key_version == "v1"
    mixed = f"{again.markers[0]} {old_markers[SENT3]} {again.markers[1]}"
    out = await a06.restore(cid, mixed)
    assert out.text == f"{SENT1} {SENT3} Zoe Nueva"


def _set(h: Harness, text: str, *findings):
    h.adapter.on(text, *findings)
    return text


async def test_missing_keys_fail_closed_without_partial_text(a06: Harness):  # AC-A06-41
    cid, res = await sentinel_protect(a06)
    a06.keys.add("v2")
    text2 = "Hola Zoe Nueva"
    new = await a06.ops().protect_text(
        "a",
        context_id=cid,
        kind="conversation",
        text=_set(a06, text2, f(text2, "Zoe Nueva")),
        policy=PSEUDO,
        analysis=INLINE,
    )
    a06.keys.retire("v1")
    response = f"{res.markers[0]} y {new.markers[0]}"  # one record needs v1 (gone), the other v2
    with pytest.raises(ProtectionKeyUnavailableError):
        await a06.restore(cid, response)
    assert (await a06.restore(cid, new.markers[0])).text == "Zoe Nueva"  # records of the available version still read
    # creating a context needs a usable active version
    a06.keys.broken_active = True
    with pytest.raises(ProtectionKeyUnavailableError):
        await a06.ops().create_context("a", kind="conversation")
    assert len(await a06.rows("protection_contexts")) == 1
    a06.keys.broken_active = False
    a06.keys.ring.pop("v2")
    a06.keys.active = "v9"
    with pytest.raises(ProtectionKeyUnavailableError):
        await a06.ops().create_context("a", kind="conversation")
    assert len(await a06.rows("protection_contexts")) == 1


async def tamper(h: Harness, table: str = "protection_mappings", **values):
    t = getattr(h.engine.tables, table)
    async with h.engine._engine.begin() as c:  # type: ignore[union-attr]
        await c.execute(sa.update(t).values(**values))


@pytest.mark.parametrize("column", ["ciphertext", "nonce", "salt", "equality_index"])
async def test_a_modified_record_fails_authentication(a06: Harness, column):  # AC-A06-42
    cid, res = await a06.protect("Hola Zoe", f("Hola Zoe", "Zoe"))
    row = (await a06.rows("protection_mappings"))[0]
    value = bytearray(getattr(row, column))
    value[0] ^= 0x01
    await tamper(a06, **{column: bytes(value)})
    with pytest.raises(MappingIntegrityError):
        await a06.restore(cid, res.text)


async def test_a_changed_version_label_fails_closed(a06: Harness):  # AC-A06-42
    cid, res = await a06.protect("Hola Zoe", f("Hola Zoe", "Zoe"))
    a06.keys.add("v2", activate=False)
    await tamper(a06, key_version="v2")  # an existing, WRONG key under another label
    with pytest.raises(MappingIntegrityError):
        await a06.restore(cid, res.text)
    await tamper(a06, key_version="v9")  # a label whose key does not exist
    with pytest.raises(ProtectionKeyUnavailableError):
        await a06.restore(cid, res.text)


async def test_a_row_moved_to_another_token_context_kind_or_scope_fails(migrated):  # AC-A06-42
    backend, url = migrated
    h = await make_harness(backend, url, scopes=("a", "b"))
    try:
        cid, res = await h.protect("Hola Zoe", f("Hola Zoe", "Zoe"))
        m = res.markers[0]
        t = h.engine.tables
        # (1) another token: the marker the row now answers to is not the one it was sealed for
        other_token = "Z" * 16
        await tamper(h, token=other_token)
        other_marker = f"[[CCE1:{other_token}]]"
        with pytest.raises(MappingIntegrityError):
            await h.restore(cid, other_marker, delivered_markers=[other_marker])
        await tamper(h, token=token_of(m))
        assert (await h.restore(cid, m)).text == "Zoe"
        # (2) another context of the same scope and the same kind
        c2 = await h.context()
        await tamper(h, context_id=c2)
        with pytest.raises(MappingIntegrityError):
            await h.restore(c2, m, delivered_markers=[m])
        await tamper(h, context_id=cid)
        # (3) the context's kind was changed to 'job' while the record was sealed for 'conversation'
        await tamper(h, table="protection_contexts", kind="job")
        with pytest.raises(MappingIntegrityError):
            await h.ops().restore_text(
                "a", context_id=cid, kind="job", text=m, analysis=INLINE, delivered_markers=[m]
            ) if h.adapter.on(m) else None
        await tamper(h, table="protection_contexts", kind="conversation")
        # (4) another scope: the record is attached to a context of scope 'b'
        cb = await h.context("b")
        async with h.engine._engine.begin() as c:  # type: ignore[union-attr]
            await c.execute(sa.update(t.protection_mappings).values(scope_id="b", context_id=cb))
        h.adapter.on(m)
        with pytest.raises(MappingIntegrityError):
            await h.ops("b").restore_text(
                "b", context_id=cb, kind="conversation", text=m, analysis=INLINE, delivered_markers=[m]
            )
    finally:
        await h.engine.aclose()


async def test_tampered_index_key_blob_fails_protection(a06: Harness):  # AC-A06-42
    cid = await a06.context()
    ctx = (await a06.rows("protection_contexts"))[0]
    blob = bytearray(ctx.index_key_wrapped)
    blob[-1] ^= 1
    await tamper(a06, table="protection_contexts", index_key_wrapped=bytes(blob))
    with pytest.raises(MappingIntegrityError):
        await a06.protect("Hola Zoe", f("Hola Zoe", "Zoe"), ctx=cid)
    assert await a06.rows("protection_mappings") == []


# ---- reuse verification (AC-A06-61) -------------------------------------------------------------------------
async def two_values(h: Harness):
    text = "Ana y Zoe"
    cid, res = await h.protect(text, f(text, "Ana"), f(text, "Zoe"))
    return cid, text, res.markers


async def protect_again(h: Harness, cid: str, text: str, *frags: str):
    h.adapter.on(text, *[f(text, x) for x in frags])
    return await h.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
    )


async def test_index_pointing_to_another_fragments_index_never_reuses_a_marker(a06: Harness):  # AC-A06-61 (a)
    cid, text, (m_ana, m_zoe) = await two_values(a06)
    rows = {r.token: r for r in await a06.rows("protection_mappings")}
    ana, zoe = rows[token_of(m_ana)], rows[token_of(m_zoe)]
    # the row of "Ana" is made to answer to the equality index of "Zoe" (after removing Zoe's own row)
    async with a06.engine._engine.begin() as c:  # type: ignore[union-attr]
        t = a06.engine.tables.protection_mappings
        await c.execute(sa.delete(t).where(t.c.token == zoe.token))
        await c.execute(sa.update(t).where(t.c.token == ana.token).values(equality_index=zoe.equality_index))
    before = await a06.rows("protection_mappings")
    with pytest.raises(MappingIntegrityError):
        await protect_again(a06, cid, "Hola Zoe", "Zoe")
    assert [(r.token, bytes(r.equality_index)) for r in await a06.rows("protection_mappings")] == [
        (r.token, bytes(r.equality_index)) for r in before
    ]  # nothing was created in its place


async def test_authentic_row_with_different_content_is_rejected(a06: Harness):  # AC-A06-61 (c)
    cid = await a06.context()
    marker = await inject_mapping(a06, cid, "Ana")
    # re-seal the row so that it authenticates perfectly but holds another value under the index of "Zoe"
    from cawbase_content_engine.protection import crypto

    t = a06.engine.tables
    async with a06.engine._engine.begin() as c:  # type: ignore[union-attr]
        ctx = (await c.execute(sa.select(t.protection_contexts))).one()
        ik = crypto.unwrap_index_key(
            a06.keys.ring["v1"], scope_id="a", context_id=cid, version="v1", wrapped=ctx.index_key_wrapped
        )
        zoe_idx = crypto.equality_index(ik, "Zoe")
        salt, nonce, ct = crypto.seal_fragment(
            a06.keys.ring["v1"], scope_id="a", context_id=cid, kind="conversation",
            token=token_of(marker), version="v1", eq_index=zoe_idx, fragment="Ana",
        )  # fmt: skip
        await c.execute(
            sa.update(t.protection_mappings).values(salt=salt, nonce=nonce, ciphertext=ct, equality_index=zoe_idx)
        )
    with pytest.raises(MappingIntegrityError):
        await protect_again(a06, cid, "Hola Zoe", "Zoe")
    assert len(await a06.rows("protection_mappings")) == 1


@pytest.mark.parametrize("column", ["ciphertext", "nonce", "salt"])
async def test_reusing_a_marker_authenticates_the_stored_record(a06: Harness, column):  # AC-A06-61 (d)
    cid, text, (m_ana, _m) = await two_values(a06)
    row = next(r for r in await a06.rows("protection_mappings") if r.token == token_of(m_ana))
    value = bytearray(getattr(row, column))
    value[1] ^= 4
    t = a06.engine.tables.protection_mappings
    async with a06.engine._engine.begin() as c:  # type: ignore[union-attr]
        await c.execute(sa.update(t).where(t.c.token == row.token).values(**{column: bytes(value)}))
    with pytest.raises(MappingIntegrityError):
        await protect_again(a06, cid, "Hola Ana", "Ana")


async def test_changed_version_label_on_reuse(a06: Harness):  # AC-A06-61 (d)
    cid, text, (m_ana, _m) = await two_values(a06)
    a06.keys.add("v2", activate=False)
    t = a06.engine.tables.protection_mappings
    async with a06.engine._engine.begin() as c:  # type: ignore[union-attr]
        await c.execute(sa.update(t).where(t.c.token == token_of(m_ana)).values(key_version="v2"))
    with pytest.raises(MappingIntegrityError):
        await protect_again(a06, cid, "Hola Ana", "Ana")


async def test_index_moved_to_a_value_nobody_computes_is_the_declared_limit(a06: Harness):  # AC-A06-61 (b)
    cid, text, (m_ana, _m) = await two_values(a06)
    t = a06.engine.tables.protection_mappings
    async with a06.engine._engine.begin() as c:  # type: ignore[union-attr]
        await c.execute(sa.update(t).where(t.c.token == token_of(m_ana)).values(equality_index=b"\x07" * 32))
    again = await protect_again(a06, cid, "Hola Ana", "Ana")  # dedup is lost: a second marker is created ...
    assert again.markers[0] != m_ana and again.counts["new_markers"] == 1
    with pytest.raises(MappingIntegrityError):  # ... but the tampered row can never restore a wrong value
        await a06.restore(cid, m_ana)
    assert (await a06.restore(cid, again.markers[0])).text == "Ana"


# ---- previous key absent when reusing (AC-A06-62) ----------------------------------------------------------
async def test_absent_row_key_on_reuse_is_isolated_from_the_index_key(migrated):  # AC-A06-62 (a)
    backend, url = migrated
    keys = Keys("v0")  # v0 wraps the context's index key and stays available throughout
    h = await make_harness(backend, url, keys=keys)
    try:
        cid = await h.context()
        keys.add("v1")  # active: v1 encrypts the record
        text = "Hola Ana"
        first = await protect_again(h, cid, text, "Ana")
        assert (await h.rows("protection_mappings"))[0].key_version == "v1"
        keys.add("v2")  # rotate again and retire v1: the index key (v0) is still available
        keys.retire("v1")
        before = [(r.token, bytes(r.ciphertext)) for r in await h.rows("protection_mappings")]
        with pytest.raises(ProtectionKeyUnavailableError):
            await protect_again(h, cid, text, "Ana")
        assert [(r.token, bytes(r.ciphertext)) for r in await h.rows("protection_mappings")] == before  # no new marker
        # restoring the old marker fails for the same reason; a value never seen before still works (v2)
        with pytest.raises(ProtectionKeyUnavailableError):
            await h.restore(cid, first.markers[0])
        ok = await protect_again(h, cid, "Hola Zoe", "Zoe")
        assert ok.counts["new_markers"] == 1
        keys.ring["v1"] = b"\x01" * 32  # a WRONG key under the old label is an integrity failure, not "unavailable"
        with pytest.raises(MappingIntegrityError):
            await protect_again(h, cid, text, "Ana")
    finally:
        await h.engine.aclose()


async def test_absent_index_key_version_and_the_cases_that_need_no_keys(migrated):  # AC-A06-62 (b), (c)
    backend, url = migrated
    keys = Keys("v1")
    h = await make_harness(backend, url, keys=keys)
    try:
        cid = await h.context()  # the index key is wrapped with v1
        keys.add("v2")
        keys.retire("v1")
        with pytest.raises(ProtectionKeyUnavailableError):
            await protect_again(h, cid, "Hola Ana", "Ana")
        assert await h.rows("protection_mappings") == []
        calls = keys.key_calls
        red = await h.ops().protect_text(  # (c) only redaction: no key and no mapping is touched
            "a", context_id=cid, kind="conversation", text=_set(h, "Hola Ana", f("Hola Ana", "Ana")),
            policy=ProtectionPolicy("redact"), analysis=INLINE,
        )  # fmt: skip
        assert red.text == "Hola [REDACTED]" and keys.key_calls == calls
        none = await h.ops().protect_text(
            "a",
            context_id=cid,
            kind="conversation",
            text=_set(
                h,
                "Nada",
            ),
            policy=PSEUDO,
            analysis=INLINE,
        )
        assert none.outcome == "no_findings" and keys.key_calls == calls
        keys.ring["v1"] = b"\x02" * 32  # restoring a v1 key: the index key no longer authenticates
        with pytest.raises(MappingIntegrityError):
            await protect_again(h, cid, "Hola Ana", "Ana")
    finally:
        await h.engine.aclose()


async def test_reuse_with_the_old_key_present_returns_the_same_marker(a06: Harness):  # AC-A06-62 / 40
    cid, text, (m_ana, _m) = await two_values(a06)
    a06.keys.add("v2")
    again = await protect_again(a06, cid, "Hola Ana", "Ana")
    assert again.markers == (m_ana,)


# ---- persistence failures (AC-A06-43) ----------------------------------------------------------------------
async def test_failure_during_insertion_returns_no_text_and_leaves_nothing(a06: Harness, monkeypatch):
    from cawbase_content_engine.protection import ops

    cid = await a06.context()
    text = "Ana y Zoe y Luz"
    fs = [f(text, "Ana"), f(text, "Zoe"), f(text, "Luz")]
    a06.adapter.on(text, *fs)
    real, calls = ops.insert_ignore, []

    async def flaky(conn, tables, table, values):
        calls.append(1)
        if len(calls) == 2:
            raise OperationalError("INSERT", {}, Exception("connection reset"))
        return await real(conn, tables, table, values)

    monkeypatch.setattr(ops, "insert_ignore", flaky)
    with pytest.raises(DatabaseUnavailableError):
        await a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )
    assert await a06.rows("protection_mappings") == []  # all or nothing: no half-visible markers
    assert (await a06.rows("protection_contexts"))[0].mapping_count == 0
    monkeypatch.setattr(ops, "insert_ignore", real)
    ok = await a06.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
    )
    assert len(ok.markers) == 3 and len(await a06.rows("protection_mappings")) == 3


async def test_a_failed_commit_and_an_unknown_commit_converge_on_retry(a06: Harness, monkeypatch):  # AC-A06-43
    import cawbase_content_engine.engine as engine_module

    cid = await a06.context()
    text = "Ana y Zoe"
    a06.adapter.on(text, f(text, "Ana"), f(text, "Zoe"))

    def call():
        return a06.ops().protect_text(
            "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
        )

    # (1) the COMMIT itself fails: nothing is persisted and no text is returned
    from sqlalchemy import event

    sync = a06.engine._engine.sync_engine  # type: ignore[union-attr]
    armed = {"on": True}

    def boom(conn):
        if armed["on"]:
            raise OperationalError("COMMIT", {}, Exception("commit failed"))

    event.listen(sync, "commit", boom)
    try:
        with pytest.raises(DatabaseUnavailableError):
            await call()
    finally:
        armed["on"] = False
        event.remove(sync, "commit", boom)
    assert await a06.rows("protection_mappings") == []
    # (2) the commit SUCCEEDS but its answer is lost: no text, mappings exist, the retry reuses them
    real_run_own = engine_module.run_own

    async def lost_answer(*a, **k):
        result = await real_run_own(*a, **k)
        if k.get("write"):  # only the decision transaction: it COMMITS, then its answer is "lost"
            raise DatabaseUnavailableError()
        return result

    monkeypatch.setattr(engine_module, "run_own", lost_answer)
    with pytest.raises(DatabaseUnavailableError):
        await call()
    monkeypatch.setattr(engine_module, "run_own", real_run_own)
    persisted = {r.token for r in await a06.rows("protection_mappings")}
    assert len(persisted) == 2
    retry = await call()
    assert {token_of(m) for m in retry.markers} == persisted and retry.counts["new_markers"] == 0
    assert len(await a06.rows("protection_mappings")) == 2  # no duplicates


async def test_retry_guarantee_is_exactly_the_documented_one(a06: Harness):  # AC-A06-43 (b), (c), (d)
    text = "Ana, Zoe y Luz"
    a, z, lz = f(text, "Ana"), f(text, "Zoe"), f(text, "Luz")
    cid, first = await a06.protect(text, a, z)
    # (b) same text, same effective analysis, same policy: byte-identical
    same = await a06.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
    )
    assert same.text == first.text and same.markers == first.markers and same.groups == first.groups
    # (c) another effective analysis (other findings) and another policy: the output may differ, but every fragment
    #     pseudonymized in BOTH runs keeps its marker; a fragment that was never pseudonymized gets a new marker
    a06.adapter.on(text, a, lz)
    other_analysis = await a06.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
    )
    assert other_analysis.text != first.text and other_analysis.markers[0] == first.markers[0]
    assert other_analysis.counts["new_markers"] == 1 and other_analysis.counts["reused_markers"] == 1
    a06.adapter.on(text, a, z)
    other_policy = await a06.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text, analysis=INLINE,
        policy=ProtectionPolicy("pseudonymize", {"personal_data": "redact"}),
    )  # fmt: skip
    assert other_policy.text != first.text and other_policy.markers == () and other_policy.counts["redacted"] == 2
    back = await a06.ops().protect_text(
        "a", context_id=cid, kind="conversation", text=text, policy=PSEUDO, analysis=INLINE
    )
    assert back.text == first.text  # and the original markers are still there for the same fragments
    assert len(await a06.rows("protection_mappings")) == 3  # Ana, Zoe (first run) + Luz (other analysis)


# ---- host transactions (AC-A06-44) ---------------------------------------------------------------------------
async def test_every_operation_rejects_a_host_transaction_before_any_effect(migrated):  # AC-A06-44
    backend, url = migrated
    host = await make_host_engine(backend, url)
    keys, adapter_h = Keys(), None
    from cawbase_content_engine.inference import InferenceSetup
    from cawbase_content_engine.protection.types import ProtectionSetup

    from .a03_helpers import build_profile
    from .a06_helpers import ScriptedAdapter

    adapter_h = ScriptedAdapter()
    eng = ContentEngine(
        EngineConfig(async_engine=host),
        inference=InferenceSetup(adapter_h, timeout_s=5.0),
        protection=ProtectionSetup(keys=keys),
    )
    await eng.start()
    try:
        await build_profile(eng, "a", create_scope=True)
        from .a06_helpers import A06_ALL

        async with host.connect() as conn:
            await conn.begin()
            tx = HostTransaction(conn)
            ops = eng.scoped(eng.trusted_scope_context("a", A06_ALL, actor="t"), tx)
            cid = "ctx_" + "0" * 32
            with capture_sql(eng) as stmts:
                for coro in (
                    ops.create_context("a", kind="conversation"),
                    ops.get_context("a", cid),
                    ops.list_contexts("a"),
                    ops.delete_context("a", cid),
                    ops.protect_text(
                        "a", context_id=cid, kind="conversation", text="x", policy=PSEUDO, analysis=INLINE
                    ),
                    ops.restore_text(
                        "a", context_id=cid, kind="conversation", text="x", analysis=INLINE, delivered_markers=[]
                    ),
                ):
                    with pytest.raises(IncompatibleTransactionError):
                        await coro
            assert stmts == [] and adapter_h.calls == [] and keys.key_calls == 0
            await conn.rollback()
    finally:
        await eng.aclose()
        await host.dispose()


async def test_without_protection_setup_operations_raise_after_authorization(migrated):  # AC-A06-51
    backend, url = migrated
    eng = ContentEngine(EngineConfig(database_url=url))
    await eng.start()
    try:
        from .a06_helpers import A06_ALL
        from .helpers import make_scope

        await make_scope(eng, "a")
        ops = eng.scoped(eng.trusted_scope_context("a", A06_ALL, actor="t"))
        with pytest.raises(ProtectionNotConfiguredError):
            await ops.create_context("a", kind="conversation")
        with pytest.raises(ProtectionNotConfiguredError):
            await ops.restore_text(
                "a", context_id="ctx_" + "0" * 32, kind="conversation", text="x", analysis=INLINE, delivered_markers=[]
            )
        denied = eng.scoped(eng.trusted_scope_context("a", [], actor="t"))
        from cawbase_content_engine.errors import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):  # authorization comes first
            await denied.create_context("a", kind="conversation")
        assert eng.protection is None and (await eng.readiness()).ready
    finally:
        await eng.aclose()


async def test_start_runs_the_crypto_known_answer_test_and_validates_setup_type(migrated, monkeypatch):
    backend, url = migrated
    from cawbase_content_engine.errors import InvalidConfigurationError
    from cawbase_content_engine.protection import crypto

    with pytest.raises(InvalidConfigurationError):
        ContentEngine(EngineConfig(database_url=url), protection=object())  # type: ignore[arg-type]

    def broken():
        raise InvalidConfigurationError("known-answer test failed")

    monkeypatch.setattr(crypto, "self_test", broken)
    from cawbase_content_engine.protection.types import ProtectionSetup

    eng = ContentEngine(EngineConfig(database_url=url), protection=ProtectionSetup(keys=Keys()))
    with pytest.raises(InvalidConfigurationError):
        await eng.start()
    await eng.aclose()


# ---- observability (AC-A06-45) -------------------------------------------------------------------------------
async def test_logs_errors_and_diagnostics_never_hold_text_markers_keys_or_hashes(a06: Harness, caplog):  # AC-A06-45
    # The engine's own loggers are checked. Third-party DEBUG logging (aiosqlite, SQLAlchemy echo) prints bound
    # parameters such as tokens: enabling it is the host's decision (documented limit, see the report).
    caplog.set_level(logging.DEBUG)
    cid, res = await sentinel_protect(a06)
    errors: list[str] = []
    out = await a06.restore(cid, res.text)
    assert out.text == SENT_TEXT
    bad = marker = f"[[CCE1:{'Q' * 16}]]"
    for coro in (
        a06.restore(cid, f"{res.markers[0]} {bad}", delivered_markers=[res.markers[0], bad]),
        a06.restore(cid, f"[[cce1:{'q' * 16}]] {res.markers[0]}", delivered_markers=[res.markers[0]]),
        a06.restore(cid, res.markers[0], delivered_markers=[]),
    ):
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            errors += [str(exc), repr(exc), getattr(exc, "code", "")]
    await tamper(a06, ciphertext=b"\x00" * 80)
    try:
        await a06.restore(cid, res.markers[0])
    except Exception as exc:  # noqa: BLE001
        errors += [str(exc), repr(exc)]
    diag = repr(
        await a06.engine.admin(a06.engine.trusted_admin_context(list(AdminPermission), actor="t")).diagnostics()
    )
    summary = repr(await a06.engine.maintenance().run_protection_cleanup())
    usage = repr(await a06.engine.maintenance().protection_key_usage())
    secrets = [SENT1, SENT2, SENT3, *res.markers, *(token_of(m) for m in res.markers), bad, marker]
    secrets += [k.hex() for k in a06.keys.ring.values()] + [repr(k) for k in a06.keys.ring.values()]
    secrets += [bytes(r.equality_index).hex() for r in await a06.rows("protection_mappings")]
    haystack = "\n".join(
        [r.getMessage() + repr(r.__dict__) for r in caplog.records if r.name.startswith("cawbase_content_engine")]
        + errors
        + [diag, summary, usage, repr(a06.engine.protection)]
    )
    for s in secrets:
        assert s not in haystack, s[:12]
    assert any(
        r.name == "cawbase_content_engine.protection" for r in caplog.records
    )  # it does log (ids, codes, counts)
    for r in caplog.records:
        assert not hasattr(r, "text") and not hasattr(r, "offsets")


async def test_context_error_is_unavailable_for_deleted_contexts_in_protection_too(a06: Harness):
    cid = await a06.context()
    await a06.ops().delete_context("a", cid)
    with pytest.raises(ContextUnavailableError):
        await a06.protect("Hola Zoe", f("Hola Zoe", "Zoe"), ctx=cid)
    with pytest.raises(MarkerNotAuthorizedError):
        await a06.restore(cid, f"[[CCE1:{'Q' * 16}]]", delivered_markers=[])


_ = create_async_engine
