"""A07 transformer, verifier and pure helpers WITHOUT a database (the same code the isolated child runs).

The oracle of every case is written by hand (by construction), never taken from the extractor under test.
"""

from __future__ import annotations

import base64
import re

import pytest

from cawbase_content_engine.documents import docx, docx_transform, docx_verify
from cawbase_content_engine.documents.common import Limits
from cawbase_content_engine.docx_protection import binding as bd
from cawbase_content_engine.docx_protection.execute import check_positions
from cawbase_content_engine.jobs.setup import JobsLimits

from .a07_fixtures import CODE, MAIL, NAME, Doc, esc, image_run, p, part, patch, r, t, tbl, tc

LIM = Limits.from_params({k: getattr(JobsLimits(), k) for k in Limits.__dataclass_fields__})
M1 = "[[CCE1:0123456789ABCDEF]]"
M2 = "[[CCE1:GHJKMNPQRSTVWXYZ]]"[:7] + "GHJKMNPQRSTVWXYZ" + "]]"
RED = "[REDACTED]"


def units(data: bytes) -> list[dict]:
    return docx.extract(data, LIM)["units"]


def sel(data: bytes, picks: list[tuple[str, str, int, str]]) -> list[dict]:
    """picks: (unit text marker fragment to locate the unit, value, occurrence, substitute)."""
    us = units(data)
    out = []
    for anchor, value, occ, sub in picks:
        u = next(u for u in us if anchor in u["text"])
        pos = -1
        for _ in range(occ + 1):
            pos = u["text"].index(value, pos + 1)
        out.append({"unit_id": u["unit_id"], "start": pos, "end": pos + len(value), "substitute": sub})
    return out


def protect(data: bytes, picks: list[tuple[str, str, int, str]]) -> dict:
    return docx_transform.protect(data, LIM, sel(data, picks), check_only=False)


def verify(original: bytes, derivative: bytes, picks: list[tuple[str, str, int, str]], oracle: list[str]) -> dict:
    """Oracle by construction; values / substitute spans / marker spans derived from the picks and the oracle."""
    us = units(original)
    subs: list[list[list[int]]] = [[] for _ in us]
    marks: list[list[list[int]]] = [[] for _ in us]
    values = []
    grouped: dict[str, list[tuple[int, int, str]]] = {}
    for s in sel(original, picks):
        grouped.setdefault(s["unit_id"], []).append((s["start"], s["end"], s["substitute"]))
        u = next(u for u in us if u["unit_id"] == s["unit_id"])
        values.append(u["text"][s["start"] : s["end"]])
    for i, u in enumerate(us):
        trip = sorted(grouped.get(u["unit_id"], []))
        text, spans = bd.oracle_text(u["text"], trip)
        assert text == oracle[i], (text, oracle[i])  # the construction and the oracle agree
        subs[i] = [list(x) for x in spans]
        marks[i] = [list(x) for x, (_a, _b, sb) in zip(spans, trip, strict=True) if sb != RED]
    return docx_verify.verify(
        original, derivative, {"oracle": oracle, "values": values, "subs": subs, "markers": marks, "mode": "protect"}
    )


def derivative(out: dict) -> bytes:
    assert "derivative" in out, out
    return base64.b64decode(out["derivative"])


# ---- TX-01..04: runs, mixed format, several selections, Unicode --------------------------------------------
def test_value_across_three_runs_takes_the_first_run_format():  # AC-A07-12
    body = [p(r("Contactar a An", "<w:i/>"), r("a Ej", "<w:b/>"), r("emplo hoy.", '<w:color w:val="FF0000"/>'))]
    data = Doc(body=body).build()
    picks = [("Contactar", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    assert [u["text"] for u in units(der)] == [f"Contactar a {M1} hoy."]
    xml = part(der, "word/document.xml").decode()
    assert f"<w:r><w:rPr><w:i/></w:rPr><w:t>Contactar a {M1}</w:t></w:r>" in xml  # first run's format
    assert "<w:b/>" not in xml  # the middle run (wholly inside the value) is gone
    assert '<w:color w:val="FF0000"/></w:rPr><w:t xml:space="preserve">mplo hoy.</w:t>' not in xml
    assert "emplo" not in xml and "hoy." in xml
    assert verify(data, der, picks, [f"Contactar a {M1} hoy."])["ok"]


def test_several_selections_in_one_text_node_and_unicode():  # AC-A07-13, 14
    text = f"😀 {NAME} y é {MAIL} ‍ {CODE} "
    data = Doc(body=[p(r(text))]).build()
    picks = [("😀", NAME, 0, M1), ("😀", MAIL, 0, RED), ("😀", CODE, 0, M2)]
    der = derivative(protect(data, picks))
    expected = f"😀 {M1} y é {RED} ‍ {M2} "
    assert [u["text"] for u in units(der)] == [expected]
    assert 'xml:space="preserve"' in part(der, "word/document.xml").decode()
    assert verify(data, der, picks, [expected])["ok"]


def test_tables_nested_merged_headers_footers_notes():  # AC-A07-15, 16
    inner = tbl([[tc(p(r(f"Interna {NAME}"))), tc(p(r("x")))]])
    body = [
        tbl(
            [
                [tc(p(r(f"Celda {NAME}")), tcpr='<w:gridSpan w:val="2"/>')],
                [tc(p(r("a")), inner, tcpr='<w:vMerge w:val="restart"/>'), tc(p(r("b")))],
                [tc(p(), tcpr="<w:vMerge/>"), tc(p(r(f"Final {MAIL}")))],
            ]
        ),
        p(r("Texto"), '<w:r><w:footnoteReference w:id="1"/></w:r>'),
    ]
    doc = Doc(
        body=body,
        headers={"default": [p(r(f"Encabezado {NAME}"))], "first": [p(r("Primera"))], "even": [p(r("Par"))]},
        footers={"default": [p(r(f"Pie {MAIL}"))]},
        footnotes=[[p(r(f"Nota {NAME}"))]],
        title_pg=True,
        even_odd=True,
    )
    data = doc.build()
    picks = [
        ("Celda", NAME, 0, M1),
        ("Interna", NAME, 0, M1),
        ("Final", MAIL, 0, RED),
        ("Encabezado", NAME, 0, M1),
        ("Pie", MAIL, 0, RED),
        ("Nota", NAME, 0, M1),
    ]
    der = derivative(protect(data, picks))
    oracle = [u["text"].replace(NAME, M1).replace(MAIL, RED) for u in units(data)]
    assert [u["text"] for u in units(der)] == oracle
    xml = part(der, "word/document.xml").decode()
    assert '<w:gridSpan w:val="2"/>' in xml and '<w:vMerge w:val="restart"/>' in xml and "<w:vMerge/>" in xml
    assert '<w:footnoteReference w:id="1"/>' in xml and "<w:titlePg/>" in xml
    assert part(der, "word/settings.xml") == part(data, "word/settings.xml")  # untouched parts byte for byte
    assert verify(data, der, picks, oracle)["ok"]


# ---- hyperlinks, controls, fields, structural elements, containers --------------------------------
def test_hyperlink_inside_is_transformed_and_crossing_fails():  # AC-A07-17
    link = f'<w:hyperlink r:id="rIdH">{r(f"ver {NAME}", '<w:rStyle w:val="Hyperlink"/>')}</w:hyperlink>'
    data = Doc(body=[p(r("Antes "), link, r(" despues"))], hyperlinks={"rIdH": "https://example.test/x"}).build()
    der = derivative(protect(data, [("ver", NAME, 0, M1)]))
    xml = part(der, "word/document.xml").decode()
    assert '<w:hyperlink r:id="rIdH">' in xml and M1 in xml
    assert verify(data, der, [("ver", NAME, 0, M1)], [f"Antes ver {M1} despues"])["ok"]
    crossing = Doc(
        body=[p(r("Antes Ana "), f'<w:hyperlink r:id="rIdH">{r("Ejemplo")}</w:hyperlink>')],
        hyperlinks={"rIdH": "https://example.test/x"},
    ).build()
    assert protect(crossing, [("Antes", NAME, 0, M1)]) == {"failure": "selection_crosses_boundary"}


@pytest.mark.parametrize(
    ("sdtpr", "expected"),
    [
        ('<w:alias w:val="Campo"/>', None),
        ('<w:alias w:val="Campo"/><w:text/>', None),
        ("<w:richText/>", "selection_not_transformable"),
        ("<w:showingPlcHdr/>", "selection_not_transformable"),
        ('<w:lock w:val="sdtContentLocked"/>', "selection_not_transformable"),
        ('<w:lock w:val="contentLocked"/>', "selection_not_transformable"),
    ],
)
def test_content_controls(sdtpr, expected):  # AC-A07-18
    sdt = f"<w:sdt><w:sdtPr>{sdtpr}</w:sdtPr><w:sdtContent>{r(f'Control {NAME} fin')}</w:sdtContent></w:sdt>"
    data = Doc(body=[p(sdt)]).build()
    out = protect(data, [("Control", NAME, 0, M1)])
    if expected is None:
        der = derivative(out)
        assert verify(data, der, [("Control", NAME, 0, M1)], [f"Control {M1} fin"])["ok"]
        assert f"<w:sdtPr>{sdtpr}</w:sdtPr>" in part(der, "word/document.xml").decode()
    else:
        assert out == {"failure": expected}


def test_selection_crossing_a_control_boundary_fails():  # AC-A07-18
    sdt = f"<w:sdt><w:sdtPr><w:text/></w:sdtPr><w:sdtContent>{r('Ejemplo')}</w:sdtContent></w:sdt>"
    data = Doc(body=[p(r("Ana "), sdt)]).build()
    assert protect(data, [("Ana", NAME, 0, M1)]) == {"failure": "selection_crosses_boundary"}


FIELD_CASES = {
    "simple": p(r("Autor: "), f'<w:fldSimple w:instr=" AUTHOR ">{r(NAME)}</w:fldSimple>'),
    "complex": p(
        r("Correo: "),
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> MERGEFIELD X </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        r(NAME),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ),
    "into_result": p(
        r("Ana "),
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> REF x </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        r("Ejemplo"),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ),
    "hyperlink_field": p(
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "https://example.test" </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        r(NAME),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ),
    "nested": p(
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> IF </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> REF y </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        r(NAME),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        r("z"),
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ),
}


@pytest.mark.parametrize("case", list(FIELD_CASES))
def test_field_results_are_not_transformable(case):  # AC-A07-19
    data = Doc(body=[FIELD_CASES[case]]).build()
    u = units(data)[0]
    assert NAME in u["text"]  # the A05 extractor maps the result as unit text, unmarked
    assert protect(data, [(u["text"][:3], NAME, 0, M1)]) == {"failure": "field_result_not_transformable"}


def test_field_crossing_paragraphs_and_unbalanced_stack():  # AC-A07-19, 20
    body = [
        p(
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> TOC </w:instrText></w:r>',
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
            r(f"Indice {NAME}"),
        ),
        p(r(f"Segunda {NAME}"), '<w:r><w:fldChar w:fldCharType="end"/></w:r>'),
        p(r(f"Fuera {NAME}")),
    ]
    data = Doc(body=body).build()
    assert protect(data, [("Segunda", NAME, 0, M1)]) == {"failure": "field_result_not_transformable"}
    der = derivative(protect(data, [("Fuera", NAME, 0, M1)]))
    assert M1 in units(der)[2]["text"]
    broken = Doc(body=[p('<w:r><w:fldChar w:fldCharType="begin"/></w:r>', r(f"Sin fin {NAME}"))]).build()
    assert protect(broken, [("Sin fin", NAME, 0, M1)]) == {"failure": "unsupported_structure"}


@pytest.mark.parametrize(
    "inner",
    [
        "<w:r><w:tab/></w:r>",
        "<w:r><w:br/></w:r>",
        '<w:r><w:sym w:font="Symbol" w:char="F020"/></w:r>',
        '<w:r><w:footnoteReference w:id="1"/></w:r>',
        "<w:r><w:noBreakHyphen/></w:r>",
    ],
)
def test_non_text_run_content_inside_a_selection(inner):  # AC-A07-21
    data = Doc(body=[p(r("Ana"), inner, r(" Ejemplo"))], footnotes=[[p(r("n"))]]).build()
    u = units(data)[0]
    start = 0
    end = len(u["text"])
    out = docx_transform.protect(
        data, LIM, [{"unit_id": u["unit_id"], "start": start, "end": end, "substitute": M1}], check_only=True
    )
    assert out == {"failure": "selection_not_transformable"}


def test_bookmarks_inside_and_outside_a_selection():  # AC-A07-22
    inside = Doc(
        body=[p(r("Ana "), '<w:bookmarkStart w:id="0" w:name="b"/>', r("Ejemplo"), '<w:bookmarkEnd w:id="0"/>')]
    ).build()
    assert protect(inside, [("Ana", NAME, 0, M1)]) == {"failure": "selection_crosses_boundary"}
    outside = Doc(
        body=[p('<w:bookmarkStart w:id="0" w:name="b"/>', r(f"x {NAME} y"), '<w:bookmarkEnd w:id="0"/>')]
    ).build()
    der = derivative(protect(outside, [("x", NAME, 0, M1)]))
    assert '<w:bookmarkStart w:id="0" w:name="b"/>' in part(der, "word/document.xml").decode()


@pytest.mark.parametrize(
    "wrap",
    [
        lambda x: f'<w:smartTag w:uri="urn:x" w:element="e">{x}</w:smartTag>',
        lambda x: f'<w:customXml w:element="e">{x}</w:customXml>',
        lambda x: f'<w:dir w:val="ltr">{x}</w:dir>',
    ],
)
def test_inline_containers_are_not_transformable(wrap):  # AC-A07-23
    data = Doc(body=[p(wrap(r(f"x {NAME}")))]).build()
    assert protect(data, [("x", NAME, 0, M1)]) == {"failure": "selection_not_transformable"}


def test_one_bad_selection_fails_the_whole_document():  # AC-A07-25
    data = Doc(body=[p(r(f"Bien {NAME}")), FIELD_CASES["simple"]]).build()
    out = docx_transform.protect(
        data, LIM, sel(data, [("Bien", NAME, 0, M1), ("Autor", NAME, 0, M1)]), check_only=False
    )
    assert out == {"failure": "field_result_not_transformable"}


# ---- E classes, coverage, verification controls ------------------------------------------------------
def commented_doc(**kw) -> Doc:
    body = [
        p(
            '<w:commentRangeStart w:id="0"/>',
            r(f"Texto anotado con {NAME}"),
            '<w:commentRangeEnd w:id="0"/>',
            '<w:r><w:commentReference w:id="0"/></w:r>',
        )
    ]
    return Doc(
        body=body,
        headers={
            "default": [
                p(
                    '<w:commentRangeStart w:id="1"/>',
                    r("Cabecera"),
                    '<w:commentRangeEnd w:id="1"/>',
                    '<w:r><w:commentReference w:id="1"/></w:r>',
                )
            ]
        },
        comments=[f"Comentario sobre {NAME}", "Otro comentario"],
        comments_extended=True,
        people=True,
        core={
            "dc:title": f"Informe de {NAME}",
            "dc:creator": "Autor Sintetico",
            "cp:lastModifiedBy": "Autor Sintetico",
        },
        app={"Company": "Empresa Sintetica", "Manager": "Gerente Sintetico", "Pages": "1"},
        custom={"Cliente": NAME},
        thumbnail=True,
        **kw,
    )


def test_e_classes_removed_and_annotated_text_kept():  # AC-A07-28, 29, 30
    data = commented_doc().build()
    out = protect(data, [("anotado", NAME, 0, M1)])
    der = derivative(out)
    classes = {c["element_class"]: c["count"] for c in out["removed"]}
    assert classes == {
        "comments": 2,
        "comment_people": 1,
        "core_descriptive_properties": 3,
        "app_descriptive_properties": 2,
        "custom_properties": 1,
        "thumbnail": 1,
    }
    for gone in (
        "word/comments.xml",
        "word/commentsExtended.xml",
        "word/people.xml",
        "docProps/custom.xml",
        "docProps/thumbnail.png",
    ):
        assert gone not in docx_verify.Pkg(der).raw
    xml = part(der, "word/document.xml").decode()
    assert "commentRange" not in xml and "commentReference" not in xml and "Texto anotado con" in xml
    assert b"comments" not in part(der, "[Content_Types].xml") and b"thumbnail" not in part(der, "_rels/.rels")
    core = part(der, "docProps/core.xml").decode()
    assert "dcterms:created" in core and "title" not in core and "creator" not in core  # dates kept
    app = part(der, "docProps/app.xml").decode()
    assert "SyntheticWriter" in app and "<Pages>1</Pages>" in app and "Company" not in app
    assert verify(data, der, [("anotado", NAME, 0, M1)], [f"Texto anotado con {M1}", "Cabecera"])["ok"]


def test_unknown_comment_part_is_refused():  # AC-A07-30 (PC-01)
    data = Doc(body=[p(r(f"x {NAME}"))], extra_parts={"word/commentsFoo.xml": b"<x/>"}).build()
    assert protect(data, [("x", NAME, 0, M1)]) == {"failure": "unsupported_structure"}


def test_verifier_negative_controls():  # AC-A07-26, 32, 33
    data = Doc(body=[p(r(f"Uno {NAME}")), p(r("Dos"))]).build()
    picks = [("Uno", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    oracle = [f"Uno {M1}", "Dos"]
    assert verify(data, der, picks, oracle)["ok"]
    assert verify(data, data, picks, oracle)["failure"] in ("derivative_verification_failed", "residue_detected")
    lost = patch(der, "word/document.xml", lambda b: b.replace(b"Dos", b"Do"))
    assert verify(data, lost, picks, oracle) == {"failure": "derivative_verification_failed", "check": "oracle"}
    moved = patch(der, "word/document.xml", lambda b: b.replace(b"<w:t>Uno ", b"<w:t>Un o "))
    assert verify(data, moved, picks, oracle)["failure"] == "derivative_verification_failed"
    restyled = patch(
        der,
        "word/document.xml",
        lambda b: b.replace(b"<w:p><w:r><w:t>Dos", b'<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:t>Dos'),
    )
    assert verify(data, restyled, picks, oracle) == {"failure": "derivative_verification_failed", "check": "structure"}


@pytest.mark.parametrize(
    "where",
    ["alt_text", "hyperlink_target", "instr", "property"],
)
def test_copies_of_a_selected_value_block_publication(where):  # AC-A07-34
    body = [p(r(f"Hola {NAME}"))]
    doc = Doc(body=body, image=where == "alt_text")
    if where == "alt_text":
        body.append(p(image_run(descr=f"Foto de {NAME}")))
    if where == "hyperlink_target":
        doc.hyperlinks = {"rIdH": "https://example.test/u?n=Ana%20Ejemplo"}
        body.append(p(f'<w:hyperlink r:id="rIdH">{r("enlace")}</w:hyperlink>'))
    if where == "instr":
        body.append(
            p(
                '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
                '<w:r><w:instrText> SET x "Ana </w:instrText></w:r>',
                '<w:r><w:instrText>Ejemplo" </w:instrText></w:r>',
                '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
            )
        )
    if where == "property":
        doc.app = {"Pages": "1", "Words": NAME}  # kept element (structural statistics) holding a copy
    data = doc.build()
    picks = [("Hola", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    oracle = [u["text"].replace(f"Hola {NAME}", f"Hola {M1}") for u in units(data)]
    out = verify(data, der, picks, oracle)
    assert out["failure"] == "residue_detected", out


def test_permitted_occurrence_in_one_text_node_passes_every_target():  # AC-A07-35, 79
    data = Doc(body=[p(r(f"Seleccionado {NAME}")), p(r(f"Mencion libre de {NAME} aqui"))]).build()
    picks = [("Seleccionado", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    raw = part(der, "word/document.xml")
    assert raw.count(NAME.encode()) == 1  # the raw-bytes target sees it ...
    out = verify(data, der, picks, [f"Seleccionado {M1}", f"Mencion libre de {NAME} aqui"])
    assert out["ok"], out  # ... and it is attributed to its exact location
    assert out["inspection_scope"]["targets"]["K7"] >= 1


@pytest.mark.parametrize("extra", ["attribute", "instr", "xml_comment"])
def test_an_extra_copy_in_the_same_part_fails(extra):  # AC-A07-80
    data = Doc(body=[p(r(f"Seleccionado {NAME}")), p(r(f"Mencion libre de {NAME} aqui"))], image=True).build()
    picks = [("Seleccionado", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    if extra == "attribute":
        bad = patch(
            der,
            "word/document.xml",
            lambda b: b.replace(b"aqui</w:t></w:r></w:p>", b"aqui</w:t></w:r></w:p>").replace(
                b"<w:body>", f"<w:body><w:p>{image_run(descr=NAME)}</w:p>".encode()
            ),
        )
        bad_orig = patch(
            data,
            "word/document.xml",
            lambda b: b.replace(b"<w:body>", f"<w:body><w:p>{image_run(descr=NAME)}</w:p>".encode()),
        )
        oracle = ["", f"Seleccionado {M1}", f"Mencion libre de {NAME} aqui"]
        out = docx_verify.verify(
            bad_orig,
            bad,
            {
                "oracle": oracle,
                "values": [NAME],
                "subs": [[], [[13, 38]], []],
                "markers": [[], [[13, 38]], []],
                "mode": "protect",
            },
        )
    else:
        inject = (
            f'<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> SET {esc(NAME)} </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
            if extra == "instr"
            else f"<!-- {NAME} -->"
        ).encode()
        bad = patch(der, "word/document.xml", lambda b: b.replace(b"<w:body>", b"<w:body>" + inject))
        bad_orig = patch(data, "word/document.xml", lambda b: b.replace(b"<w:body>", b"<w:body>" + inject))
        oracle = ([""] if extra == "instr" else []) + [f"Seleccionado {M1}", f"Mencion libre de {NAME} aqui"]
        lead = [[]] if extra == "instr" else []
        out = docx_verify.verify(
            bad_orig,
            bad,
            {
                "oracle": oracle,
                "values": [NAME],
                "subs": lead + [[[13, 38]], []],
                "markers": lead + [[[13, 38]], []],
                "mode": "protect",
            },
        )
    assert out["failure"] == "residue_detected", out


def test_split_values_permitted_and_selected():  # AC-A07-81
    data = Doc(body=[p(r(f"Sel {NAME}")), p(r("Libre Ana "), r("Ejemplo", "<w:b/>"), r(" fin"))]).build()
    picks = [("Sel", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    ok = verify(data, der, picks, [f"Sel {M1}", f"Libre {NAME} fin"])
    assert ok["ok"], ok
    # a SELECTED value that reappears split between runs outside the permitted occurrences
    bad = patch(
        der, "word/document.xml", lambda b: b.replace(b"<w:t>Sel ", b"<w:t>Sel Ana </w:t></w:r><w:r><w:t>Ejemplo ")
    )
    out = verify(data, bad, picks, [f"Sel {M1}", f"Libre {NAME} fin"])
    assert out["failure"] in ("derivative_verification_failed", "residue_detected")


def test_representations_detected_where_a_byte_search_would_not():  # AC-A07-38
    data = Doc(body=[p(r(f"Hola {NAME}"))]).build()
    picks = [("Hola", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    variants = {
        "charref": "&#65;na &#69;jemplo",
        "nfd_casefold": "ANA EJEMPLO",
        "spaces": "Ana    Ejemplo",
    }
    for label, text in variants.items():
        inj = f"<w:p><w:r><w:instrText>{text}</w:instrText></w:r></w:p>".encode()
        bad = patch(der, "word/document.xml", lambda b, inj=inj: b.replace(b"<w:body>", b"<w:body>" + inj))
        orig = patch(data, "word/document.xml", lambda b, inj=inj: b.replace(b"<w:body>", b"<w:body>" + inj))
        assert NAME.encode() not in part(bad, "word/document.xml") or label == "spaces"  # naive search misses it
        out = docx_verify.verify(
            orig,
            bad,
            {
                "oracle": ["", f"Hola {M1}"],
                "values": [NAME],
                "subs": [[], [[5, 30]]],
                "markers": [[], [[5, 30]]],
                "mode": "protect",
            },
        )
        assert out["failure"] == "residue_detected", (label, out)


def test_all_occurrences_selected_means_absence_everywhere():  # AC-A07-36
    data = Doc(body=[p(r(f"Uno {NAME}")), p(r(f"Dos {NAME}"))], core={"dc:title": NAME}).build()
    picks = [("Uno", NAME, 0, M1), ("Dos", NAME, 0, M1)]
    der = derivative(protect(data, picks))
    for n in docx_verify.Pkg(der).raw.values():
        assert NAME.encode() not in n and NAME.encode("utf-16-le") not in n
    assert verify(data, der, picks, [f"Uno {M1}", f"Dos {M1}"])["ok"]


def test_document_without_values_is_unchanged_but_cleaned():  # AC-A07-37
    data = commented_doc().build()
    out = docx_transform.protect(data, LIM, [], check_only=False)
    der = derivative(out)
    assert [u["text"] for u in units(der)] == [u["text"] for u in units(data)]
    assert out["removed"]


# ---- restoration (pure) ------------------------------------------------------------------------------
def test_restore_puts_values_back_in_one_pass_and_e_stays_absent():  # AC-A07-40, 49, 50
    data = commented_doc().build()
    der = derivative(protect(data, [("anotado", NAME, 0, M1)]))
    du = units(der)
    occ = bd.occurrence_at(du[0], du[0]["text"].index(M1), du[0]["text"].index(M1) + 25)
    tricky = f"{NAME} {M2}"  # an original that contains marker syntax is inserted literally
    out = docx_transform.restore(der, LIM, [{**occ, "marker": M1, "value": tricky}])
    rest = base64.b64decode(out["restored"])
    expected = [f"Texto anotado con {tricky}", "Cabecera"]
    assert [u["text"] for u in units(rest)] == expected
    res = docx_verify.verify(der, rest, {"oracle": expected, "values": [], "subs": [[], []], "mode": "restore"})
    assert res["ok"], res
    assert "word/comments.xml" not in docx_verify.Pkg(rest).raw
    bad = docx_transform.restore(der, LIM, [{**occ, "marker": M2, "value": "x"}])
    assert bad == {"failure": "derivative_markers_invalid"}


def test_position_check_rejects_moved_added_removed_and_altered_markers():  # AC-A07-44 (a)
    data = Doc(body=[p(r(f"Uno {NAME} y {MAIL}"))]).build()
    der = derivative(protect(data, [("Uno", NAME, 0, M1), ("Uno", MAIL, 0, M2)]))
    us = units(der)
    text = us[0]["text"]
    occs = []
    for m in (M1, M2):
        o = bd.occurrence_at(us[0], text.index(m), text.index(m) + 25)
        o["token"] = m[7:23]
        occs.append(o)
    assert check_positions(us, occs)
    assert not check_positions(us, occs[:1])  # a marker not in the binding
    assert not check_positions(us, [*occs, {**occs[0], "start": 0, "end": 25}])  # a missing one
    assert not check_positions(us, [{**occs[0], "token": "0" * 16}, occs[1]])  # another token
    moved = [{**occs[0], "start": occs[0]["start"] + 1, "end": occs[0]["end"] + 1}, occs[1]]
    assert not check_positions(us, moved)
    altered = [dict(u) for u in us]
    altered[0] = {**us[0], "text": us[0]["text"].replace(M1, M1.lower())}
    assert not check_positions(altered, occs)


def test_mock_provenance_rules():  # AC-A07-82 (pure part)
    pinned = {
        "kind": "mock",
        "adapter_id": "cce-mock-fixtures",
        "adapter_version": "1",
        "configuration_id": "fxs-0123456789abcdef",
        "model_id": None,
        "model_version": None,
        "inference_contract_version": 1,
    }
    unit = {"status": "analyzed", "analysis": {"provenance": bd.projection(pinned)}}
    assert bd.projection(pinned)["simulated"] is True and bd.projection(pinned)["model"] is None
    assert bd.provenance_ok(dict(pinned), pinned, [unit, {"status": "empty"}])
    assert not bd.provenance_ok({**pinned, "configuration_id": "fxs-ffffffffffffffff"}, pinned, [unit])  # header
    bad_unit = {"status": "analyzed", "analysis": {"provenance": {**bd.projection(pinned), "simulated": False}}}
    assert not bd.provenance_ok(dict(pinned), pinned, [unit, bad_unit])  # one unit
    model = {**pinned, "kind": "model", "adapter_id": "cce-real", "model_id": "m", "model_version": "1"}
    model_unit = {"status": "analyzed", "analysis": {"provenance": bd.projection(model)}}
    assert bd.projection(model)["simulated"] is False and bd.projection(model)["model"] == {"id": "m", "version": "1"}
    assert not bd.provenance_ok(dict(model), model, [model_unit])  # a model analysis
    assert not bd.provenance_ok(dict(pinned), {**pinned, "adapter_id": "cce-a05-stub"}, [unit])


def test_binding_mac_detects_any_change():  # AC-A07-44 (b), pure part
    row = {
        "scope_id": "a",
        "derivative_id": "ddv_" + "0" * 32,
        "context_id": "ctx_" + "1" * 32,
        "context_kind": "job",
        "sha256": "ab" * 32,
        "size_bytes": 10,
        "extractor_id": "cce-docs-extract",
        "extractor_version": "1",
        "occurrences": [{"unit_id": "u0001", "start": 1, "end": 26, "token": "0123456789ABCDEF"}],
    }
    key = b"k" * 32
    mac = bd.binding_mac(key, row)
    assert bd.mac_ok(key, row, mac)
    for field_, val in (("sha256", "cd" * 32), ("size_bytes", 11), ("context_id", "ctx_" + "2" * 32)):
        assert not bd.mac_ok(key, {**row, field_: val}, mac)
    assert not bd.mac_ok(key, {**row, "occurrences": [{**row["occurrences"][0], "start": 2}]}, mac)
    assert not bd.mac_ok(b"x" * 32, row, mac)


def test_markers_in_input_are_found_by_the_a06_parser():  # AC-A07-11 (pure part)
    from cawbase_content_engine.protection.markers import scan

    assert scan(f"x {M1}").strict and scan("[[cce1:0123456789abcdef]]").altered
    assert not scan("[CCE report]").strict and not scan("[CCE report]").altered


_ = (re, t)
