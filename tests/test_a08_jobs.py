"""A08 durable operations end to end on PostgreSQL and SQLite: real storage, real isolated child, real cryptography,
real permissions; the inference of the source analysis is the A03 mock adapter itself (docs/pdf-protection.md)."""

from __future__ import annotations

import pytest

from cawbase_content_engine.protection.markers import REDACTED, STRICT

from .a08_helpers import (
    TEMPLATE,
    World,
    corpus,
    flat,
    page_texts,
    protect_case,
    read_all,
    redacted_of,
    restore_case,
    values_of,
)


@pytest.fixture
async def world(migrated, tmp_path):
    _, url = migrated
    w = World(url=url, tmp=tmp_path)
    yield w
    await w.close()


def expected_restored(case: str) -> list[str]:
    """The original page texts with every redacted value replaced by [REDACTED] (whitespace-insensitive)."""
    out = []
    for t in page_texts(corpus(case)):
        for v in redacted_of(TEMPLATE[case]):
            t = t.replace(v, REDACTED, 1) if v in t else t
        out.append(flat(t))
    return out


@pytest.mark.parametrize("case", ["L1", "C1"])
async def test_protect_download_and_restore_end_to_end(world, case):  # AC-A08-30, 37
    eng, o, fid, aid, acc, out = await protect_case(world, case)
    assert out.state == "succeeded", out
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    assert res.derivative["format"] == "pdf" and res.source["provenance"]["simulated"] is True
    assert res.zones["z2"] >= 1 and res.verification["residues"] == "passed"
    raw_report = str(res)
    for v in values_of(TEMPLATE[case]):
        assert v not in raw_report
    derivative = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    texts = page_texts(derivative)
    for v in values_of(TEMPLATE[case]):
        assert sum(t.count(v) for t in texts) <= 1  # only the permitted, non-selected occurrence (O1) may remain
    assert sum(len(STRICT.findall(t)) for t in texts) == res.counts["occurrences"]
    racc, rout = await restore_case(eng, o, derivative, res.derivative_id, acc.context_id)
    assert rout.state == "succeeded", rout
    restored = await read_all(await o.open_pdf_restored("a", racc.job.job_id))
    assert [flat(t) for t in page_texts(restored)] == expected_restored(case)
    for v in redacted_of(TEMPLATE[case]):
        assert v not in restored.decode("latin-1") and all(v not in t for t in page_texts(restored))
    rres = await o.get_pdf_restoration_result("a", racc.job.job_id)
    assert rres.counts["occurrences_restored"] == res.counts["occurrences"]


@pytest.mark.parametrize("case", ["M1", "L5", "M5"])
async def test_corpus_cases_with_the_approved_visual_criterion(world, case):  # AC-A08-30, 33 (renders: evidence suite)
    eng, o, fid, aid, acc, out = await protect_case(world, case)
    assert out.state == "succeeded", out
    res = await o.get_pdf_protection_result("a", acc.job.job_id)
    derivative = await read_all(await o.open_pdf_protected("a", acc.job.job_id))
    racc, rout = await restore_case(eng, o, derivative, res.derivative_id, acc.context_id)
    assert rout.state == "succeeded", rout
    restored = await read_all(await o.open_pdf_restored("a", racc.job.job_id))
    assert [flat(t) for t in page_texts(restored)] == expected_restored(case)


async def test_c5_fails_explicitly_and_creates_nothing(world):  # approved scope: C5 must fail (C-2B-03)
    eng, o, fid, aid, acc, out = await protect_case(world, "C5")
    info = await o.get_job("a", acc.job.job_id)
    assert info.state == "failed" and info.failure_code == "zone_plan_unjustified"
    import sqlalchemy as sa

    async with eng._engine.begin() as c:
        d, m = eng.tables.pdf_derivatives, eng.tables.protection_mappings
        assert (await c.execute(sa.select(sa.func.count()).select_from(d))).scalar_one() == 0
        assert (await c.execute(sa.select(sa.func.count()).select_from(m))).scalar_one() == 0  # refused before markers
        a = eng.tables.job_artifacts
        rows = (await c.execute(sa.select(a.c.state).where(a.c.job_id == acc.job.job_id))).all()
        assert all(r.state != "published" for r in rows)
