"""Harness of the A08 tests: REAL database (PostgreSQL and SQLite), storage, isolated child, cryptography and
permissions; ONLY the inference is simulated, with the A03 mock adapter itself (fixtures bound to the exact unit
texts of the PDF extractor v2), so the analyses carry the genuine simulated provenance that S10b requires.

The corpus (``tests/fixtures/a08``) are the native PDFs of stages 2 and 2B (LibreOffice, Chromium, macOS) with the
stage-2B manifest: the values, their contexts and their types were written BEFORE exporting, never by the extractor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cawbase_content_engine.documents import pdf_extract
from cawbase_content_engine.inference import full_coverage
from cawbase_content_engine.protection.types import ProtectionPolicy

from .a03_helpers import AE, PR, PW, fixture_for
from .a07_helpers import CAPS, LIM, SP, World, ops, read_all, run, setup_scope, upload  # noqa: F401
from .a07_helpers import FULL as A07_FULL

FX = Path(__file__).parent / "fixtures" / "a08"
MANIFEST = json.loads((FX / "manifest-2b.json").read_text(encoding="utf-8"))
PDF_ALL = [SP.PDF_PROTECT, SP.PDF_RESTORE, SP.PDF_PROTECTED_READ, SP.PDF_RESTORED_READ]
FULL = [*A07_FULL, *PDF_ALL]
#: the profile of the tests has two types; the manifest's redacted types map to ``password`` (redacted by policy)
TYPE_MAP = {"personal_data": "personal_data", "government_id": "password", "salary_compensation": "password"}
POLICY = ProtectionPolicy("pseudonymize", {"password": "redact"})
TEMPLATE = {"L1": "T1", "C1": "T1", "M1": "T1", "L5": "T5", "C5": "T5", "M5": "T5"}


def corpus(case: str) -> bytes:
    return (FX / f"{case}.pdf").read_bytes()


def units(data: bytes) -> list[dict[str, Any]]:
    return pdf_extract.extract(data, LIM)["units"]  # type: ignore[no-any-return]


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def locate(text: str, left: str, value: str, right: str) -> tuple[int, int]:
    """Offsets of ``value`` between its authored contexts (whitespace-tolerant); exactly one match."""
    norm, idx, prev = [], [], False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev:
                norm.append(" ")
                idx.append(i)
            prev = True
        else:
            norm.append(ch)
            idx.append(i)
            prev = False
    s = "".join(norm)
    pat = (re.escape(_ws(left)) + r" ?" if left else "") + "(" + re.escape(_ws(value)) + ")"
    pat += r" ?" + re.escape(_ws(right)) if right else ""
    hits = [(idx[m.start(1)], idx[m.end(1) - 1] + 1) for m in re.finditer(pat, s)]
    assert len(hits) == 1, (value, len(hits))
    return hits[0]


def findings_for(data: bytes, template: str) -> dict[str, list[tuple[str, int, int]]]:
    """unit text -> [(type_id, start, end)] of the manifest's selected occurrences (never the permitted ones)."""
    us = units(data)
    out: dict[str, list[tuple[str, int, int]]] = {u["text"]: [] for u in us if not u["empty"]}
    for s in MANIFEST["occurrences"][template]["selected"]:
        v = MANIFEST["values"][s["value_id"]]
        u = us[s["page"] - 1]
        a, b = locate(u["text"], s["left_context"], v["value"], s["right_context"])
        out[u["text"]].append((TYPE_MAP[v["type_id"]], a, b))
    return out


def values_of(template: str) -> list[str]:
    return [MANIFEST["values"][s["value_id"]]["value"] for s in MANIFEST["occurrences"][template]["selected"]]


def redacted_of(template: str) -> list[str]:
    return [
        MANIFEST["values"][s["value_id"]]["value"]
        for s in MANIFEST["occurrences"][template]["selected"]
        if MANIFEST["values"][s["value_id"]]["action"] == "redact"
    ]


def output(text: str, found: list[tuple[str, int, int]]) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "detection": {
            "status": "completed",
            "reason": None,
            "coverage": full_coverage(["password", "personal_data"], text),
            "findings": [{"type_id": t, "start": a, "end": b, "text": text[a:b]} for t, a, b in found],
        },
    }


async def mock_for(base: Any, per_text: dict[str, list[tuple[str, int, int]]], scope_id: str = "a") -> Any:
    from cawbase_content_engine.inference import MockInferenceAdapter

    o = ops(base, [PR, PW, AE], scope_id)
    fxs = []
    for i, (text, found) in enumerate(per_text.items()):
        fxs.append(
            await fixture_for(
                o, scope_id, {"kind": "output", "output": output(text, found)}, text=text, fid=f"fx{i}", caps=CAPS
            )
        )
    return MockInferenceAdapter(fxs, acknowledge_simulated=True)


async def analyse(eng: Any, data: bytes, key: str = "f1", scope_id: str = "a") -> tuple[str, str]:
    o = ops(eng, FULL, scope_id)
    fid = await upload(o, data, key, scope_id)
    acc = await o.submit_document_analysis(scope_id, file_id=fid, idempotency_key="an-" + key, capabilities=CAPS)
    out = await run(eng, acc.job.job_id)
    assert out.state == "succeeded", out
    return fid, acc.job.job_id


async def protect_case(w: World, case: str, *, context: Any = None, policy: Any = POLICY, key: str = "p1") -> Any:
    from cawbase_content_engine.pdf_protection.types import NewContext

    data = corpus(case)
    base = await setup_scope(w)
    eng = await w.engine(await mock_for(base, findings_for(data, TEMPLATE[case])))
    fid, aid = await analyse(eng, data)
    o = ops(eng, FULL)
    acc = await o.submit_pdf_protection(
        "a", analysis_job_id=aid, policy=policy, context=context or NewContext(), idempotency_key=key
    )
    out = await run(eng, acc.job.job_id)
    return eng, o, fid, aid, acc, out


async def restore_case(
    eng: Any, o: Any, derivative: bytes, did: str, context_id: str, kind: str = "job", key: str = "r1"
) -> Any:
    copy_id = await upload(o, derivative, "copy-" + key)
    racc = await o.submit_pdf_restoration(
        "a", derivative_id=did, file_id=copy_id, context_id=context_id, kind=kind, idempotency_key=key
    )
    return racc, await run(eng, racc.job.job_id)


def page_texts(data: bytes) -> list[str]:
    return [u["text"] for u in units(data)]


def flat(s: str) -> str:
    return "".join(s.split())
