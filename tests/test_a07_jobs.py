"""A07 durable operations end to end on PostgreSQL and SQLite: real storage, real isolated child, real cryptography,
real permissions; the inference of the source analysis is the A03 mock adapter itself."""

from __future__ import annotations

import pytest

from cawbase_content_engine.docx_protection.types import ExistingContext, NewContext
from cawbase_content_engine.errors import (
    AnalysisSourceInvalidError,
    DerivativeMismatchError,
    IdempotencyConflictError,
    IncompatibleTransactionError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from cawbase_content_engine.protection.markers import STRICT

from .a07_fixtures import MAIL, NAME, Doc, p, r
from .a07_helpers import (
    CAPS,
    FULL,
    PSEUDO,
    SP,
    World,
    analyse,
    mock_for,
    ops,
    read_all,
    run,
    setup_scope,
    unit_texts,
    upload,
)


@pytest.fixture
async def world(migrated, tmp_path):
    _, url = migrated
    w = World(url=url, tmp=tmp_path)
    yield w
    await w.close()


DOC = Doc(
    body=[p(r("Contactar a An"), r("a Ej", "<w:b/>"), r(f"emplo ({MAIL}).")), p(r(f"Mencion libre de {NAME}."))],
    headers={"default": [p(r(f"Encabezado {NAME}"))]},
    comments=["Comentario sintetico"],
    core={"dc:title": "Informe sintetico", "dc:creator": "Autor Sintetico"},
)


def picks_for(data: bytes) -> dict[str, list[tuple[str, str, int]]]:
    out: dict[str, list[tuple[str, str, int]]] = {}
    for text in unit_texts(data):
        if text.startswith("Contactar"):
            out[text] = [("personal_data", NAME, 0), ("personal_data", MAIL, 0)]
        elif text.startswith("Encabezado"):
            out[text] = [("personal_data", NAME, 0)]
        else:
            out[text] = []  # the free mention is deliberately NOT selected
    return out


async def protect_world(world: World, data: bytes, *, context=None, policy=PSEUDO, picks=None):
    base = await setup_scope(world)
    adapter = await mock_for(base, picks or picks_for(data))
    eng = await world.engine(adapter)
    fid, aid = await analyse(eng, data)
    o = ops(eng)
    acc = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=policy, context=context or NewContext(), idempotency_key="p1"
    )
    out = await run(eng, acc.job.job_id)
    return eng, o, fid, aid, acc, out


async def test_protect_and_restore_end_to_end(world):  # AC-A07-01, 41
    data = DOC.build()
    eng, o, fid, aid, acc, out = await protect_world(world, data)
    assert out.state == "succeeded", out
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    assert res.counts["pseudonymized"] == 3 and res.counts["occurrences"] == 3
    assert {c["element_class"] for c in res.authorized_removals_performed} >= {
        "comments",
        "core_descriptive_properties",
    }
    assert res.source["provenance"]["simulated"] is True and res.source["provenance"]["kind"] == "mock"
    raw_report = str(res)
    assert NAME not in raw_report and MAIL not in raw_report  # no value in the report
    derivative = await read_all(await o.open_docx_protected("a", acc.job.job_id))
    texts = unit_texts(derivative)
    assert NAME not in texts[0] and MAIL not in texts[0] and len(STRICT.findall(texts[0])) == 2
    assert texts[1] == f"Mencion libre de {NAME}."  # the identical, non-selected occurrence stays in place
    info = await o.get_docx_derivative("a", res.derivative_id)
    assert info.state == "active" and info.occurrence_count == 3
    copy_id = await upload(o, derivative, "copy")
    racc = await o.submit_docx_restoration(
        "a",
        derivative_id=res.derivative_id,
        file_id=copy_id,
        context_id=acc.context_id,
        kind="job",
        idempotency_key="r1",
    )
    rout = await run(eng, racc.job.job_id)
    assert rout.state == "succeeded", rout
    restored = await read_all(await o.open_docx_restored("a", racc.job.job_id))
    assert unit_texts(restored) == [t for t in unit_texts(data)]
    rres = await o.get_docx_restoration_result("a", racc.job.job_id)
    assert rres.counts["occurrences_restored"] == 3


async def test_generic_readers_never_serve_a07_artifacts(world):  # AC-A07-54, 85
    data = DOC.build()
    eng, o, fid, aid, acc, out = await protect_world(world, data)
    generic = ops(eng, [p_ for p_ in FULL if p_ not in (SP.DOCX_PROTECTED_READ, SP.DOCX_RESTORED_READ)])
    for kind in ("result", "source_map"):
        with pytest.raises(ResourceNotFoundError):
            await generic.open_job_artifact("a", acc.job.job_id, kind)
    with pytest.raises(ResourceNotFoundError):
        await generic.get_job_result("a", acc.job.job_id)
    with pytest.raises(PermissionDeniedError):
        await generic.open_docx_protected("a", acc.job.job_id)
    info = await generic.get_job("a", acc.job.job_id)
    assert info.operation == "docx_protection" and info.state == "succeeded"
    # docx:protect does not grant downloading the original
    protector = ops(eng, [SP.DOCX_PROTECT, SP.RESULTS_READ])
    with pytest.raises(PermissionDeniedError):
        await protector.open_file_content("a", fid)


async def test_submission_checks_and_idempotency(world):  # AC-A07-02, 03, 65, 86
    data = DOC.build()
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, picks_for(data)))
    fid, aid = await analyse(eng, data)
    o = ops(eng)
    import inspect

    params = set(inspect.signature(o.submit_docx_protection).parameters)
    assert not params & {"findings", "analysis", "source_map", "offsets"}
    with pytest.raises(PermissionDeniedError):
        await ops(eng, [SP.DOCX_PROTECT]).submit_docx_protection(
            "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="x"
        )
    with pytest.raises(ResourceNotFoundError):
        await o.submit_docx_protection(
            "a", analysis_job_id="job_" + "0" * 32, policy=PSEUDO, context=NewContext(), idempotency_key="x2"
        )
    text_job = await o.submit_text_analysis("a", text=f"Hola {NAME}", idempotency_key="t1", capabilities=CAPS)
    with pytest.raises(AnalysisSourceInvalidError):
        await o.submit_docx_protection(
            "a", analysis_job_id=text_job.job.job_id, policy=PSEUDO, context=NewContext(), idempotency_key="x3"
        )
    a1 = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="k"
    )
    a2 = await o.submit_docx_protection(
        "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="k"
    )
    assert a2.replayed and a2.job.job_id == a1.job.job_id and a2.context_id == a1.context_id
    assert a2.context_expires_at == a1.context_expires_at
    with pytest.raises(IdempotencyConflictError):
        await o.submit_docx_protection(
            "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(ttl_s=120), idempotency_key="k"
        )
    ctxs = await o.list_contexts("a")
    assert len([c for c in ctxs.items if c.context_id == a1.context_id]) == 1
    from cawbase_content_engine.transactions import HostTransaction

    async with eng._engine.connect() as conn:  # host transactions are refused before any effect
        await conn.begin()
        h = eng.scoped(eng.trusted_scope_context("a", FULL, actor="t"), HostTransaction(conn))
        for coro in (
            h.submit_docx_protection(
                "a", analysis_job_id=aid, policy=PSEUDO, context=NewContext(), idempotency_key="h"
            ),
            h.submit_docx_restoration(
                "a",
                derivative_id="ddv_" + "0" * 32,
                file_id=fid,
                context_id="ctx_" + "0" * 32,
                kind="job",
                idempotency_key="h2",
            ),
            h.open_docx_protected("a", a1.job.job_id),
        ):
            with pytest.raises(IncompatibleTransactionError):
                await coro
        await conn.rollback()


async def test_existing_context_and_restore_text_cross_path(world):  # AC-A07-58 (P-A07-01)
    data = DOC.build()
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, picks_for(data)))
    o = ops(eng)
    conv = await o.create_context("a", kind="conversation")
    fid, aid = await analyse(eng, data)
    acc = await o.submit_docx_protection(
        "a",
        analysis_job_id=aid,
        policy=PSEUDO,
        context=ExistingContext(conv.context_id, "conversation"),
        idempotency_key="p",
    )
    assert (await run(eng, acc.job.job_id)).state == "succeeded"
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    marker = next(g["marker"] for u in res.units for g in u["groups"] if g["marker"])
    # restore_text with text:restore restores the value AS TEXT only with A06's conditions; no file is produced
    from cawbase_content_engine.protection.types import JobAnalysis

    reply = f"Respuesta con {marker}."
    # (the reply's analysis comes from the mock: its fixture is registered on a second engine)
    eng2 = await world.engine(await mock_for(base, {reply: []}))
    o2 = ops(eng2)
    tjob = await o2.submit_text_analysis("a", text=reply, idempotency_key="rt", capabilities=CAPS)
    assert (await run(eng2, tjob.job.job_id)).state == "succeeded"
    from cawbase_content_engine.errors import MarkerNotAuthorizedError

    with pytest.raises(MarkerNotAuthorizedError):
        await o2.restore_text(
            "a",
            context_id=conv.context_id,
            kind="conversation",
            text=reply,
            analysis=JobAnalysis(tjob.job.job_id),
            delivered_markers=[],
        )
    back = await o2.restore_text(
        "a",
        context_id=conv.context_id,
        kind="conversation",
        text=reply,
        analysis=JobAnalysis(tjob.job.job_id),
        delivered_markers=[marker],
    )
    assert back.text in (f"Respuesta con {NAME}.", f"Respuesta con {MAIL}.")
    with pytest.raises(PermissionDeniedError):
        await ops(eng2, [SP.RESULTS_READ, SP.DOCX_RESTORE]).restore_text(
            "a",
            context_id=conv.context_id,
            kind="conversation",
            text=reply,
            analysis=JobAnalysis(tjob.job.job_id),
            delivered_markers=[marker],
        )


async def test_restoration_rejects_another_file(world):  # AC-A07-43
    data = DOC.build()
    eng, o, fid, aid, acc, out = await protect_world(world, data)
    res = await o.get_docx_protection_result("a", acc.job.job_id)
    derivative = await read_all(await o.open_docx_protected("a", acc.job.job_id))
    edited = derivative[:-1] + bytes([derivative[-1] ^ 1])
    for label, blob in (("other", data), ("edited", edited)):
        cid = await upload(o, blob, "c-" + label)
        with pytest.raises((DerivativeMismatchError, Exception)) as ei:
            await o.submit_docx_restoration(
                "a",
                derivative_id=res.derivative_id,
                file_id=cid,
                context_id=acc.context_id,
                kind="job",
                idempotency_key="r-" + label,
            )
        assert getattr(ei.value, "code", "") in ("derivative_mismatch", "file_not_usable")
