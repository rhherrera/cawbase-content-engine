"""A05 document analysis end to end (real extraction child, real storage) and the conservative support matrix."""

from __future__ import annotations

import io
import zipfile

import pikepdf
import pytest

from cawbase_content_engine.errors import FileNotUsableError

from .a05_helpers import TEXT, chunked, make_docx, make_engine, make_pdf, raw, ready_scope, run_all


@pytest.fixture
async def eng(migrated, tmp_path):
    _, url = migrated
    e = await make_engine(url, tmp_path)
    yield e
    await e.aclose()


async def doc_job(eng, ops, data: bytes, key: str, **kw):
    f = (await ops.upload_file("a", chunked(data), idempotency_key="up-" + key)).value
    acc = await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key=key, **kw)
    await run_all(eng)
    job = await ops.get_job("a", acc.job.job_id)
    return f, job


async def rejected(eng, ops, data: bytes, key: str) -> str:
    f, job = await doc_job(eng, ops, data, key)
    assert job.state == "failed" and job.failure_code == "file_rejected", (job.failure_code, job.last_error_code)
    return job.last_error_code  # type: ignore[no-any-return]


def zip_with(data: bytes, edit) -> bytes:
    """Rewrites a DOCX applying ``edit(name, bytes) -> bytes | None`` to each part (None drops it)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for info in src.infolist():
            new = edit(info.filename, src.read(info))
            if new is not None:
                zf.writestr(info.filename, new)
    return out.getvalue()


async def test_docx_end_to_end_with_unicode_and_non_covered_content(eng):
    ops = await ready_scope(eng)
    docx_bytes = make_docx(
        [TEXT, "😀 emoji y é combinado", ""], comments=["nota uno", "nota dos"], table=[["Ana Ejemplo", "x"]]
    )
    f, job = await doc_job(eng, ops, docx_bytes, "d1")
    assert job.state == "succeeded"
    result = await ops.get_job_result("a", job.job_id)
    cov = result["coverage"]
    # E content (comments / metadata) is NOT analysed: it is declared and blocks DOCUMENT coverage only
    classes = {d["element_class"]: d for d in cov["extraction"]["declared_not_covered"]}
    assert classes["comments"]["count"] == 2 and classes["comments"]["reason"] == "not_analyzed"
    assert (
        cov["extraction"]["textual_subset_complete"] is True
        and cov["extraction"]["document_coverage_complete"] is False
    )
    assert cov["extraction"]["authorized_removals_performed"] == []
    assert result["status"] == "completed" and result["document"]["format"] == "docx"
    units = {u["unit_id"]: u for u in result["units"]}
    first = units["u0001"]
    assert first["text_length"] == len(TEXT) and first["status"] == "analyzed"
    # offsets are code points of the exact text, and map to the original pieces
    smap = await ops.open_job_artifact("a", job.job_id, "source_map")
    import json

    sm = json.loads(b"".join([c async for c in smap]))
    assert sm["identity"]["file_sha256"] == f.sha256 and sm["identity"]["extractor"]["id"] == "cce-docs-extract"
    u1 = next(u for u in sm["units"] if u["unit_id"] == "u0001")
    assert u1["segments"][0]["pieces"][0]["part_name"] == "word/document.xml"
    assert units["u0002"]["text_length"] == len("😀 emoji y é combinado")  # 1 code point for the emoji
    assert any(u["status"] == "empty" for u in result["units"])  # the empty paragraph never reaches the adapter
    assert "" not in eng.inference.adapter.calls
    d = first["analysis"]["detection"]["findings"]
    assert [x["start"] for x in d][:1] == [TEXT.index("Ana Ejemplo")]
    assert result["coverage"]["inference"]["per_capability"]["classification"]["status"] == "completed"
    assert result["provenance"]["isolation_profile"] == "unconfined_development"
    # one validation per identity, authoritative
    v = await raw(eng, "SELECT result FROM {file_validations}")
    assert [r.result for r in v] == ["passed"]
    fm = await ops.get_file("a", f.file_id)
    assert fm.download_validation == "passed"


async def test_capability_states_without_ghost_coverage(eng):
    ops = await ready_scope(eng)
    data = make_docx([TEXT])
    f, job = await doc_job(eng, ops, data, "c1", capabilities=("detection",))
    pc = (await ops.get_job_result("a", job.job_id))["coverage"]["inference"]["per_capability"]
    assert pc["classification"] == {"status": "not_requested"}  # no `complete` field, no vacuous truth
    assert pc["detection"]["status"] == "completed" and pc["detection"]["complete"] is True
    _, job2 = await doc_job(eng, ops, data, "c2", capabilities=("classification",))
    pc2 = (await ops.get_job_result("a", job2.job_id))["coverage"]["inference"]["per_capability"]
    assert pc2["detection"] == {"status": "not_requested"} and pc2["classification"]["complete"] is True


async def test_pdf_end_to_end_two_pages(eng):
    ops = await ready_scope(eng)
    pdf = make_pdf([[TEXT, "segunda línea"], ["página dos"]])
    f, job = await doc_job(eng, ops, pdf, "p1")
    assert job.state == "succeeded"
    result = await ops.get_job_result("a", job.job_id)
    assert result["document"]["format"] == "pdf" and [u["kind"] for u in result["units"]] == ["pdf_page", "pdf_page"]
    import json

    sm = json.loads(b"".join([c async for c in await ops.open_job_artifact("a", job.job_id, "source_map")]))
    segs = sm["units"][0]["segments"]
    assert any(
        s["kind"] == "synthesized" and not s["pieces"] for s in segs
    )  # the separator between lines is synthesized
    assert segs[0]["pieces"][0]["page_index"] == 0 and segs[0]["pieces"][0]["relation"] == "1:1"
    assert result["units"][0]["text_length"] == len(TEXT) + 1 + len("segunda línea")


async def test_documents_without_text_are_not_a_vacuous_success(eng):
    ops = await ready_scope(eng)
    f, job = await doc_job(eng, ops, make_docx([""]), "e1")
    assert job.state == "failed" and job.failure_code == "no_extractable_text"


async def test_unit_larger_than_a03_limits_is_rejected_not_truncated(eng):
    ops = await ready_scope(eng)
    f, job = await doc_job(eng, ops, make_docx(["x" * 9000]), "big")
    assert job.state == "failed" and job.failure_code == "unit_too_large"
    assert eng.inference.adapter.calls == []


async def test_rejection_is_registered_per_identity_and_blocks_new_jobs(eng):
    ops = await ready_scope(eng)
    data = zip_with(
        make_docx([TEXT]),
        lambda n, b: (
            b.replace(b"<w:body>", b"<w:body><w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>x</w:t></w:r></w:p>")
            if n == "word/document.xml"
            else b
        ),
    )
    assert await rejected(eng, ops, data, "r1") == "hidden_text"
    f = (await ops.list_files("a")).items[0]
    assert f.download_validation == "rejected"
    with pytest.raises(FileNotUsableError):
        await ops.submit_document_analysis("a", file_id=f.file_id, idempotency_key="again")


DOCX_CASES = {
    "tracked_changes": lambda n, b: (
        b.replace(b"<w:body>", b'<w:body><w:ins w:id="1" w:author="a"><w:r><w:t>x</w:t></w:r></w:ins>')
        if n == "word/document.xml"
        else b
    ),
    "unsupported_structure": lambda n, b: (
        b.replace(b"<w:body>", b"<w:body><w:p><w:pPr><w:sectPr/></w:pPr></w:p>") if n == "word/document.xml" else b
    ),
    "external_reference": lambda n, b: (
        b.replace(
            b"</Relationships>",
            b'<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate" Target="http://example.test/t.dotx" TargetMode="External"/></Relationships>',
        )
        if n == "word/_rels/document.xml.rels"
        else b
    ),
    "macros": lambda n, b: (
        b.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.document.macroEnabled.main+xml")
        if n == "[Content_Types].xml"
        else b
    ),
}


@pytest.mark.parametrize("reason", sorted(DOCX_CASES))
async def test_docx_matrix_rejections(eng, reason):
    ops = await ready_scope(eng)
    assert await rejected(eng, ops, zip_with(make_docx([TEXT]), DOCX_CASES[reason]), "x-" + reason) == reason


async def test_docx_doctype_and_nesting_and_bombs_are_rejected(eng):
    ops = await ready_scope(eng)
    dt = zip_with(
        make_docx([TEXT]),
        lambda n, b: (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]>' + b.split(b"?>", 1)[1]
            if n == "word/document.xml"
            else b
        ),
    )
    assert await rejected(eng, ops, dt, "dt") == "unsupported_structure"
    bomb = zip_with(
        make_docx([TEXT]),
        lambda n, b: b + b"<!--" + b"A" * (60 * 1024 * 1024) + b"-->" if n == "word/document.xml" else b,
    )
    assert await rejected(eng, ops, bomb, "bomb") == "decompression_limit"
    assert await rejected(eng, ops, b"MZ not a document at all", "junk") == "unsupported_format"
    assert await rejected(eng, ops, b"\xd0\xcf\x11\xe0" + b"0" * 100, "ole") == "encrypted"


async def test_external_hyperlink_is_inventoried_not_resolved(eng):
    ops = await ready_scope(eng)
    link = lambda n, b: (
        b.replace(
            b"</Relationships>",
            b'<Relationship Id="rIdL" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="http://example.test/p" TargetMode="External"/></Relationships>',
        )
        if n == "word/_rels/document.xml.rels"
        else b
    )
    f, job = await doc_job(eng, ops, zip_with(make_docx([TEXT]), link), "h1")
    assert job.state == "succeeded"
    cov = (await ops.get_job_result("a", job.job_id))["coverage"]["extraction"]
    assert {"element_class": "hyperlink_targets", "count": 1, "reason": "no_extraction"} in cov["declared_not_covered"]
    assert cov["document_coverage_complete"] is False and cov["textual_subset_complete"] is True


def _save(pdf: pikepdf.Pdf, **kw) -> bytes:
    buf = io.BytesIO()
    pdf.save(buf, **kw)
    return buf.getvalue()


async def test_pdf_matrix_rejections(eng):
    ops = await ready_scope(eng)
    base = make_pdf([[TEXT]])
    # encrypted (empty user password: opening must not turn it into a success)
    pdf = pikepdf.open(io.BytesIO(base))
    enc = _save(pdf, encryption=pikepdf.Encryption(user="", owner="o"))
    assert await rejected(eng, ops, enc, "p-enc") == "encrypted"
    # object streams
    pdf = pikepdf.open(io.BytesIO(base))
    assert (
        await rejected(eng, ops, _save(pdf, object_stream_mode=pikepdf.ObjectStreamMode.generate), "p-objstm")
        == "unsupported_structure"
    )
    # rotation 270 (A04 left it pending: refused in A05)
    pdf = pikepdf.open(io.BytesIO(base))
    pdf.pages[0].obj["/Rotate"] = 270
    assert (
        await rejected(eng, ops, _save(pdf, object_stream_mode=pikepdf.ObjectStreamMode.disable), "p-rot")
        == "unsupported_structure"
    )
    # /Contents as an array
    pdf = pikepdf.open(io.BytesIO(base))
    pdf.pages[0].obj["/Contents"] = pikepdf.Array([pdf.pages[0].obj["/Contents"]])
    assert (
        await rejected(eng, ops, _save(pdf, object_stream_mode=pikepdf.ObjectStreamMode.disable), "p-arr")
        == "unsupported_structure"
    )
    # Type0 / CID font
    pdf = pikepdf.open(io.BytesIO(base))
    fo = pdf.pages[0].obj["/Resources"]["/Font"]["/F1"]
    fo["/Subtype"] = pikepdf.Name.Type0
    assert (
        await rejected(eng, ops, _save(pdf, object_stream_mode=pikepdf.ObjectStreamMode.disable), "p-t0")
        == "font_mapping_insufficient"
    )
    # text state operator outside the subset (Tc)
    pdf = pikepdf.open(io.BytesIO(base))
    pdf.pages[0].obj["/Contents"].write(b"BT /F1 12 Tf 2 Tc 72 720 Td (hola) Tj ET")
    assert (
        await rejected(eng, ops, _save(pdf, object_stream_mode=pikepdf.ObjectStreamMode.disable), "p-tc")
        == "unsupported_text_state"
    )
    # incremental update
    assert await rejected(eng, ops, base + b"\n%%EOF\n", "p-inc") == "incremental_update"
    # truncated / needs repair
    assert await rejected(eng, ops, base[: len(base) // 2], "p-trunc") in ("parser_repair", "unsupported_structure")
