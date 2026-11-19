"""A08 without a database: extractor v2, matrix rejections, planner/transformer/restorer and the independent
verifier, run in the extraction child's entry point (``child.process``) exactly as the worker calls it.

Corpus (native exports of stages 2 and 2B, manifest written before exporting) and synthetic controls whose expected
outcome is fixed here, before running (protocol 2B section 6; C-2B-01 to 06; docs/pdf-protection.md section 5).
"""

from __future__ import annotations

import base64
import json

import pikepdf
import pytest

from cawbase_content_engine.documents import pdf, pdf_extract
from cawbase_content_engine.protection import markers as A06

from . import a08_synth as S
from .a07_helpers import LIM
from .a08_helpers import MANIFEST, TEMPLATE, corpus, findings_for, flat, redacted_of

HEAD = "Pagina de control sintetico con texto suficiente para ocupar una linea completa."
LINE = "Arbol: Ana Gil Soler, documento 00000000-X, pago aprobado."


def doc(*extra: str, **kw: object) -> bytes:
    body = [S.line(72, 780, HEAD), S.line(72, 740, LINE), *extra]
    return S.build([" ".join(body)], **kw)  # type: ignore[arg-type]


# ---- extractor v2 ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case", sorted(TEMPLATE))
def test_extractor_v2_is_deterministic_on_the_corpus(case):  # H-A08-01, AC-A08-31
    data = corpus(case)
    a, b = pdf_extract.extract(data, LIM), pdf_extract.extract(data, LIM)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert [u["kind"] for u in a["units"]] == ["pdf_page"] * len(a["units"])
    for u in a["units"]:
        for s in u["segments"]:
            assert s["kind"] in ("original", "synthesized_line", "synthesized_space")
            assert u["text"][s["unit_start"] : s["unit_end"]]


def test_extractor_v1_is_kept_frozen_for_legacy_jobs():  # D-A08-04, P-A08-04
    data = corpus("C1")  # Type0/CID: the v1 extractor refuses it, v2 reads it
    with pytest.raises(Exception) as e:
        pdf.extract(data, LIM)
    assert getattr(e.value, "reason", "") == "unsupported_structure"
    assert pdf_extract.extract(data, LIM)["units"][0]["text"]


def test_identities_are_separate_and_docx_is_unchanged():  # E-A08-02, H-A08-09
    from cawbase_content_engine.jobs.ident import config_sha, docx_identity, extractor_set

    s = extractor_set(LIM)
    assert s["docx"] == {"id": "cce-docs-extract", "version": "1", "config_sha256": config_sha(LIM)}
    assert s["docx"] == docx_identity(LIM)
    assert s["pdf"]["id"] == "cce-pdf-extract" and s["pdf"]["version"] == "1"
    assert s["pdf"]["config_sha256"] != s["docx"]["config_sha256"]


@pytest.mark.parametrize(
    "variant",
    ["objstm", "contents_array", "rotate270", "text_state"],
)
def test_v2_admits_what_v1_refused(variant):  # PT-06, PT-09, PT-14, PT-15
    kw: dict[str, object] = {}
    extra = ""
    if variant == "objstm":
        kw["objstm"] = True
    elif variant == "contents_array":
        kw["contents_array"] = True
    elif variant == "rotate270":
        kw["rotate"] = 270
    else:
        extra = S.line(72, 700, "Estado con espaciado.", pre="0.5 Tc 1 Tw 95 Tz 0 Ts")
    out = S.extract(doc(extra, **kw))
    assert "units" in out and "Ana Gil Soler" in out["units"][0]["text"]


def _reject(data: bytes) -> str:
    return str(S.extract(data).get("rejected"))


def _hook_annot(sub: str):
    def h(pdf: pikepdf.Pdf) -> None:
        pdf.pages[0].obj.Annots = pikepdf.Array(
            [
                pdf.make_indirect(
                    pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name(sub), Rect=[0, 0, 9, 9])
                )
            ]
        )

    return h


@pytest.mark.parametrize(
    ("name", "data_fn", "reason"),
    [
        ("hidden Tr 3", lambda: doc(S.line(72, 700, "Oculto", pre="3 Tr")), "hidden_text"),
        ("clip Tr 7", lambda: doc(S.line(72, 700, "Recorte", pre="7 Tr")), "unsupported_text_state"),
        ("outside crop", lambda: doc(S.line(900, 700, "Fuera")), "hidden_text"),
        ("covered later", lambda: doc("1 1 1 rg 60 735 300 20 re f"), "hidden_text"),
        (
            "ActualText differs",
            lambda: doc("/Span <</ActualText (otro texto)>> BDC " + S.line(72, 700, "Texto") + " EMC"),
            "alternate_text_duplication",
        ),
        (
            "Alt in content",
            lambda: doc("/Span <</Alt (x)>> BDC " + S.line(72, 700, "T") + " EMC"),
            "alternate_text_duplication",
        ),
        ("layer", lambda: doc("/OC /L1 BDC " + S.line(72, 700, "T") + " EMC"), "forms_or_layers"),
        ("FreeText annotation", lambda: doc(hook=_hook_annot("/FreeText")), "unsupported_structure"),
        ("Widget", lambda: doc(hook=_hook_annot("/Widget")), "forms_or_layers"),
        ("FileAttachment", lambda: doc(hook=_hook_annot("/FileAttachment")), "embedded_content"),
        (
            "JavaScript OpenAction",
            lambda: doc(
                hook=lambda p: p.Root.__setattr__(
                    "OpenAction", pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("x"))
                )
            ),
            "executable_action",
        ),
        (
            "AcroForm",
            lambda: doc(hook=lambda p: p.Root.__setattr__("AcroForm", pikepdf.Dictionary(Fields=pikepdf.Array()))),
            "forms_or_layers",
        ),
        (
            "Type3",
            lambda: doc(hook=lambda p: p.pages[0].obj.Resources.Font.F1.__setattr__("Subtype", pikepdf.Name.Type3)),
            "font_mapping_insufficient",
        ),
        (
            "Differences without ToUnicode",
            lambda: doc(
                hook=lambda p: p.pages[0].obj.Resources.Font.F1.__setattr__(
                    "Encoding", pikepdf.Dictionary(Differences=pikepdf.Array([65, pikepdf.Name("/B")]))
                )
            ),
            "font_mapping_insufficient",
        ),
        ("scanned page", lambda: S.build(["q 595 0 0 842 0 0 cm /Im1 Do Q"], image=True), "scanned_without_text"),
    ],
)
def test_matrix_rejections_of_the_v2_extractor(name, data_fn, reason):  # PT-05, 07, 17, 24, 29, 31, 33
    assert _reject(data_fn()) == reason, name


def test_encrypted_and_incremental_are_refused():  # PT-17 (A04 rejections kept)
    import io

    data = doc()
    p = pikepdf.open(io.BytesIO(data))
    buf = io.BytesIO()
    p.save(buf, encryption=pikepdf.Encryption(user="", owner="o"))
    assert _reject(buf.getvalue()) == "encrypted"
    assert _reject(data + b"\n%%EOF\n") == "incremental_update"


# ---- the corpus, pure ------------------------------------------------------------------------------------------
def corpus_picks(case: str) -> list[tuple[int, str, int, str]]:
    data = corpus(case)
    units = S.extract(data)["units"]
    per = findings_for(data, TEMPLATE[case])
    out = []
    for pi, u in enumerate(units):
        for t, a, b in per.get(u["text"], []):
            frag = u["text"][a:b]
            occ = u["text"][:a].count(frag)
            out.append((pi, frag, occ, "redact" if t == "password" else "pseudonymize"))
    return out


@pytest.mark.parametrize("case", ["L1", "C1", "M1", "L5", "M5"])
def test_corpus_cycle_is_verified_without_the_original(case):  # AC-A08-30, 36, 37 (pure)
    r = S.cycle(corpus(case), corpus_picks(case))
    assert r["stage"] == "cycle", (r["stage"], r["reason"])
    der, rest = r["derivative"], r["restored"]
    for s in r["sels"]:
        if s["action"] == "redact":
            assert s["value"] not in rest.decode("latin-1")
    restored_text = [flat(u["text"]) for u in S.extract(rest)["units"]]
    expected = []
    for u in S.extract(corpus(case))["units"]:
        t = u["text"]
        for v in redacted_of(TEMPLATE[case]):
            t = t.replace(v, A06.REDACTED, 1)
        expected.append(flat(t))
    assert restored_text == expected
    assert any(ln["kind"] == "Z2" for ln in r["protect"]["layout"])  # a short mid-line value needs Z2 (C-2B-01)
    assert {c["element_class"] for c in r["protect"]["removed"]} >= {"document_info_descriptive"}
    assert "derivative" in r and der != corpus(case)


def test_c5_fails_explicitly_actualtext_in_the_z2_region():  # C-2B-03, the approved scope: C5 must fail
    r = S.cycle(corpus("C5"), corpus_picks("C5"))
    assert (r["stage"], r["reason"]) == ("protect", "zone_plan_unjustified")


def test_c1_selected_text_is_type0_cid_subset():  # the Type0/CID demonstrated in C1
    m = pdf_extract.model(corpus("C1"), LIM)
    kinds = {(f.kind, f.subtype, f.subset) for f in m.interp.fonts.values()}
    assert ("type0", "/Type0", True) in kinds
    picks = corpus_picks("C1")
    sels, _ = S.selections(corpus("C1"), picks)
    page = m.pages[int(sels[0]["unit_id"][1:]) - 1]
    gl = {g.gid: g for g in page.glyphs}
    seg = [
        s
        for s in page.segments
        if s["gid"] is not None and s["unit_start"] >= sels[0]["start"] and s["unit_end"] <= sels[0]["end"]
    ]
    assert all(m.interp.fonts[gl[s["gid"]].font_key].kind == "type0" for s in seg)


def test_manifest_values_are_synthetic():
    for v in MANIFEST["values"].values():
        assert "@" not in v["value"] or v["value"].endswith("example.test")


# ---- synthetic positives (CS) ----------------------------------------------------------------------------------
def test_z1_and_z2_with_permitted_occurrence_links_and_auxiliary_classes():  # CS, PT-25..28, D-A08-03
    data = doc(
        S.line(72, 700, "Contacto: Ana Gil Soler en la sede central."),
        S.line(72, 660, "Pagina con mas texto para tener un area ancha de trabajo y bastante espacio."),
        info={"Title": "Titulo sintetico", "Author": "Autor", "CreationDate": "D:20260101000000Z"},
        xmp=True,
        annots=[[S.link([72, 655, 200, 670]), S.note([500, 800, 520, 820])]],
    )
    r = S.cycle(data, [(0, "Gil", 0, "pseudonymize"), (0, "00000000-X", 0, "redact")])
    assert r["stage"] == "cycle", (r["stage"], r["reason"])
    removed = {c["element_class"] for c in r["protect"]["removed"]}
    assert removed == {"document_info_descriptive", "xmp_metadata", "comments"}
    der = S.extract(r["derivative"])["units"][0]["text"]
    assert "Contacto: Ana Gil Soler en la sede central." in der  # identical non-selected occurrence, in place
    p = pikepdf.open(__import__("io").BytesIO(r["restored"]))
    assert list(p.trailer.Info.keys()) == ["/CreationDate"] and "/Metadata" not in p.Root
    assert [str(a.Subtype) for a in p.pages[0].Annots] == ["/Link"]


@pytest.mark.parametrize(
    ("name", "content", "picks", "expected"),
    [
        (
            "text state Tc Tw Tz Ts",
            S.line(72, 700, "Firma de Teo Vidal Ruiz aqui.", pre="0.4 Tc 1.5 Tw 96 Tz 0.5 Ts"),
            [(0, "Vidal", 0, "pseudonymize")],
            "cycle",
        ),
        (
            "Tr 2",
            S.line(72, 700, "Firma de Teo Vidal Ruiz aqui.", pre="2 Tr"),
            [(0, "Vidal", 0, "pseudonymize")],
            "cycle",
        ),
        (
            "two pseudonymizations in one line",
            S.line(72, 700, "Firmaron Teo Vidal y Ruth Sanz como testigos del acto."),
            [(0, "Teo Vidal", 0, "pseudonymize"), (0, "Ruth Sanz", 0, "pseudonymize")],
            "cycle",
        ),
        (
            "redaction then pseudonymization (RZ)",
            S.line(72, 700, "Arbol: 00000000-X Ana Gil Soler pago aprobado."),
            [(0, "00000000-X", 0, "redact"), (0, "Ana Gil Soler", 1, "pseudonymize")],
            "cycle",
        ),
        (
            "pseudonymization then short redaction with room (RZ)",
            S.line(72, 700, "Lo cobro 350 EUR al contado.") + " " + S.line(72, 650, HEAD + " Mas texto largo."),
            [(0, "Lo cobro", 0, "pseudonymize"), (0, "350 EUR", 0, "redact")],
            "cycle",
        ),
    ],
)
def test_structural_positives(name, content, picks, expected):  # CS05, CS11, CS12, CS14, CS17
    r = S.cycle(doc(content), picks)
    assert r["stage"] == expected, (name, r["stage"], r["reason"])


def test_short_redaction_without_room_fails_closed_on_restoration():  # CS16, Q-A08-17
    data = S.build([S.line(72, 740, "Ana Gil Soler cobro 350 EUR al contado.")])
    r = S.cycle(data, [(0, "Ana Gil Soler", 0, "pseudonymize"), (0, "350 EUR", 0, "redact")])
    assert (r["stage"], r["reason"]) in (("restore", "restoration_layout_unavailable"), ("cycle", None))
    if r["stage"] == "cycle":  # the line had room after all: then the excess must have stayed in the zone
        assert r["verify_restore"]["ok"]


@pytest.mark.parametrize(
    ("name", "data_fn", "picks", "reason"),
    [
        (
            "multi-line selection",
            lambda: doc(),
            [(0, "linea completa.\nArbol", 0, "pseudonymize")],
            "selection_crosses_boundary",
        ),
        (
            "image (no OCR)",
            lambda: doc("q 20 0 0 20 400 600 cm /Im1 Do Q", image=True),
            [(0, "Gil", 0, "pseudonymize")],
            "uncovered_content",
        ),
        (
            "curved path",
            lambda: doc("q 100 600 m 150 650 200 600 250 650 c S Q"),
            [(0, "Gil", 0, "pseudonymize")],
            "uncovered_content",
        ),
        (
            "Form XObject",
            lambda: S.build([S.line(72, 780, HEAD) + " /X1 Do"], xobjects={"/X1": S.line(72, 740, LINE)}),
            [(0, "Gil", 0, "pseudonymize")],
            "selection_not_transformable",
        ),
        (
            "base-14 without /Widths",
            lambda: doc(no_widths=True),
            [(0, "Gil", 0, "pseudonymize")],
            "font_mapping_insufficient",
        ),
        (
            "partial ligature",
            lambda: doc(S.hexline(72, 700, S.f2_codes("Unafirma clara."))),
            [(0, "Unaf", 0, "pseudonymize")],
            "localization_ambiguous",
        ),
        (
            "right to left",
            lambda: doc(S.hexline(72, 700, S.f2_codes("Nombre א aqui."))),
            [(0, "א", 0, "pseudonymize")],
            "selection_not_transformable",
        ),
        (
            "ActualText containing the selection",
            lambda: doc("/Span <</ActualText (Ruth Sanz)>> BDC " + S.line(72, 700, "Ruth Sanz") + " EMC"),
            [(0, "Ruth Sanz", 0, "pseudonymize")],
            "selection_not_transformable",
        ),
        (
            "ActualText in the moved Z2 region",
            lambda: doc(
                "BT /F1 11 Tf 72 700 Td (Llego Bea a las 9 y ) Tj /Span <</ActualText (firmo)>> BDC (firmo) Tj EMC ( el libro.) Tj ET"
            ),
            [(0, "Bea", 0, "pseudonymize")],
            "zone_plan_unjustified",
        ),
    ],
)
def test_safe_rejections(name, data_fn, picks, reason):  # CR, C-2B-03, C-2B-06
    data = data_fn()
    if "rejected" in S.extract(data):
        pytest.fail(f"{name}: refused at extraction: {S.extract(data)}")
    try:
        sels, _ = S.selections(data, picks)
    except ValueError:
        pytest.fail(f"{name}: fragment not in the unit text")
    pr = S.protect(data, sels)
    assert pr.get("failure") == reason, (name, pr.get("failure"), pr.get("rejected"))


def test_table_grid_is_admitted_by_rule():  # RP3
    grid = (
        " ".join(f"0.5 w 60 {y} m 400 {y} l S" for y in (690, 720, 750))
        + " "
        + " ".join(f"0.5 w {x} 690 m {x} 750 l S" for x in (60, 230, 400))
    )
    r = S.cycle(doc(grid), [(0, "Ana Gil Soler", 0, "pseudonymize")])
    assert r["stage"] == "cycle", r["reason"]


# ---- the independent verifier detects every alteration (CV) -------------------------------------------------
@pytest.fixture(scope="module")
def good():
    data = doc(
        S.line(72, 700, "Contacto: Ana Gil Soler en la sede central."),
        info={"Title": "Titulo"},
        annots=[[S.link([72, 695, 200, 712])]],
    )
    r = S.cycle(data, [(0, "Gil", 0, "pseudonymize"), (0, "00000000-X", 0, "redact")])
    assert r["stage"] == "cycle", r["reason"]
    return data, r


def _vp(good, der=None, layout=None):
    data, r = good
    return S.verify_protect(data, der or r["derivative"], r["sels"], layout or r["protect"]["layout"])


def _relayout(good, fn):
    layout = json.loads(json.dumps(good[1]["protect"]["layout"]))
    fn(layout)
    return layout


def _z2(layout):
    return next(ln for ln in layout if ln["kind"] == "Z2")


_PDF = pikepdf.new()


def parse(src: bytes) -> list:
    return list(pikepdf.parse_content_stream(pikepdf.Stream(_PDF, src)))


def _tj(ops):
    return [i for i, x in enumerate(ops) if str(x.operator) in ("Tj", "TJ")]


CV = {
    "value in a kept /Info key": lambda g: (
        S.edit_pdf(
            g[1]["derivative"],
            lambda p: p.trailer.__setattr__(
                "Info", p.make_indirect(pikepdf.Dictionary(ModDate=pikepdf.String("D:2026 00000000-X")))
            ),
        ),
        None,
        "residue_detected",
    ),
    "descriptive /Info reinserted": lambda g: (
        S.edit_pdf(
            g[1]["derivative"],
            lambda p: p.trailer.__setattr__(
                "Info", p.make_indirect(pikepdf.Dictionary(Title=pikepdf.String("Titulo")))
            ),
        ),
        None,
        "derivative_verification_failed",
    ),
    "value in a content comment": lambda g: (S.edit_page(g[1]["derivative"], 0, lambda ops: ops), None, None),
    "value drawn again outside the zones": lambda g: (
        S.edit_page(g[1]["derivative"], 0, lambda ops: ops + parse(b"BT /F1 3 Tf 300 300 Td (Gil) Tj ET")),
        None,
        "derivative_verification_failed",
    ),
    "value in an empty /ActualText span": lambda g: (
        S.edit_page(
            g[1]["derivative"],
            0,
            lambda ops: [
                pikepdf.ContentStreamInstruction(
                    [pikepdf.Name("/Span"), pikepdf.Dictionary(ActualText=pikepdf.String("00000000-X"))],
                    pikepdf.Operator("BDC"),
                ),
                pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")),
                *ops,
            ],
        ),
        None,
        None,
    ),
    "glyph outside the zones moved 1 pt": lambda g: (
        S.edit_page(g[1]["derivative"], 0, lambda ops: _move_first_td(ops)),
        None,
        "derivative_verification_failed",
    ),
    "non-text painting changed": lambda g: (
        S.edit_page(g[1]["derivative"], 0, lambda ops: parse(b"0.5 w 10 10 m 20 10 l S") + ops),
        None,
        "derivative_verification_failed",
    ),
    "link changed": lambda g: (
        S.edit_pdf(
            g[1]["derivative"],
            lambda p: p.pages[0].Annots[0].A.__setattr__("URI", pikepdf.String("https://example.test/otro")),
        ),
        None,
        "derivative_verification_failed",
    ),
    "XMP reinserted": lambda g: (
        S.edit_pdf(g[1]["derivative"], lambda p: p.Root.__setattr__("Metadata", p.make_stream(b"<x/>"))),
        None,
        "derivative_verification_failed",
    ),
    "plan widens its Z2 zone (CV31)": lambda g: (
        None,
        _relayout(g, lambda L: _z2(L).__setitem__("r", _z2(L)["r"] + 40)),
        "derivative_verification_failed",
    ),
    "plan factor falsified (CV35)": lambda g: (
        None,
        _relayout(g, lambda L: _z2(L).__setitem__("f", _z2(L)["f"] - 0.05)),
        "derivative_verification_failed",
    ),
    "binding x_orig falsified": lambda g: (
        None,
        _relayout(g, lambda L: L[0]["sels"][0].__setitem__("x_orig", L[0]["sels"][0]["x_orig"] + 2)),
        "derivative_verification_failed",
    ),
    "binding span falsified (CV32 analogue)": lambda g: (
        None,
        _relayout(g, lambda L: _z2(L)["sels"][0].__setitem__("span", _z2(L)["sels"][0]["span"] + 1)),
        "derivative_verification_failed",
    ),
    "binding moved glyph falsified": lambda g: (
        None,
        _relayout(g, lambda L: _z2(L)["moved"][0].__setitem__("x_orig", _z2(L)["moved"][0]["x_orig"] + 1)),
        "derivative_verification_failed",
    ),
    "binding band widened": lambda g: (
        None,
        _relayout(g, lambda L: _z2(L)["band"].__setitem__(1, _z2(L)["band"][1] + 5)),
        "derivative_verification_failed",
    ),
}


def _move_first_td(ops):
    for i, x in enumerate(ops):
        if str(x.operator) == "Td":
            v = [float(q) for q in x.operands]
            ops[i] = pikepdf.ContentStreamInstruction([v[0] + 1, v[1]], pikepdf.Operator("Td"))
            break
    return ops


def _comment(der: bytes) -> bytes:
    import io
    import zlib

    p = pikepdf.open(io.BytesIO(der))
    st = p.pages[0].obj.Contents
    st.write(st.read_bytes() + b"\n% Gil 00000000-X\n")
    buf = io.BytesIO()
    p.save(buf, compress_streams=True)
    _ = zlib
    return buf.getvalue()


@pytest.mark.parametrize("name", sorted(CV))
def test_verifier_detects_alterations(good, name):  # AC-A08-36, CV controls (C-2B-05)
    der, layout, code = CV[name](good)
    if name == "value in a content comment":
        der, code = _comment(good[1]["derivative"]), "residue_detected"
    v = _vp(good, der, layout)
    assert not v.get("ok"), (name, v)
    if code is not None:
        assert v["failure"] == code, (name, v)


def test_verifier_accepts_the_unaltered_derivative(good):
    assert _vp(good)["ok"]


def test_restore_verifier_detects_alterations(good):  # AC-A08-37
    data, r = good
    der, layout, vals = r["derivative"], r["protect"]["layout"], r["vals"]
    assert S.verify_restore(der, r["restored"], layout, vals)["ok"]
    # a marker left in place (restoration skipped)
    assert not S.verify_restore(der, der, layout, vals)["ok"]
    # a restored value moved by 1 pt
    moved = S.edit_page(r["restored"], 0, lambda ops: _shift_last_tm(ops))
    assert not S.verify_restore(der, moved, layout, vals)["ok"]
    # another value than the context's
    wrong = {k: "Otro Valor" for k in vals}
    assert not S.verify_restore(der, r["restored"], layout, wrong)["ok"]


def _shift_last_tm(ops):
    idx = [i for i, x in enumerate(ops) if str(x.operator) == "Tm"]
    i = idx[0]
    v = [float(q) for q in ops[i].operands]
    v[4] += 1.0
    ops[i] = pikepdf.ContentStreamInstruction(v, pikepdf.Operator("Tm"))
    return ops


def test_restorer_needs_only_the_derivative_binding_and_values(good):  # D-A08-05
    data, r = good
    out = S.restore(r["derivative"], r["protect"]["layout"], r["vals"])
    assert base64.b64decode(out["restored"]) == r["restored"]  # deterministic, never reads the original
    bad = S.restore(r["derivative"], r["protect"]["layout"], {})
    assert bad.get("failure") == "derivative_markers_invalid"


def test_report_and_child_outputs_never_carry_values(good):
    data, r = good
    assert "Gil" not in json.dumps(r["verify"]) and "00000000" not in json.dumps(r["verify"])
    lay = json.dumps(r["protect"]["layout"])
    assert "Gil" not in lay and "00000000-X" not in lay


# ---- short values (1-2 characters) on the decoded text surfaces of contract 10.4 --------------------------
SHORT_LINE = "Firma de Bo en la sede central del acto."


def short_doc(uri: str = "https://example.test/doc", **kw: object) -> bytes:
    body = [S.line(72, 780, HEAD), S.line(72, 740, SHORT_LINE), S.line(72, 700, "Otra linea de texto ajeno.")]
    extra = kw.pop("extra", "")
    if extra:
        body.append(str(extra))
    return S.build([" ".join(body)], annots=[[S.link([72, 695, 200, 712], uri)]], **kw)  # type: ignore[arg-type]


def _dest_hook(pdf: pikepdf.Pdf) -> None:
    pdf.pages[0].Annots[0].A = pikepdf.Dictionary(S=pikepdf.Name.GoTo, D=pikepdf.Name("/Bo"))


@pytest.mark.parametrize(
    ("surface", "data_fn"),
    [
        ("link /URI", lambda: short_doc("https://example.test/perfil/Bo")),
        ("link /URI, percent-encoded", lambda: short_doc("https://example.test/perfil/%42o")),
        ("named destination of a link", lambda: short_doc(hook=_dest_hook)),
        ("kept /Info date string", lambda: short_doc(info={"CreationDate": "D:2026 Bo"})),
    ],
)
def test_short_value_left_on_a_text_surface_is_a_residue(surface, data_fn):  # contract 10.4, no minimum length
    r = S.cycle(data_fn(), [(0, "Bo", 0, "pseudonymize")])
    assert (r["stage"], r["reason"]) == ("verify", "residue_detected"), (surface, r["stage"], r["reason"])


def test_one_character_value_in_a_link_is_a_residue():
    data = S.build(
        [" ".join([S.line(72, 780, HEAD), S.line(72, 740, "Grupo Q del acto central.")])],
        annots=[[S.link([72, 600, 200, 612], "https://example.test/grupo/Q")]],
    )
    r = S.cycle(data, [(0, "Q", 0, "pseudonymize")])
    assert (r["stage"], r["reason"]) == ("verify", "residue_detected")


def _inject(der: bytes, src: bytes) -> bytes:
    import io

    p = pikepdf.open(io.BytesIO(der))
    st = p.pages[0].obj.Contents
    st.write(src + b"\n" + st.read_bytes())
    buf = io.BytesIO()
    p.save(buf, compress_streams=True)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("surface", "src"),
    [
        ("content-stream comment", b"% nota Bo"),
        ("/ActualText of an empty marked span", b"/Span <</ActualText (Bo)>> BDC EMC"),
        ("string operand of a non-text operator", b"/Span <</Lang (Bo)>> BDC EMC"),
    ],
)
def test_verifier_finds_a_short_value_copied_into_a_content_stream(surface, src):  # transformer fault, 10.4
    data = short_doc()
    r = S.cycle(data, [(0, "Bo", 0, "pseudonymize")])
    assert r["stage"] == "cycle", r["reason"]
    v = S.verify_protect(data, _inject(r["derivative"], src), r["sels"], r["protect"]["layout"])
    assert not v["ok"], (surface, v)
    if surface == "content-stream comment":
        assert v["failure"] == "residue_detected", v


def test_short_value_is_protected_when_no_copy_remains():  # the clean control
    r = S.cycle(short_doc(), [(0, "Bo", 0, "pseudonymize")])
    assert r["stage"] == "cycle", (r["stage"], r["reason"])
    der = S.extract(r["derivative"])["units"][0]["text"]
    assert "Bo" not in der and A06.STRICT.search(der)
    assert "Bo" in S.extract(r["restored"])["units"][0]["text"]
