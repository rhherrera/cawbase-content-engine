"""A08 access rules, submission checks, compatibility with earlier analyses and runtimes (PostgreSQL and SQLite)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from cawbase_content_engine.errors import (
    AnalysisSourceInvalidError,
    DerivativeMismatchError,
    IdempotencyConflictError,
    IncompatibleTransactionError,
    PermissionDeniedError,
    ResourceNotFoundError,
)
from cawbase_content_engine.pdf_protection.types import ExistingContext, NewContext
from cawbase_content_engine.protection.markers import REDACTED

from . import a08_synth as S
from .a08_helpers import (
    CAPS,
    FULL,
    POLICY,
    SP,
    World,
    analyse,
    mock_for,
    ops,
    read_all,
    restore_case,
    run,
    setup_scope,
    upload,
)

NAME, MAIL = "Ana Gil Soler", "ana.soler@example.test"
SYN = S.build(
    [
        " ".join(
            [
                S.line(72, 780, "Pagina de control sintetico con texto suficiente para una linea entera."),
                S.line(72, 740, f"Contacto: {NAME}, correo {MAIL}, sede norte."),
                S.line(72, 700, "Documento 00000000-X del titular, verificado sin cambios."),
                S.line(72, 660, f"Mencion libre de {NAME} en otra linea."),
            ]
        )
    ],
    info={"Title": "Titulo sintetico", "CreationDate": "D:20260101000000Z"},
)


def syn_findings(data: bytes) -> dict[str, list[tuple[str, int, int]]]:
    from .a08_helpers import units

    out = {}
    for u in units(data):
        t = u["text"]
        f = []
        if NAME in t and "Contacto" in t:
            f.append(("personal_data", t.index(NAME), t.index(NAME) + len(NAME)))
            f.append(("personal_data", t.index(MAIL), t.index(MAIL) + len(MAIL)))
        if "00000000-X" in t:
            f.append(("password", t.index("00000000-X"), t.index("00000000-X") + 10))
        out[t] = f
    return out


async def failure(eng, acc) -> str | None:
    await run(eng, acc.job.job_id)
    return (await ops(eng, FULL).get_job("a", acc.job.job_id)).failure_code


@pytest.fixture
async def world(migrated, tmp_path):
    _, url = migrated
    w = World(url=url, tmp=tmp_path)
    yield w
    await w.close()


async def protected(world: World, *, context=None, key: str = "p1"):
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    fid, aid = await analyse(eng, SYN, key="f-" + key)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=context or NewContext(), idempotency_key=key
    )
    out = await run(eng, acc.job.job_id)
    info = await o.get_job("a", acc.job.job_id)
    assert out.state == "succeeded", (info.failure_code, info.last_error_code)
    return eng, o, fid, aid, acc, base


async def test_synthetic_document_cycle_and_identical_occurrence(world):  # AC-A08-30, 37, D-A08-03
    eng, o, fid, aid, acc, _ = await protected(world)
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    assert {c["element_class"] for c in res.authorized_removals_performed} == {"document_info_descriptive"}
    der = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    assert b"Titulo sintetico" not in der
    from .a08_helpers import page_texts

    t = page_texts(der)[0]
    assert MAIL not in t and "00000000-X" not in t and f"Mencion libre de {NAME}" in t
    racc, rout = await restore_case(eng, o, der, res.derivative_id, acc.context_id)
    assert rout.state == "succeeded"
    rest = await read_all(await o.open_pdf_restored("a", racc.job.job_id))
    rt = page_texts(rest)[0]
    assert f"Contacto: {NAME}, correo {MAIL}" in rt and REDACTED in rt and "00000000-X" not in rt
    assert b"Titulo sintetico" not in rest  # removed auxiliary classes never come back


async def test_generic_readers_never_serve_a08_artifacts(world):  # AC-A08-39
    eng, o, fid, aid, acc, _ = await protected(world)
    generic = ops(eng, [p for p in FULL if p not in (SP.PDF_PROTECTED_READ, SP.PDF_RESTORED_READ)])
    for kind in ("result", "source_map"):
        with pytest.raises(ResourceNotFoundError):
            await generic.open_job_artifact("a", acc.job.job_id, kind)
    with pytest.raises(ResourceNotFoundError):
        await generic.get_job_result("a", acc.job.job_id)
    info = await generic.get_job("a", acc.job.job_id)
    assert info.operation == "pdf_protection" and info.state == "succeeded"
    protector = ops(eng, [SP.PDF_PROTECT, SP.RESULTS_READ])
    with pytest.raises(PermissionDeniedError):  # pdf:protect never downloads the original
        await protector.open_file_content("a", fid)
    docx_only = ops(eng, [p for p in FULL if not str(p).startswith("pdf:")])
    with pytest.raises(PermissionDeniedError):  # docx:* never grant PDF operations
        await docx_only.open_pdf_protected("a", acc.job.job_id)
    with pytest.raises(ResourceNotFoundError):  # and the DOCX readers never serve a PDF job
        await ops(eng, FULL).open_docx_protected("a", acc.job.job_id)


async def test_permission_matrix_of_every_a08_operation(world):  # AC-A08-39
    eng, o, fid, aid, acc, _ = await protected(world)
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    der = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    racc, _r = await restore_case(eng, o, der, res.derivative_id, acc.context_id)
    copy = await upload(o, der, "copy-2")
    calls = {
        SP.PDF_PROTECT: lambda x: x.submit_pdf_protection(
            "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="pp"
        ),
        SP.PDF_RESTORE: lambda x: x.submit_pdf_restoration(
            "a",
            derivative_id=res.derivative_id,
            file_id=copy,
            context_id=acc.context_id,
            kind="job",
            idempotency_key="rr",
        ),
        SP.PDF_PROTECTED_READ: lambda x: x.open_pdf_protected("a", acc.job.job_id),
        SP.PDF_RESTORED_READ: lambda x: x.open_pdf_restored("a", racc.job.job_id),
    }
    for needed, call in calls.items():
        with pytest.raises(PermissionDeniedError):
            await call(ops(eng, [p for p in FULL if p != needed]))
    with pytest.raises(PermissionDeniedError):  # protection also needs results:read
        await calls[SP.PDF_PROTECT](ops(eng, [p for p in FULL if p != SP.RESULTS_READ]))
    for reader, arg in (("get_pdf_protection_result", acc.job.job_id), ("get_pdf_derivative", res.derivative_id)):
        with pytest.raises(PermissionDeniedError):
            await getattr(ops(eng, [p for p in FULL if p != SP.PDF_PROTECTED_READ]), reader)("a", arg)
    with pytest.raises(PermissionDeniedError):
        await ops(eng, [p for p in FULL if p != SP.PDF_RESTORED_READ]).get_pdf_restoration_result("a", racc.job.job_id)
    other_scope = eng.scoped(eng.trusted_scope_context("b", FULL, actor="t"))
    from cawbase_content_engine.errors import ResourceNotAccessibleError

    with pytest.raises(ResourceNotAccessibleError):
        await other_scope.open_pdf_protected("a", acc.job.job_id)
    from .helpers import make_scope

    await make_scope(eng, "b")
    with pytest.raises(ResourceNotFoundError):  # another scope never sees the job nor the binding
        await other_scope.open_pdf_protected("b", acc.job.job_id)
    with pytest.raises(ResourceNotFoundError):
        await other_scope.get_pdf_derivative("b", res.derivative_id)
    from cawbase_content_engine.permissions import ALL_ADMIN_PERMISSIONS, ALL_SCOPE_PERMISSIONS

    assert len(ALL_SCOPE_PERMISSIONS) + len(ALL_ADMIN_PERMISSIONS) == 29


async def test_submission_checks_idempotency_and_host_transaction(world):  # AC-A08-31, 42
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    fid, aid = await analyse(eng, SYN)
    o = ops(eng, FULL)
    a = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="k"
    )
    b = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="k"
    )
    assert b.replayed and b.job.job_id == a.job.job_id and b.context_id == a.context_id
    from cawbase_content_engine.protection.types import ProtectionPolicy

    with pytest.raises(IdempotencyConflictError):
        await o.submit_pdf_protection(
            "a", analysis_job_id=aid, policy=ProtectionPolicy("redact"), context=NewContext(), idempotency_key="k"
        )
    with pytest.raises(AnalysisSourceInvalidError):  # S1: a protection job is not an analysis
        await o.submit_pdf_protection(
            "a", analysis_job_id=a.job.job_id, policy=POLICY, context=NewContext(), idempotency_key="k2"
        )
    with pytest.raises(ResourceNotFoundError):
        await o.submit_pdf_protection(
            "a", analysis_job_id="job_" + "0" * 32, policy=POLICY, context=NewContext(), idempotency_key="k3"
        )
    from cawbase_content_engine.transactions import HostTransaction

    async with eng._engine.connect() as conn:  # host transactions are refused before any effect
        await conn.begin()
        h = eng.scoped(eng.trusted_scope_context("a", FULL, actor="t"), HostTransaction(conn))
        for coro in (
            h.submit_pdf_protection("a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="h"),
            h.submit_pdf_restoration(
                "a",
                derivative_id="pdv_" + "0" * 32,
                file_id=fid,
                context_id="ctx_" + "0" * 32,
                kind="job",
                idempotency_key="h2",
            ),
            h.open_pdf_protected("a", a.job.job_id),
        ):
            with pytest.raises(IncompatibleTransactionError):
                await coro
        await conn.rollback()


async def test_a_docx_analysis_cannot_feed_pdf_protection_nor_the_reverse(world):  # S6
    from .a07_fixtures import Doc, p, r
    from .a07_helpers import mock_for as docx_mock
    from .a07_helpers import unit_texts

    docx = Doc(body=[p(r(f"Contacto {NAME}."))]).build()
    base = await setup_scope(world)
    fx = {t: [("personal_data", NAME, 0)] for t in unit_texts(docx)}
    eng = await world.engine(await docx_mock(base, fx))
    _f, aid = await analyse(eng, docx, key="d")
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="x"
    )
    assert (await failure(eng, acc)) == "analysis_source_invalid"


async def test_marker_collision_in_the_original(world):  # PC-04
    data = S.build([S.line(72, 740, f"Texto con [[CCE1:0123456789ABCDEF]] y {NAME} al final de la linea.")])
    base = await setup_scope(world)
    eng = await world.engine(
        await mock_for(base, {t: [] for t in [__import__("tests.a08_helpers", fromlist=["x"]).units(data)[0]["text"]]})
    )
    _f, aid = await analyse(eng, data)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=POLICY, context=NewContext(), idempotency_key="c"
    )
    assert (await failure(eng, acc)) == "marker_collision"


async def test_restoration_rejects_another_file_and_needs_no_inference(world):  # AC-A08-37, 38
    eng, o, fid, aid, acc, _ = await protected(world)
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    der = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    other = await upload(o, der + b"\n", "other")
    with pytest.raises(DerivativeMismatchError):
        await o.submit_pdf_restoration(
            "a",
            derivative_id=res.derivative_id,
            file_id=other,
            context_id=acc.context_id,
            kind="job",
            idempotency_key="o",
        )
    bare = await world.engine()  # no inference configured at all
    racc, rout = await restore_case(bare, ops(bare, FULL), der, res.derivative_id, acc.context_id, key="bare")
    assert rout.state == "succeeded"


async def test_existing_context_and_restore_text_cross_path(world):  # AC-A08-41 (D-A08-06)
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    o = ops(eng, FULL)
    conv = await o.create_context("a", kind="conversation")
    eng2, o2, fid, aid, acc, _ = await protected(
        world, context=ExistingContext(conv.context_id, "conversation"), key="pc"
    )
    res = await o2.get_pdf_protection_result("a", acc.job.job_id)
    marker = next(g["marker"] for u in res.units for g in u["groups"] if g["marker"])
    from cawbase_content_engine.errors import MarkerNotAuthorizedError
    from cawbase_content_engine.protection.types import JobAnalysis

    reply = f"Respuesta con {marker}."
    eng3 = await world.engine(await mock_for(base, {reply: []}))
    o3 = ops(eng3, FULL)
    tjob = await o3.submit_text_analysis("a", text=reply, idempotency_key="rt", capabilities=CAPS)
    assert (await run(eng3, tjob.job.job_id)).state == "succeeded"
    with pytest.raises(MarkerNotAuthorizedError):
        await o3.restore_text(
            "a",
            context_id=conv.context_id,
            kind="conversation",
            text=reply,
            analysis=JobAnalysis(tjob.job.job_id),
            delivered_markers=[],
        )
    back = await o3.restore_text(
        "a",
        context_id=conv.context_id,
        kind="conversation",
        text=reply,
        analysis=JobAnalysis(tjob.job.job_id),
        delivered_markers=[marker],
    )
    assert back.text in (f"Respuesta con {NAME}.", f"Respuesta con {MAIL}.")
    with pytest.raises(PermissionDeniedError):  # pdf:restore is about files; text needs text:restore
        await ops(eng3, [SP.RESULTS_READ, SP.PDF_RESTORE]).restore_text(
            "a",
            context_id=conv.context_id,
            kind="conversation",
            text=reply,
            analysis=JobAnalysis(tjob.job.job_id),
            delivered_markers=[marker],
        )


# ---- compatibility (D-A08-04, P-A08-04) -------------------------------------------------------------------
def _v1_text(data: bytes) -> list[str]:
    from cawbase_content_engine.documents import pdf

    from .a07_helpers import LIM

    return [u["text"] for u in pdf.extract(data, LIM)["units"]]


async def test_v1_analysis_is_stale_and_a_new_analysis_of_the_same_file_is_valid(world, monkeypatch):  # AC-A08-32
    from cawbase_content_engine.jobs import ops as jops

    base = await setup_scope(world)
    per = syn_findings(SYN) | {t: [] for t in _v1_text(SYN)}
    eng = await world.engine(await mock_for(base, per))
    o = ops(eng, FULL)
    fid = await upload(o, SYN, "f")
    # an analysis accepted by a 0.4.0-0.6.0 runtime: A05 snapshot (no extractor set) and the A05 key
    real_key, real_set, real_vid = jops.requirements_key, jops.extractor_set, jops.validation_identity_v2
    from cawbase_content_engine.jobs.ident import validation_identity as v1_identity

    monkeypatch.setattr(jops, "validation_identity_v2", v1_identity)
    monkeypatch.setattr(jops, "extractor_set", lambda lim: None)
    monkeypatch.setattr(
        jops, "requirements_key", lambda e, op: real_key(e, "document_analysis_v1" if op == "document_analysis" else op)
    )
    old = await o.submit_document_analysis("a", file_id=fid, idempotency_key="old", capabilities=CAPS)
    monkeypatch.setattr(jops, "requirements_key", real_key)
    monkeypatch.setattr(jops, "extractor_set", real_set)
    monkeypatch.setattr(jops, "validation_identity_v2", real_vid)
    assert (await run(eng, old.job.job_id)).state == "succeeded"  # the new runtime finishes it with the frozen v1
    result = await o.get_job_result("a", old.job.job_id)
    assert result["provenance"]["extractor"]["id"] == "cce-docs-extract"
    sm = b"".join([c async for c in await o.open_job_artifact("a", old.job.job_id, "source_map")])
    assert b"document-structures/1" in sm  # read as it was published, never reinterpreted
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=old.job.job_id, policy=POLICY, context=NewContext(), idempotency_key="s"
    )
    assert (await failure(eng, acc)) == "analysis_source_stale"
    new = await o.submit_document_analysis("a", file_id=fid, idempotency_key="new", capabilities=CAPS)
    assert (await run(eng, new.job.job_id)).state == "succeeded"
    acc2 = await o.submit_pdf_protection(
        "a", analysis_job_id=new.job.job_id, policy=POLICY, context=NewContext(), idempotency_key="s2"
    )
    assert (await run(eng, acc2.job.job_id)).state == "succeeded"


async def test_a_file_refused_by_v1_can_be_analysed_again_with_v2(world, monkeypatch):  # D-A08-04
    from cawbase_content_engine.jobs import ops as jops

    from .a08_helpers import corpus, findings_for

    data = corpus("C1")  # Type0: refused by the A05 extractor
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, findings_for(data, "T1")))
    o = ops(eng, FULL)
    fid = await upload(o, data, "c1")
    real_key, real_set, real_vid = jops.requirements_key, jops.extractor_set, jops.validation_identity_v2
    from cawbase_content_engine.jobs.ident import validation_identity as v1_identity

    monkeypatch.setattr(jops, "validation_identity_v2", v1_identity)
    monkeypatch.setattr(jops, "extractor_set", lambda lim: None)
    monkeypatch.setattr(
        jops, "requirements_key", lambda e, op: real_key(e, "document_analysis_v1" if op == "document_analysis" else op)
    )
    old = await o.submit_document_analysis("a", file_id=fid, idempotency_key="old", capabilities=CAPS)
    monkeypatch.setattr(jops, "requirements_key", real_key)
    monkeypatch.setattr(jops, "extractor_set", real_set)
    monkeypatch.setattr(jops, "validation_identity_v2", real_vid)
    await run(eng, old.job.job_id)
    assert await failure(eng, old) == "file_rejected"
    new = await o.submit_document_analysis("a", file_id=fid, idempotency_key="new", capabilities=CAPS)
    assert (await run(eng, new.job.job_id)).state == "succeeded"
    f = await o.get_file("a", fid)
    assert f.download_validation == "passed"  # the A08 identity prevails over the v1 refusal


async def test_an_earlier_runtime_does_not_claim_new_jobs(world):  # AC-A08-32, H-A08-09
    base = await setup_scope(world)
    eng = await world.engine(await mock_for(base, syn_findings(SYN)))
    o = ops(eng, FULL)
    fid = await upload(o, SYN, "f")
    acc = await o.submit_document_analysis("a", file_id=fid, idempotency_key="n", capabilities=CAPS)
    from cawbase_content_engine.jobs.ops import requirements_key

    old_keys = {
        requirements_key(eng, "text_analysis"),
        requirements_key(eng, "document_analysis_v1"),
        requirements_key(eng, "docx_protection"),
        requirements_key(eng, "docx_restoration"),
    }  # exactly what 0.6.0 announces
    async with eng._engine.begin() as c:
        j = eng.tables.jobs
        key = (await c.execute(sa.select(j.c.requirements_key).where(j.c.job_id == acc.job.job_id))).scalar_one()
    assert key not in old_keys
    assert (
        requirements_key(eng, "pdf_protection") not in old_keys
        and requirements_key(eng, "pdf_restoration") not in old_keys
    )
    assert key in eng.job_worker("w").supported_keys()
