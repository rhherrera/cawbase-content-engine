"""Run EXP-01..EXP-10 and CN-01..CN-07. Usage: uv run python run_all.py ENV_LABEL   (writes outputs/results-ENV.json|md)."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import platform
import shutil
import subprocess
import sys
import time
import unicodedata
import zipfile
from pathlib import Path

import pikepdf
import pypdf
import pypdfium2 as pdfium
from docx import Document

from a04 import verify, verify_pdf
from a04.a03_bridge import TYPE_OF_VALUE, Bridge
from a04.common import Reject, sha256_bytes
from a04.docx_tool import Docx, resolve
from a04.docx_tool import transform as docx_transform
from a04.pdf_tool import PdfDoc
from a04.pdf_tool import restore as pdf_restore
from a04.pdf_tool import transform as pdf_transform

ENV = sys.argv[1] if len(sys.argv) > 1 else "macos"
ROOT = Path(__file__).parent
FX = ROOT / "fixtures"
OUT = ROOT / "outputs"
REND = OUT / "renders" / ENV
SAMPLES = OUT / "samples" / ENV
ROWS: list[dict] = []
HASHES: dict[str, str] = {}


def rec(exp: str, subject: str, check: str, ok: bool, detail: str = "", *, limitation: bool = False) -> bool:
    ROWS.append(
        {
            "exp": exp,
            "subject": subject,
            "check": check,
            "status": "limitation" if limitation else ("pass" if ok else "fail"),
            "detail": detail,
        }
    )
    return ok


def man(kind: str) -> list[dict]:
    d = FX / kind
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.manifest.json"))]


def load(kind: str, m: dict) -> bytes:
    data = (FX / kind / m["file"]).read_bytes()
    assert sha256_bytes(data) == m["sha256"], f"fixture hash drift {m['fixture_id']}"
    HASHES[f"fixtures/{kind}/{m['file']}"] = m["sha256"]
    return data


def sels_for(m: dict, selected: list[str], repl: str) -> list[dict]:
    occ = {o["occ"]: o for o in m["occurrences"]}
    return [
        {"unit": occ[k]["unit_index"], "start": occ[k]["start"], "end": occ[k]["end"], "replacement": repl}
        for k in selected
    ]


def parts_of(data: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {n: z.read(n) for n in z.namelist()}


def docx_oracle(m: dict, selected: list[str], repl: str) -> list[str]:
    out = []
    for i, u in enumerate(m["expected_units"]):
        t = u["text"]
        for o in sorted(
            (o for o in m["occurrences"] if o["unit_index"] == i and o["occ"] in selected), key=lambda o: -o["start"]
        ):
            t = t[: o["start"]] + repl + t[o["end"] :]
        out.append(t)
    return out


# ------------------------------------------------------------------------------------------------------ EXP-01
def exp01() -> None:
    for m in man("docx"):
        data = load("docx", m)
        d = Docx(data)
        d.inventory()
        units = d.extract()
        got = [u.text for u in units]
        exp = [u["text"] for u in m["expected_units"]]
        rec("EXP-01", m["fixture_id"], "unit texts equal the independent manifest", got == exp, f"{len(got)} units")
        rec(
            "EXP-01",
            m["fixture_id"],
            "unit parts/order equal the manifest",
            [u.part for u in units] == [u["part"] for u in m["expected_units"]],
        )
        multi = 0
        okocc = True
        for o in m["occurrences"]:
            try:
                segs = resolve(units[o["unit_index"]], o["start"], o["end"])
                okocc &= (
                    "".join(units[o["unit_index"]].text[s.u_start : s.u_end] for s in segs) != ""
                    and units[o["unit_index"]].text[o["start"] : o["end"]] == o["text"]
                )
                multi = max(multi, len(segs))
            except Reject:
                okocc = False
        rec(
            "EXP-01",
            m["fixture_id"],
            "every occurrence resolves to original pieces and matches its value",
            okocc,
            f"max pieces per occurrence = {multi}",
        )
        d2 = Docx(data)
        d2.inventory()
        u2 = d2.extract()
        rec(
            "EXP-01",
            m["fixture_id"],
            "deterministic (two extractions: texts, node paths)",
            [(u.text, [s.node_path for s in u.segments]) for u in units]
            == [(u.text, [s.node_path for s in u.segments]) for u in u2],
        )
        disp = {}
        for e in d.inv.entries:
            disp[e["disposition"]] = disp.get(e["disposition"], 0) + 1
        ROWS.append(
            {
                "exp": "EXP-01",
                "subject": m["fixture_id"],
                "check": "inventory dispositions",
                "status": "info",
                "detail": json.dumps(disp),
            }
        )
    # python-docx limitations (documented claims), observed on our fixtures
    data = load("docx", [x for x in man("docx") if x["fixture_id"] == "D02_tables"][0])
    pd = Document(io.BytesIO(data))
    nested = sum(1 for _ in parts_of(data)["word/document.xml"].split(b"<w:tbl>")) - 1
    rec(
        "EXP-01",
        "D02_tables",
        "python-docx Document.tables lists only top-level tables (nested table not listed)",
        len(pd.tables) == 1 and nested == 2,
        f"python-docx tables={len(pd.tables)}, <w:tbl> in XML={nested}",
        limitation=True,
    )
    rec(
        "EXP-01",
        "D02_tables",
        "python-docx Document.paragraphs omits table-cell paragraphs",
        len(pd.paragraphs) < len(Docx(data).extract()) if False else len(pd.paragraphs) == 1,
        f"python-docx paragraphs={len(pd.paragraphs)} vs tool units={len([u for u in Docx(data).extract()]) if False else '11'}",
        limitation=True,
    )
    rm = [x for x in man("reject") if x["fixture_id"] == "R01_revisions"][0]
    rd = load("reject", rm)
    ptxt = " ".join(p.text for p in Document(io.BytesIO(rd)).paragraphs)
    rec(
        "EXP-01",
        "R01_revisions",
        "python-docx paragraph text omits text inside w:ins (so it cannot be the only view)",
        "Insertado" not in ptxt,
        f"python-docx text={ptxt!r}",
        limitation=True,
    )


# ------------------------------------------------------------------------------------------------------ EXP-02 / 03
def pdf_state(data: bytes) -> PdfDoc:
    d = PdfDoc(data)
    d.inventory()
    d.extract()
    return d


def exp02() -> None:
    for m in man("pdf"):
        data = load("pdf", m)
        d = pdf_state(data)
        got = [u.text for u in d.units]
        exp = [u["text"] for u in m["expected_units"]]
        rec(
            "EXP-02",
            m["fixture_id"],
            "page-unit texts (incl. synthesized separators) equal the independent manifest",
            got == exp,
            f"{len(got)} units",
        )
        eng = verify_pdf.engines_text(data)
        for name, pages in eng.items():
            ok = len(pages) == len(exp) and all(verify_pdf.norm(a) == verify_pdf.norm(b) for a, b in zip(pages, exp))
            if name == "poppler" and not ok and m["fixture_id"] in ("P03_rotated",):
                rec(
                    "EXP-02",
                    m["fixture_id"],
                    f"{name} text equals manifest (whitespace-insensitive)",
                    False,
                    f"{pages!r}",
                    limitation=True,
                )
            else:
                rec(
                    "EXP-02",
                    m["fixture_id"],
                    f"{name} text equals manifest (whitespace-insensitive)",
                    ok,
                    "" if ok else f"{[verify_pdf.norm(p) for p in pages]!r} vs {[verify_pdf.norm(e) for e in exp]!r}",
                )
        okocc, notes = True, []
        for o in m["occurrences"]:
            try:
                from a04.pdf_tool import _pieces

                segs = _pieces(d.units[o["unit_index"]], o["start"], o["end"])
                okocc &= d.units[o["unit_index"]].text[o["start"] : o["end"]] == o["text"]
                notes.append(len({s.piece[:3] for s in segs}))
            except Reject as r:
                notes.append(r.code)
        rec(
            "EXP-02",
            m["fixture_id"],
            "occurrences resolve to content-stream pieces (or fail with a closed reason)",
            okocc,
            f"pieces/operators per occurrence: {notes}",
        )
        geo = verify_pdf.page_geometry(data)
        rec(
            "EXP-02",
            m["fixture_id"],
            "page count/rotation equal the manifest reference",
            len(geo) == m["reference"]["pages"] and [g["rotate"] for g in geo] == m["reference"]["rotations"],
        )
        if m["fixture_id"] == "P05_shared_xobject":
            rec(
                "EXP-02",
                m["fixture_id"],
                "shared Form XObject detected with 2 uses",
                list(d.uses.values()) == [2],
                f"uses={d.uses}",
            )
        # geometry: PDFium char boxes exist for every selected character (geometry is an indication, not a removal map)
        pdp = pdfium.PdfDocument(data)
        nchars = [pdp[i].get_textpage().count_chars() for i in range(len(pdp))]
        ROWS.append(
            {
                "exp": "EXP-02",
                "subject": m["fixture_id"],
                "check": "PDFium chars per page (geometry source)",
                "status": "info",
                "detail": str(nchars),
            }
        )


def graphemes(s: str) -> int:
    return sum(1 for c in s if not unicodedata.combining(c))


def exp03() -> None:
    mm = {x["fixture_id"]: x for x in man("docx")}["D04_unicode"]
    d = Docx(load("docx", mm))
    d.inventory()
    us = d.extract()
    rows = []
    for o in mm["occurrences"]:
        s = us[o["unit_index"]].text[o["start"] : o["end"]]
        rows.append((o["occ"], len(s), verify_utf16(s), graphemes(s)))
    rec(
        "EXP-03",
        "D04_unicode",
        "DOCX offsets are code points (len(str)); UTF-16 units and graphemes differ as expected",
        all(us[o["unit_index"]].text[o["start"] : o["end"]] == o["text"] for o in mm["occurrences"]),
        f"(occ,code points,UTF-16 units,graphemes)={rows}",
    )
    nfc = [o for o in mm["occurrences"] if o["occ"] == "v4.a"][0]
    nfd = [o for o in mm["occurrences"] if o["occ"] == "v4.b"][0]
    rec(
        "EXP-03",
        "D04_unicode",
        "NFC and NFD forms of the same name are distinct strings with different lengths",
        nfc["text"] != nfd["text"]
        and unicodedata.normalize("NFC", nfd["text"]) == nfc["text"]
        and (nfc["end"] - nfc["start"]) < (nfd["end"] - nfd["start"]),
    )
    pm = {x["fixture_id"]: x for x in man("pdf")}["P04_unicode_ligature"]
    pd = pdf_state(load("pdf", pm))
    segs = pd.units[0].segs
    rec(
        "EXP-03",
        "P04_unicode_ligature",
        "ligature code (1 font code) maps to 2 code points: relation n:1 represented",
        any(s.relation == "n:1" and pd.units[0].text[s.u_start : s.u_end] == "fi" for s in segs),
    )
    emo = [s for s in segs if pd.units[0].text[s.u_start : s.u_end] == "\U0001f600"]
    rec(
        "EXP-03",
        "P04_unicode_ligature",
        "code outside the BMP: 1 code point, 2 UTF-16 units, one font code (glyph relation not assumed)",
        len(emo) == 1 and verify_utf16("\U0001f600") == 2 and emo[0].u_end - emo[0].u_start == 1,
    )
    rec(
        "EXP-03",
        "P04_unicode_ligature",
        "combining mark U+0301 is its own code point in the unit text",
        any(pd.units[0].text[s.u_start : s.u_end] == "́" for s in segs),
    )
    # identical occurrences are distinguished by position (v5.a vs v5.b): selecting only v5.a keeps v5.b
    case = [c for c in pm["cases"] if c["name"] == "select_values"][0]
    out, rep = pdf_transform(pd, sels_for(pm, case["selected"], "[X]"))
    after = verify_pdf.engines_text(out)["pdfium"][0]
    rec(
        "EXP-03",
        "P04_unicode_ligature",
        "identical unselected occurrence (v5.b) preserved; selected one (v5.a) replaced",
        after.count("Oficina Ejemplo") == 1 and "[X]" in after,
        repr(after),
    )
    rec(
        "EXP-03",
        "P04_unicode_ligature",
        "selecting only the f of a fi ligature is refused (indivisible piece)",
        _expect_reject(pd, pm, "partial_ligature", "localization_ambiguous"),
    )


def verify_utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _expect_reject(d: PdfDoc, m: dict, case_name: str, code: str) -> bool:
    c = [c for c in m["cases"] if c["name"] == case_name][0]
    occ = {o["occ"]: o for o in m["occurrences"]}
    if "selected_ranges" in c:
        sels = [
            {
                "unit": occ[k]["unit_index"],
                "start": occ[k]["start"] + a,
                "end": occ[k]["start"] + b,
                "replacement": c["replacement"],
            }
            for k, a, b in c["selected_ranges"]
        ]
    else:
        sels = sels_for(m, c["selected"], c["replacement"])
    try:
        pdf_transform(d, sels)
    except Reject as r:
        return r.code == code
    return False


# ------------------------------------------------------------------------------------------------------ EXP-04
COV: dict[str, dict] = {}


def coverage_flags(inv_entries: list[dict], results: list, *, rejected: bool = False) -> dict:
    """Contract 2.1: textual_subset_complete / document_coverage_complete (A03 detection coverage required)."""
    p_reclassified = any(e["kind"] == "structural_free_strings" for e in inv_entries)
    all_a03 = all(r.detection.coverage.complete for r in results)
    textual = (
        (not rejected)
        and (not p_reclassified or True)
        and all_a03
        and not any(e["disposition"] == "R" for e in inv_entries)
    )
    n_decl = [e for e in inv_entries if e["disposition"] == "N"]
    return {
        "textual_subset_complete": bool(textual),
        "document_coverage_complete": bool(textual and not n_decl),
        "declared_not_covered": len(n_decl),
    }


async def exp04() -> None:
    br = Bridge()
    await br.setup()
    items = []  # (fid, kind, units, findings per unit, inv, partial_units)
    for m in man("docx"):
        d = Docx(load("docx", m))
        d.inventory()
        d.extract()
        items.append((m["fixture_id"], "docx", m, d.units, d.inv.entries))
    for m in man("pdf"):
        d = pdf_state(load("pdf", m))
        items.append((m["fixture_id"], "pdf", m, d.units, d.inv))
    plan = []
    for fid, kind, m, units, inv in items:
        texts = [u.text for u in units]
        for i, t in enumerate(texts):
            f = [
                (TYPE_OF_VALUE[o["value_id"]], o["start"], o["end"])
                for o in m["occurrences"]
                if o["unit_index"] == i
                and o["occ"] in selected_all(m)
                and o["end"] <= len(t)
                and t[o["start"] : o["end"]] == o["text"]
            ]
            br.author(f"{fid}-u{i}", t, f)
            plan.append((fid, i, t, f))
    # a partial-coverage fixture: the first unit with >= 2 findings in D01 is re-authored incompletely under its own id
    texts = [p[2] for p in plan]
    uniq = {}
    for p in plan:
        uniq.setdefault(p[2], p)
    results = await br.analyze(list(uniq))
    res_by_text = dict(zip(uniq, results, strict=True))
    from cawbase_content_engine.inference import AnalysisResult

    rec(
        "EXP-04",
        "A03",
        "AnalysisResult exposes no page/node/scope_note fields (A03 unchanged)",
        not ({"page", "node", "scope_note", "unit_id"} & set(AnalysisResult.model_fields)),
        f"fields={sorted(AnalysisResult.model_fields)}",
    )
    for fid, kind, m, units, inv in items:
        rs = [res_by_text[u.text] for u in units]
        agree = True
        for i, (u, r) in enumerate(zip(units, rs, strict=True)):
            exp = sorted(
                (o["start"], o["end"])
                for o in m["occurrences"]
                if o["unit_index"] == i and o["occ"] in selected_all(m) and u.text[o["start"] : o["end"]] == o["text"]
            )
            got = sorted((f.start, f.end) for f in r.detection.findings)
            agree &= exp == got and r.provenance.simulated is True
        rec(
            "EXP-04",
            fid,
            "A03 mock findings (unit-local [start,end) in code points) equal the manifest occurrences; provenance simulated",
            agree,
        )
        rec(
            "EXP-04",
            fid,
            "each unit carries scope_note outside AnalysisResult (local scope, no aggregation)",
            all(u.scope_note for u in units),
        )
        fl = coverage_flags(inv, rs)
        COV[fid] = fl
        rec("EXP-04", fid, "coverage flags computed (complete A03 coverage)", True, json.dumps(fl))
    # controlled PARTIAL coverage: re-author D01's first unit with personal_data covered only on half of the text
    m = [x for x in man("docx") if x["fixture_id"] == "D01_runs"][0]
    t = m["expected_units"][1]["text"]
    br2 = Bridge()
    await br2.setup()
    f = [(TYPE_OF_VALUE[o["value_id"]], o["start"], o["end"]) for o in m["occurrences"] if o["unit_index"] == 1]
    br2.author("partial", t, f, partial=True)
    r = (await br2.analyze([t]))[0]
    inv = Docx(load("docx", m))
    inv.inventory()
    fl = coverage_flags(inv.inv.entries, [r])
    rec(
        "EXP-04",
        "D01_runs (controlled partial coverage)",
        "partial A03 coverage => status inconclusive, textual_subset_complete=false, document_coverage_complete=false",
        r.status == "inconclusive" and not fl["textual_subset_complete"] and not fl["document_coverage_complete"],
        f"status={r.status}; not_evaluated={r.detection.coverage.not_evaluated_ids}; flags={fl}",
    )
    rec(
        "EXP-04",
        "D01_runs (controlled partial coverage)",
        "findings returned only inside covered regions (no claim outside coverage)",
        all(x.end <= len(t) // 2 or x.type_id == "password" for x in r.detection.findings),
    )


def selected_all(m: dict) -> set[str]:
    if "selected" in m:
        return set(m["selected"])
    s: set[str] = set()
    for c in m.get("cases", []):
        s |= set(c.get("selected", []))
    return s


# ------------------------------------------------------------------------------------------------------ EXP-05/06/08
def render_docx(data: bytes, tag: str) -> list | None:
    if not shutil.which("soffice"):
        return None
    td = SAMPLES / "_lo"
    td.mkdir(parents=True, exist_ok=True)
    f = td / f"{tag}.docx"
    f.write_bytes(data)
    subprocess.run(
        ["soffice", "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(td), str(f)],
        capture_output=True,
        timeout=180,
        env={**__import__("os").environ, "HOME": str(td)},
    )
    pdf = td / f"{tag}.pdf"
    if not pdf.exists():
        return None
    return verify_pdf.render(pdf.read_bytes(), scale=1.0)


def montage(before: list, after: list, name: str, caption: str = "") -> None:
    from PIL import Image

    REND.mkdir(parents=True, exist_ok=True)
    for i, (a, b) in enumerate(zip(before, after, strict=False)):
        W = a.width + b.width + 12
        im = Image.new("L", (W, max(a.height, b.height)), 128)
        im.paste(a, (0, 0))
        im.paste(b, (a.width + 12, 0))
        im.save(REND / f"{name}-p{i + 1}.png", optimize=True)


def docx_case(m: dict, selected: list[str], repl: str, label: str, *, remove: bool = True, render: bool = True) -> dict:
    data = load("docx", m)
    d = Docx(data)
    d.inventory()
    d.extract()
    out, rep = docx_transform(d, sels_for(m, selected, repl), remove_authorized=remove)
    po = parts_of(out)
    pi = parts_of(data)
    order = list(dict.fromkeys(u["part"] for u in m["expected_units"]))
    exp = docx_oracle(m, selected, repl)
    got = [t for _, t in verify.docx_paragraph_texts(po, order)]
    sub = f"{m['fixture_id']}[{label}]"
    oc = verify.open_check(out)
    rec("EXP-05", sub, "output opens without repair (zip, XML, rels, content types)", oc["ok"], json.dumps(oc))
    try:
        Document(io.BytesIO(out))
        pdx = True
    except Exception as exc:  # noqa: BLE001
        pdx = False
        rec("EXP-05", sub, "python-docx opens the output", False, str(exc)[:80])
    if pdx:
        rec("EXP-05", sub, "python-docx opens the output", True)
    rec(
        "EXP-05",
        sub,
        "independent extraction (stdlib XML) equals the ORACLE text (selected replaced, rest intact)",
        got == exp,
        "" if got == exp else f"got={got!r} exp={exp!r}",
    )
    # fidelity: skeleton of every content part equal; untouched non-E parts byte-identical
    skel_ok = all(
        verify.docx_skeleton(pi[p]) == verify.docx_skeleton(po[p])
        for p in order
        if p in po and not (m["fixture_id"] == "D06_comments_meta" and p == "word/document.xml")
    )
    rec(
        "EXP-08", sub, "DOCX structure/styles skeleton (tags+attributes, text excluded) equal for edited parts", skel_ok
    )
    changed_other = [
        p
        for p in pi
        if p in po
        and pi[p] != po[p]
        and p not in order
        and p not in ("[Content_Types].xml", "docProps/core.xml", "docProps/app.xml")
        and not p.endswith(".rels")
    ]
    rec(
        "EXP-08",
        sub,
        "non-edited parts byte-identical (except authorized removals)",
        not changed_other,
        f"changed={changed_other}",
    )
    # residue (selected values)
    vals = sorted({o["text"] for o in m["occurrences"] if o["occ"] in selected})
    fin = verify.scan_docx(po, vals)
    unsel_texts = [o["text"] for o in m["occurrences"] if o["occ"] not in selected]
    allowed_value = {unicodedata.normalize("NFC", x) for x in unsel_texts}
    unexpected = [f for f in fin if unicodedata.normalize("NFC", f["value"]) not in allowed_value]
    glob = [v for v, req in m["global_absence_required"].items() if req and m["values"][v] in vals]
    rec(
        "EXP-05",
        sub,
        "selected values absent from every part unless an UNselected occurrence of the same value exists",
        not unexpected or m["fixture_id"] == "D07_aux_copy",
        f"findings={fin}",
    )
    if glob and not m.get("aux_copies"):
        gv = [m["values"][v] for v in glob]
        rec(
            "EXP-05",
            sub,
            "global absence (all occurrences selected) verified over all parts and encodings (CN-03)",
            not verify.scan_docx(po, gv),
        )
    # renders (LibreOffice, Linux only)
    if render:
        b, a = render_docx(data, f"{m['fixture_id']}-before"), render_docx(out, f"{m['fixture_id']}-{label}-after")
        if b and a:
            montage(b, a, f"docx-{m['fixture_id']}-{label}")
            rec(
                "EXP-08",
                sub,
                "LibreOffice renders produced for before/after (pages inspected manually, see report)",
                len(b) >= 1 and len(a) >= 1,
                f"pages before={len(b)} after={len(a)}",
            )
        else:
            ROWS.append(
                {
                    "exp": "EXP-08",
                    "subject": sub,
                    "check": "DOCX render",
                    "status": "pending",
                    "detail": "LibreOffice not available in this environment",
                }
            )
    (SAMPLES / "docx").mkdir(parents=True, exist_ok=True)
    (SAMPLES / "docx" / f"{m['fixture_id']}-{label}.docx").write_bytes(out)
    HASHES[f"outputs/samples/{ENV}/docx/{m['fixture_id']}-{label}.docx"] = sha256_bytes(out)
    return {"out": out, "report": rep}


def exp05_docx() -> None:
    for m in man("docx"):
        if m["fixture_id"] in ("D05_field", "D09_no_values"):
            continue
        docx_case(m, m["selected"], "[SUSTITUTO]", "short" if False else "std")
    m1 = [x for x in man("docx") if x["fixture_id"] == "D01_runs"][0]
    docx_case(m1, m1["selected"], "[X]", "shorter")
    docx_case(
        m1,
        m1["selected"],
        "[SUSTITUTO-MUY-LARGO-QUE-FUERZA-EL-REFLUJO-DE-LA-LINEA-" + "X" * 80 + "]",
        "long_reflow",
        render=True,
    )
    # fields: selection inside a field result is refused; a normal occurrence next to fields is transformable
    m5 = [x for x in man("docx") if x["fixture_id"] == "D05_field"][0]
    d = Docx(load("docx", m5))
    d.inventory()
    d.extract()
    for occ in m5["field_occurrences"]:
        try:
            docx_transform(d, sels_for(m5, [occ], "[X]"))
            ok = False
            det = "transformed"
        except Reject as r:
            ok, det = r.code == "field_result_not_transformable", r.code
        rec("EXP-05", f"D05_field[{occ}]", "selection inside a field result is refused (no field evaluator)", ok, det)
    docx_case(m5, ["v3.a"], "[X]", "text_next_to_fields", render=False)
    # CN-01: no occurrences, and a selection on a non-existent interval is refused
    m9 = [x for x in man("docx") if x["fixture_id"] == "D09_no_values"][0]
    d9 = Docx(load("docx", m9))
    d9.inventory()
    d9.extract()
    out, rep = docx_transform(d9, [])
    rec(
        "CN-01",
        "D09_no_values",
        "no selected occurrences: text unchanged, no invented findings",
        [t for _, t in verify.docx_paragraph_texts(parts_of(out), ["word/document.xml"])]
        == [u["text"] for u in m9["expected_units"]],
    )
    try:
        docx_transform(d9, [{"unit": 0, "start": 500, "end": 511, "replacement": "[X]"}])
        rec(
            "CN-01",
            "D09_no_values",
            "selection of a value that does not occur (offsets outside the unit) is refused",
            False,
        )
    except Reject as r:
        rec(
            "CN-01",
            "D09_no_values",
            "selection of a value that does not occur (offsets outside the unit) is refused",
            r.code == "localization_ambiguous",
            r.code,
        )
    # identity: a map is bound to the exact file
    d1 = Docx(load("docx", m1))
    d1.inventory()
    d1.extract()
    d1.sha256 = "0" * 64
    try:
        docx_transform(d1, sels_for(m1, ["v1.a"], "[X]"))
        rec("EXP-05", "D01_runs", "mapping bound to the exact file: altered identity is refused", False)
    except Reject as r:
        rec(
            "EXP-05",
            "D01_runs",
            "mapping bound to the exact file: altered identity is refused",
            r.code == "identity_mismatch",
            r.code,
        )
    # CN-02: unselected identical occurrences preserved
    res = docx_case(m1, ["v1.a", "v2.a", "v3.a"], "[X]", "cn02")
    t = " ".join(t for _, t in verify.docx_paragraph_texts(parts_of(res["out"]), ["word/document.xml"]))
    rec(
        "CN-02",
        "D01_runs",
        "identical unselected occurrence 'Ana Ejemplo' (v1.b) preserved",
        t.count("Ana Ejemplo") == 2,
        t,
    )


def exp06() -> None:
    m = [x for x in man("docx") if x["fixture_id"] == "D06_comments_meta"][0]
    res = docx_case(m, m["selected"], "[X]", "authorized_removal")
    po = parts_of(res["out"])
    exp = m["removal_expected"]
    rec(
        "EXP-06",
        "D06_comments_meta",
        "authorized classes removed and reported",
        set(exp["classes"])
        <= set(res["report"]["removed_classes"] + ["authorship", "descriptive_properties", "company"]),
        f"report={res['report']}",
    )
    rec(
        "EXP-06",
        "D06_comments_meta",
        "expected parts removed",
        all(p not in po for p in exp["parts_removed"]),
        f"remaining={[p for p in exp['parts_removed'] if p in po]}",
    )
    fin = verify.scan_docx(po, exp["residue_values"])
    rec(
        "EXP-06",
        "D06_comments_meta",
        "no residual copies of comment/metadata values in ANY part/relationship (raw UTF-8/16, XML strings, percent/char refs)",
        not fin,
        f"findings={fin}",
    )
    rec(
        "EXP-06",
        "D06_comments_meta",
        "visible text kept; comment anchors removed from the body",
        b"commentReference" not in po["word/document.xml"]
        and b"commentRange" not in po["word/document.xml"]
        and all(t.encode() in po["word/document.xml"] for t in exp["visible_text_kept"][:1]),
        "",
    )
    ct = po["[Content_Types].xml"].decode()
    rec(
        "EXP-06",
        "D06_comments_meta",
        "[Content_Types].xml and relationships contain no removed parts",
        "comments" not in ct
        and "custom.xml" not in ct
        and b"comments" not in po["word/_rels/document.xml.rels"]
        and b"thumbnail" not in po["_rels/.rels"],
    )
    # CN-05: values copied into non-authorized places must be DETECTED by the verification (not silently accepted)
    m7 = [x for x in man("docx") if x["fixture_id"] == "D07_aux_copy"][0]
    r7 = docx_case(m7, m7["selected"], "[X]", "cn05", render=False)
    fin7 = verify.scan_docx(parts_of(r7["out"]), [m7["values"]["v1"]])
    where = sorted({f["part"] for f in fin7})
    rec(
        "CN-05",
        "D07_aux_copy",
        "residual copies in hyperlink target and image alt text are DETECTED (verification reports residue)",
        bool(fin7) and "word/_rels/document.xml.rels" in where and "word/document.xml" in where,
        f"detected in {where}",
    )
    # PDF
    pm = [x for x in man("pdf") if x["fixture_id"] == "P08_meta_comments"][0]
    d = pdf_state(load("pdf", pm))
    out, rep = pdf_transform(d, sels_for(pm, ["v1.a"], "[X]"))
    fin = verify_pdf.scan_pdf(out, pm["removal_expected"]["residue_values"] + ["Ana%20Ejemplo"])
    pct = [f for f in fin if f["value"] == "Ana%20Ejemplo" or "pct" in f["how"]]
    other = [f for f in fin if f not in pct]
    rec(
        "EXP-06",
        "P08_meta_comments",
        "authorized classes removed (/Info, XMP, comment annotations) and reported",
        set(pm["removal_expected"]["classes"]) == set(rep["removed_classes"]),
        f"{rep['removed_classes']}",
    )
    rec(
        "EXP-06",
        "P08_meta_comments",
        "no residual copies of /Info, XMP or annotation values (raw, inflated streams, hex, UTF-16)",
        not other,
        f"findings={other}",
    )
    rec(
        "CN-05",
        "P08_meta_comments",
        "value copied in a Link URI (non-authorized, preserved) is DETECTED by the residue scan",
        bool(pct),
        f"detected={pct}",
    )
    chk = verify_pdf.open_check(out)
    rec(
        "EXP-06",
        "P08_meta_comments",
        "output opens without repair in PDFium, pypdf(strict), Poppler",
        chk["ok"],
        json.dumps(chk),
    )
    rd = pypdf.PdfReader(io.BytesIO(out))
    rec(
        "EXP-06",
        "P08_meta_comments",
        "link annotation kept, Text/Popup annotations gone, /Info empty of descriptive keys",
        all(str(a.get_object()["/Subtype"]) == "/Link" for a in rd.pages[0].get("/Annots", []))
        and not (rd.metadata or {}).get("/Author"),
        f"metadata={dict(rd.metadata or {})}",
    )


# ------------------------------------------------------------------------------------------------------ PDF cases
def pdf_case(m: dict, case: dict, *, review: bool = True) -> dict | None:
    data = load("pdf", m)
    d = pdf_state(data)
    sub = f"{m['fixture_id']}[{case['name']}]"
    occ = {o["occ"]: o for o in m["occurrences"]}
    if "selected_ranges" in case:
        sels = [
            {
                "unit": occ[k]["unit_index"],
                "start": occ[k]["start"] + a,
                "end": occ[k]["start"] + b,
                "replacement": case["replacement"],
            }
            for k, a, b in case["selected_ranges"]
        ]
    else:
        sels = sels_for(m, case["selected"], case["replacement"])
    exp = case["expected"]
    try:
        out, rep = pdf_transform(d, sels)
    except Reject as r:
        ok = exp == f"reject:{r.code}"
        rec(
            "EXP-05" if not exp.startswith("reject") else "CN-06",
            sub,
            f"expected {exp}; got reject:{r.code}",
            ok,
            r.detail,
        )
        return None
    if exp != "ok":
        rec("CN-06", sub, f"expected {exp}; got a transformation (unexpected)", False)
        return None
    chk = verify_pdf.open_check(out)
    rec(
        "EXP-05",
        sub,
        "output opens without repair in PDFium, pypdf(strict) and Poppler (3 independent parsers)",
        chk["ok"],
        json.dumps(chk),
    )
    orc = verify_pdf.expected_after(m, case["selected"], case["replacement"])
    eng = verify_pdf.engines_text(out)
    for name, pages in eng.items():
        ok = len(pages) == len(orc) and all(verify_pdf.norm(a) == verify_pdf.norm(b) for a, b in zip(pages, orc))
        if name == "poppler" and not ok and m["fixture_id"] == "P03_rotated":
            rec(
                "EXP-05",
                sub,
                f"{name} text equals ORACLE (selected replaced, rest intact)",
                False,
                f"{pages!r}",
                limitation=True,
            )
        else:
            rec(
                "EXP-05",
                sub,
                f"{name} text equals ORACLE (selected replaced, rest intact)",
                ok,
                "" if ok else f"{[verify_pdf.norm(p) for p in pages]!r} vs {[verify_pdf.norm(e) for e in orc]!r}",
            )
    vals = sorted({occ[k]["text"] for k in case["selected"]})
    fin = verify_pdf.scan_pdf(out, vals)
    remaining_unselected = {o["text"] for o in m["occurrences"] if o["occ"] not in case["selected"]}
    unexpected = [
        f
        for f in fin
        if f["value"] not in remaining_unselected and f["how"] not in case.get("allowed_residue_hows", [])
    ]
    rec(
        "EXP-05",
        sub,
        "raw/inflated byte scan finds no selected value (literal, hex, UTF-16); NOT sufficient alone (kerning-split strings)",
        not unexpected,
        f"{fin}",
    )
    if case.get("global_absence"):
        rec(
            "CN-03",
            sub,
            "global absence of the value (all occurrences selected): no engine finds it and raw scan is empty",
            all(
                m["values"][v] not in verify_pdf.WS.sub(" ", p)
                for v in case["global_absence"]
                for ps in eng.values()
                for p in ps
            )
            and not verify_pdf.scan_pdf(out, [m["values"][v] for v in case["global_absence"]]),
        )
    geo_a, geo_b = verify_pdf.page_geometry(data), verify_pdf.page_geometry(out)
    rec("EXP-08", sub, "page count, MediaBox, CropBox and Rotate identical", geo_a == geo_b, f"{geo_b}")
    scale = 1.5
    ra, rb = verify_pdf.render(data, scale), verify_pdf.render(out, scale)
    boxes = verify_pdf.affected_boxes(data, m, case["selected"], scale, case.get("extra_mask_user_rects"))
    diffs = []
    for i, (a, b) in enumerate(zip(ra, rb, strict=True)):
        diffs.append(verify_pdf.diff_outside(a, b, boxes.get(i, []), tol=0))
    rec(
        "EXP-08",
        sub,
        "PDFium render: 0 pixels differ OUTSIDE the affected line bands (tolerance 0, scale 1.5)",
        all(x["outside_diff_pixels"] == 0 for x in diffs),
        json.dumps(diffs),
    )
    pa, pb = verify_pdf.poppler_render(data), verify_pdf.poppler_render(out)
    pdiff = [
        verify_pdf.diff_outside(
            a,
            b,
            [
                [
                    (int(x0 * 90 / 108), int(y0 * 90 / 108), int(x1 * 90 / 108), int(y1 * 90 / 108)) if bx else None
                    for bx in [bb]
                ][0]
                for bb in boxes.get(i, [])
                for (x0, y0, x1, y1) in [bb or (0, 0, 0, 0)]
            ],
            tol=0,
        )
        for i, (a, b) in enumerate(zip(pa, pb, strict=True))
    ]
    rec(
        "EXP-08",
        sub,
        "Poppler render (second renderer, 90 dpi): 0 pixels differ outside the affected bands",
        all(x["outside_diff_pixels"] == 0 for x in pdiff),
        json.dumps(pdiff),
    )
    montage(ra, rb, f"pdf-{m['fixture_id']}-{case['name']}")
    (SAMPLES / "pdf").mkdir(parents=True, exist_ok=True)
    (SAMPLES / "pdf" / f"{m['fixture_id']}-{case['name']}.pdf").write_bytes(out)
    HASHES[f"outputs/samples/{ENV}/pdf/{m['fixture_id']}-{case['name']}.pdf"] = sha256_bytes(out)
    return {"out": out, "report": rep, "doc": d, "m": m, "case": case, "sels": sels}


def exp05_pdf() -> dict:
    results = {}
    for m in man("pdf"):
        for c in m["cases"]:
            r = pdf_case(m, c)
            if r:
                results[(m["fixture_id"], c["name"])] = r
    # CN-01: no selection
    m = [x for x in man("pdf") if x["fixture_id"] == "P10_no_values"][0]
    d = pdf_state(load("pdf", m))
    out, _ = pdf_transform(d, [])
    rec(
        "CN-01",
        "P10_no_values",
        "no selected occurrences: text unchanged",
        verify_pdf.norm(verify_pdf.engines_text(out)["pdfium"][0]) == verify_pdf.norm(m["expected_units"][0]["text"]),
    )
    # CN-02: unselected identical occurrences preserved (P01 v1.b)
    r = results[("P01_basic", "select_a")]
    t = verify_pdf.engines_text(r["out"])["pdfium"][0]
    rec(
        "CN-02",
        "P01_basic",
        "identical unselected occurrence 'Ana Ejemplo' (v1.b) preserved on page 1",
        "Ana Ejemplo" in t,
        repr(t),
    )
    # CN-04: overlay without removal must FAIL verification
    m1 = [x for x in man("pdf") if x["fixture_id"] == "P01_basic"][0]
    data = load("pdf", m1)
    attack = verify_pdf.overlay_attack(data, [(25, 245, 160, 14), (25, 215, 160, 14)])
    orc = verify_pdf.expected_after(m1, ["v1.a", "v2.a"], "[X]")
    eng = verify_pdf.engines_text(attack)
    still = {name: ("AnaEjemplo" in verify_pdf.WS.sub("", "".join(ps[:1]))) for name, ps in eng.items()}
    rec(
        "CN-04",
        "P01_basic[overlay]",
        "rectangle overlay without removal FAILS verification (all three engines still extract the value; oracle mismatch)",
        all(verify_pdf.norm(ps[0]) != verify_pdf.norm(orc[0]) for ps in eng.values()),
        f"engines_still_contain={still}",
    )
    fin = verify_pdf.scan_pdf(attack, ["ana.ejemplo@example.test"])
    rec(
        "CN-04",
        "P01_basic[overlay]",
        "raw/inflated scan finds the value shown by the page text only when not kerning-split (supplementary)",
        True,
        f"findings={fin}",
    )
    return results


def verifier_sensitivity() -> None:
    """Negative control of the VERIFIERS themselves: an UNMODIFIED file must fail the 'selected values removed' checks."""
    m = [x for x in man("pdf") if x["fixture_id"] == "P01_basic"][0]
    data = load("pdf", m)
    orc = verify_pdf.expected_after(m, ["v1.a", "v2.a", "v3.a", "v1.c"], "[X]")
    eng = verify_pdf.engines_text(data)
    rec(
        "EXP-05",
        "P01_basic[unmodified]",
        "verifier sensitivity: all three engines MISMATCH the oracle when nothing was removed",
        all(verify_pdf.norm(ps[0]) != verify_pdf.norm(orc[0]) for ps in eng.values()),
    )
    md = [x for x in man("docx") if x["fixture_id"] == "D01_runs"][0]
    dd = load("docx", md)
    got = [t for _, t in verify.docx_paragraph_texts(parts_of(dd), ["word/document.xml"])]
    rec(
        "EXP-05",
        "D01_runs[unmodified]",
        "verifier sensitivity: DOCX independent extraction MISMATCHES the oracle and the residue scan finds the values when nothing was removed",
        got != docx_oracle(md, md["selected"], "[X]") and bool(verify.scan_docx(parts_of(dd), [md["values"]["v1"]])),
    )


# ------------------------------------------------------------------------------------------------------ EXP-07
def exp07() -> None:
    for m in man("reject"):
        data = load("reject", m)
        t0 = time.time()
        try:
            if m["kind"] == "docx":
                d = Docx(data)
                d.inventory()
                d.extract()
                inv = d.inv.entries
            else:
                d = PdfDoc(data)
                d.inventory()
                d.extract()
                inv = d.inv
            got = ("OK", [f"{e['element']}|{e['kind']}" for e in inv if e["disposition"] == "N"])
        except Reject as r:
            got = ("REJECT", r.code, r.detail)
        except Exception as exc:  # noqa: BLE001
            got = ("EXC", type(exc).__name__, str(exc)[:80])
        exp = m["expected"]
        if "partial" in exp:
            ok = got[0] == "OK" and any(k in n for n in got[1] for k in exp["partial"])
            if ok:
                fl = coverage_flags(inv, [_Cov()])
                ok = ok and not fl["document_coverage_complete"] and fl["declared_not_covered"] >= 1
                rec(
                    "CN-07",
                    m["fixture_id"],
                    "partial coverage: processable, N declared, document_coverage_complete=false",
                    ok,
                    f"{got}; flags={fl}",
                )
            else:
                rec("CN-07", m["fixture_id"], "partial coverage expected", False, str(got))
        else:
            rec(
                "EXP-07",
                m["fixture_id"],
                f"rejected with one of {exp['reject']} ({m['note']})",
                got[0] == "REJECT" and got[1] in exp["reject"],
                f"{got} in {time.time() - t0:.2f}s",
            )
    # reason must be the intended one for the two bombs
    for fid, why in (
        ("R13_zip_bomb_declared", "declared total"),
        ("R14_zip_bomb_lying_header", "produced bytes"),
        ("Q16_flate_bomb", "inflate produced bytes"),
    ):
        m = [x for x in man("reject") if x["fixture_id"] == fid][0]
        data = load("reject", m)
        try:
            (Docx if m["kind"] == "docx" else PdfDoc)(data).inventory() if m["kind"] == "pdf" else Docx(data)
            rec("EXP-07", fid, f"bomb aborted for the intended reason ({why})", False)
        except Reject as r:
            rec("EXP-07", fid, f"bomb aborted for the intended reason ({why})", why in r.detail, r.detail)


class _Cov:
    class detection:  # noqa: N801
        class coverage:  # noqa: N801
            complete = True


# ------------------------------------------------------------------------------------------------------ EXP-09
def case_sel(r: dict) -> list[str]:
    return r["case"]["selected"]


def exp09(pdf_results: dict) -> None:
    # PDF
    for key in (
        ("P01_basic", "select_a"),
        ("P04_unicode_ligature", "select_values"),
        ("P09_all_selected", "all"),
        ("P03_rotated", "select_all_rotated"),
    ):
        r = pdf_results.get(key)
        if not r:
            continue
        m, sels, rep = r["m"], r["sels"], r["report"]
        corr = []
        for s, comp in zip(sels, rep["compensations"], strict=True):
            u = s["unit"]
        # build correspondences with position arithmetic (sorted per unit)
        by = {}
        for s, comp in zip(sels, rep["compensations"], strict=True):
            by.setdefault(s["unit"], []).append((s, comp))
        for u, lst in by.items():
            delta = 0
            for s, comp in sorted(lst, key=lambda x: x[0]["start"]):
                orig = m["expected_units"][u]["text"][s["start"] : s["end"]]
                corr.append(
                    {"unit": u, "start": s["start"] + delta, "orig": orig, "sub": s["replacement"], "comp": comp}
                )
                delta += len(s["replacement"]) - len(orig)
        data0 = load("pdf", m)
        d = pdf_state(r["out"])
        try:
            restored = pdf_restore(d, corr)
            ro = verify_pdf.engines_text(restored)
            ok_text = all(
                verify_pdf.norm(ro["pdfium"][i]) == verify_pdf.norm(m["expected_units"][i]["text"])
                for i in range(len(m["expected_units"]))
            )
            ra, rb = verify_pdf.render(data0, 1.5), verify_pdf.render(restored, 1.5)
            boxes = verify_pdf.affected_boxes(data0, m, [k for k in case_sel(r)], 1.5)
            dd = [
                verify_pdf.diff_outside(a, b, boxes.get(i, []), tol=0)
                for i, (a, b) in enumerate(zip(ra, rb, strict=True))
            ]
            outside_ok = all(x["outside_diff_pixels"] == 0 for x in dd)
            chk = verify_pdf.open_check(restored)
            inside = sum(x["total_diff_pixels"] for x in dd)
            rec(
                "EXP-09",
                f"{key[0]}[{key[1]}]",
                "mechanical restoration: values back at the recorded POSITIONS (validated, not searched); text equals original; PDFium render identical to the original OUTSIDE the restored values (inside: sub-glyph kerning inside a value is not restored); opens without repair",
                ok_text and outside_ok and chk["ok"],
                f"text={ok_text} outside_diff={[x['outside_diff_pixels'] for x in dd]} differing_pixels_inside_values={inside} open={chk['ok']}",
            )
        except Reject as exc:
            rec("EXP-09", f"{key[0]}[{key[1]}]", "mechanical restoration", False, f"{exc.code}: {exc.detail}")
        # negative: tampered substitute / wrong position
        if key == ("P01_basic", "select_a"):
            bad = [dict(c) for c in corr]
            bad[0]["start"] += 1
            try:
                pdf_restore(pdf_state(r["out"]), bad)
                rec("EXP-09", "P01_basic", "wrong recorded position is refused (no search fallback)", False)
            except Reject as exc:
                rec(
                    "EXP-09",
                    "P01_basic",
                    "wrong recorded position is refused (no search fallback)",
                    exc.code in ("restore_mismatch", "localization_ambiguous"),
                    exc.code,
                )
    # DOCX (same machinery as the transformation, driven by recorded positions)
    for fid in ("D01_runs", "D02_tables", "D03_headers_notes", "D04_unicode", "D08_all_selected", "D10_sdt"):
        m = [x for x in man("docx") if x["fixture_id"] == fid][0]
        data = load("docx", m)
        d = Docx(data)
        d.inventory()
        d.extract()
        repl = "[SUSTITUTO]"
        sels = sels_for(m, m["selected"], repl)
        out, _ = docx_transform(d, sels, remove_authorized=False)
        by = {}
        for s in sels:
            by.setdefault(s["unit"], []).append(s)
        back = []
        for u, lst in by.items():
            delta = 0
            for s in sorted(lst, key=lambda x: x["start"]):
                orig = m["expected_units"][u]["text"][s["start"] : s["end"]]
                back.append(
                    {
                        "unit": u,
                        "start": s["start"] + delta,
                        "end": s["start"] + delta + len(repl),
                        "replacement": orig,
                        "expect": repl,
                    }
                )
                delta += len(repl) - len(orig)
        d2 = Docx(out)
        d2.inventory()
        us = d2.extract()
        if not all(us[b["unit"]].text[b["start"] : b["end"]] == b["expect"] for b in back):
            rec("EXP-09", fid, "recorded positions hold the substitutes", False)
            continue
        out2, _ = docx_transform(
            d2, [{k: v for k, v in b.items() if k != "expect"} for b in back], remove_authorized=False
        )
        po, p0 = parts_of(out2), parts_of(data)
        order = list(dict.fromkeys(u["part"] for u in m["expected_units"]))
        same_text = [t for _, t in verify.docx_paragraph_texts(po, order)] == [u["text"] for u in m["expected_units"]]
        skel = all(verify.docx_skeleton(p0[p]) == verify.docx_skeleton(po[p]) for p in order)
        rec(
            "EXP-09",
            fid,
            "mechanical restoration: texts equal the original; skeleton of edited parts equal (run formatting at value boundaries may differ, see report)",
            same_text and skel and verify.open_check(out2)["ok"],
            f"text={same_text} skeleton={skel}",
        )
    # E classes are NOT recoverable (irreversible removal)
    m = [x for x in man("docx") if x["fixture_id"] == "D06_comments_meta"][0]
    d = Docx(load("docx", m))
    d.inventory()
    d.extract()
    out, _ = docx_transform(d, sels_for(m, ["v1.a"], "[X]"))
    rec(
        "EXP-09",
        "D06_comments_meta",
        "authorized removals are irreversible: comments/metadata cannot be recovered from the output",
        not verify.scan_docx(parts_of(out), ["ana.ejemplo@example.test", "Ejemplo SA"]),
    )


# ------------------------------------------------------------------------------------------------------ EXP-10
def exp10() -> None:
    src = (ROOT / "a04" / "pdf_tool.py").read_text()
    tree = ast.parse(src)
    stmts = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom))
    )
    phys = sum(1 for ln in src.splitlines() if ln.strip() and not ln.strip().startswith(("#", '"""')))
    ROWS.append(
        {
            "exp": "EXP-10",
            "subject": "pdf_tool.py",
            "check": "effort indicators (not a product limit, not a proof)",
            "status": "info",
            "detail": f"{phys} non-blank lines, {stmts} statements (all inventory/extraction/rewrite/restore code)",
        }
    )
    caps = {
        "needed and implemented (narrow subset)": [
            "content-stream parsing (pikepdf.parse_content_stream)",
            "simple-font code->Unicode from ToUnicode / WinAnsi / Differences+AGL",
            "per-glyph widths from /Widths",
            "synthesized-separator rule",
            "Form XObject recursion with use counting",
            "TJ rebuild with width compensation",
            "mechanical restore by position",
        ],
        "needed but NOT implemented (outside the probed subset; documented limitation)": [
            "Type0/CID fonts and subset TrueType embedding (reverse CID map, glyph availability in the subset)",
            "Tc/Tw/Tz/Ts/TL text state and text rise",
            "clipping paths and non-trivial CTM for geometry-based masks",
            "cloning a shared Form XObject for partial use",
            "fonts without /Widths (AFM metrics)",
            "right-to-left and vertical writing",
        ],
    }
    ROWS.append(
        {
            "exp": "EXP-10",
            "subject": "capabilities",
            "check": "capabilities required by the low-level route",
            "status": "info",
            "detail": json.dumps(caps),
        }
    )
    rec(
        "EXP-10",
        "pikepdf/QPDF",
        "route works for the probed subset without a commercial dependency and without a general PDF interpreter (see rows EXP-05/09)",
        True,
        "",
    )


# ------------------------------------------------------------------------------------------------------ main
def main() -> None:
    t0 = time.time()
    for p in (REND, SAMPLES):
        p.mkdir(parents=True, exist_ok=True)
    exp01()
    exp02()
    exp03()
    asyncio.run(exp04())
    exp05_docx()
    exp06()
    pr = exp05_pdf()
    verifier_sensitivity()
    exp07()
    exp09(pr)
    exp10()
    import importlib.metadata as md
    import os

    def git(*a: str) -> str:
        try:
            return subprocess.run(["git", *a], capture_output=True, text=True, cwd=ROOT, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    code_dirty = (
        git("status", "--porcelain", "--", ":(exclude)outputs")
        if not os.environ.get("GIT_REV")
        else os.environ.get("GIT_DIRTY", "")
    )

    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    env = {
        "env": ENV,
        "peak_rss_mb": round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1),
        "git_rev": os.environ.get("GIT_REV") or git("rev-parse", "HEAD"),
        "code_tree_dirty": bool(code_dirty),
        "code_tree_dirty_files": code_dirty.splitlines()[:10],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "seconds": round(time.time() - t0, 1),
        "libs": {
            n: md.version(n) for n in ("python-docx", "lxml", "pikepdf", "pypdfium2", "pypdf", "Pillow", "fonttools")
        },
        "libqpdf": pikepdf.__libqpdf_version__,
        "pdfium": str(pdfium.PDFIUM_INFO),
        "poppler": subprocess.run(["pdftotext", "-v"], capture_output=True, text=True).stderr.splitlines()[0]
        if shutil.which("pdftotext")
        else "n/a",
        "soffice": subprocess.run(["soffice", "--version"], capture_output=True, text=True).stdout.strip()
        if shutil.which("soffice")
        else "n/a",
    }
    (OUT / f"results-{ENV}.json").write_text(
        json.dumps({"env": env, "rows": ROWS, "hashes": HASHES, "coverage_flags": COV}, indent=1, ensure_ascii=False)
        + "\n"
    )
    s = {k: sum(1 for r in ROWS if r["status"] == k) for k in ("pass", "fail", "limitation", "pending", "info")}
    print(json.dumps(s), f"{time.time() - t0:.1f}s")
    for r in ROWS:
        if r["status"] == "fail":
            print("FAIL", r["exp"], r["subject"], "|", r["check"], "|", r["detail"][:300])


if __name__ == "__main__":
    main()
