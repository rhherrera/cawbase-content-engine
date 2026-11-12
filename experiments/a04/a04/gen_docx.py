"""Synthetic DOCX fixtures + independent manifests (the manifest is emitted BY CONSTRUCTION, never from the extractor)."""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

from a04.common import sha256_bytes
from a04.ooxml import CT, NS, Para, app, build_docx, core, xml

VALUES = {  # synthetic, evidently fake
    "v1": "Ana Ejemplo",
    "v2": "ana.ejemplo@example.test",
    "v3": "EjemploNoReal-123",
    "v4": "Zoë Café",  # NFC in text; NFD variant used in the unicode fixture
}


class Doc:
    def __init__(self, fid: str, note: str = ""):
        self.fid, self.note = fid, note
        self.parts_units: dict[str, list[Para]] = {}
        self.body_xml = ""
        self.extra_parts: dict[str, bytes] = {}
        self.extra_rels: list = []
        self.overrides: dict[str, str] = {}
        self.sectpr = "<w:sectPr/>"
        self.core_xml = None
        self.app_xml = None
        self.manifest_extra: dict = {}
        self.order: list[str] = ["word/document.xml"]
        self.selected: set[str] = set()
        self.kw: dict = {}

    def reg(self, part: str, p: Para) -> str:
        self.parts_units.setdefault(part, []).append(p)
        return p.xml()

    def build(self) -> tuple[bytes, dict]:
        data = build_docx(
            self.body_xml,
            extra_parts=self.extra_parts,
            extra_rels=self.extra_rels,
            overrides=self.overrides,
            sectpr=self.sectpr,
            core_xml=self.core_xml,
            app_xml=self.app_xml,
            **self.kw,
        )
        units, occs = [], []
        for part in self.order:
            for k, p in enumerate(self.parts_units.get(part, [])):
                if not p.text:
                    continue
                ui = len(units)
                units.append({"part": part, "text": p.text})
                for o in p.occs:
                    occs.append({**o, "part": part, "unit_index": ui, "selected": o["occ"] in self.selected})
        values = {o["value_id"]: VALUES.get(o["value_id"], o["text"]) for o in occs}
        by_value: dict[str, list] = {}
        for o in occs:
            by_value.setdefault(o["value_id"], []).append(o)
        man = {
            "fixture_id": self.fid,
            "kind": "docx",
            "file": f"{self.fid}.docx",
            "sha256": sha256_bytes(data),
            "note": self.note,
            "values": values,
            "expected_units": units,
            "occurrences": occs,
            "selected": sorted(self.selected),
            "global_absence_required": {v: all(o["selected"] for o in lst) for v, lst in by_value.items()},
            "expected": {"outcome": "transform"},
            **self.manifest_extra,
        }
        return data, man


def tc(content: str, span: int = 1, vmerge: str | None = None) -> str:
    pr = f'<w:gridSpan w:val="{span}"/>' if span > 1 else ""
    if vmerge is not None:
        pr += '<w:vMerge w:val="restart"/>' if vmerge == "restart" else "<w:vMerge/>"
    return f"<w:tc><w:tcPr>{pr}</w:tcPr>{content}</w:tc>"


def tbl(rows: list[list[str]], cols: int) -> str:
    grid = "<w:tblGrid>" + '<w:gridCol w:w="2000"/>' * cols + "</w:tblGrid>"
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>'
        + grid
        + "".join(f"<w:tr>{''.join(r)}</w:tr>" for r in rows)
        + "</w:tbl>"
    )


def hdr(tag: str, inner: str) -> bytes:
    return xml(f"<w:{tag} {NS}>{inner}</w:{tag}>")


def f_runs() -> (
    Doc
):  # EXP-01/05: value spread over runs, repeated identical unselected occurrence, global-absence counterpart
    d = Doc(
        "D01_runs", "value split in 3 runs with different formatting; second identical occurrence NOT selected (CN-02)"
    )
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    d.body_xml = (
        P(["Informe de prueba"], style="Heading1")
        + P(
            ["Contactar a <<v1.a|Ana ", "Ejem", "plo>> (<<v2.a|ana.ejemplo@example.test>>) hoy."],
            fmt=["", "b", "i", ""] if False else ["", "b", "i"],
        )
        + P(["Otra mención de Ana Ejemplo sin seleccionar: <<v1.b|Ana Ejemplo>>."])
        + P(["Clave: <<v3.a|EjemploNoReal-123>> fin."])
    )
    d.selected = {"v1.a", "v2.a", "v3.a"}
    return d


def f_tables() -> Doc:  # EXP-01: table, nested table, merged cells
    d = Doc("D02_tables", "simple table, nested table (depth 2), gridSpan and vMerge")
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    a = P(["Tabla:"])
    c11, c12 = P(["Nombre"]), P(["Dato"])
    c21 = P(["<<v1.a|Ana Ejemplo>>"])
    c22a = P(["x"])
    inner_p = P(["Interna <<v2.a|ana.ejemplo@example.test>>"])
    c22b = P(["tras anidada"])
    c31 = P(["Combinada <<v3.a|EjemploNoReal-123>>"])
    c41, c42 = P(["Fila vM"]), P(["c1"])
    c52 = P(["c2 <<v1.b|Ana Ejemplo>>"])
    inner = tbl([[tc(inner_p)]], 1)
    d.body_xml = a + tbl(
        [
            [tc(c11), tc(c12)],
            [tc(c21), tc(c22a + inner + c22b)],
            [tc(c31, span=2)],
            [tc(c41, vmerge="restart"), tc(c42)],
            [tc("<w:p/>", vmerge="cont"), tc(c52)],
        ],
        2,
    )
    d.selected = {"v1.a", "v2.a", "v3.a"}
    return d


def f_headers() -> Doc:  # EXP-01: headers/footers with variants, footnotes, endnotes
    d = Doc("D03_headers_notes", "default/first/even headers+footers, titlePg, evenAndOddHeaders, footnote, endnote")
    P = lambda part, *a, **k: d.reg(part, Para(*a, **k))  # noqa: E731
    d.body_xml = P("word/document.xml", ["Cuerpo con nota"]).replace(
        "</w:p>", '<w:r><w:footnoteReference w:id="1"/></w:r><w:r><w:endnoteReference w:id="1"/></w:r></w:p>'
    ) + P("word/document.xml", ["Cuerpo <<v1.c|Ana Ejemplo>> en el cuerpo."])
    d.extra_parts = {
        "word/header1.xml": hdr("hdr", P("word/header1.xml", ["Encabezado default <<v1.a|Ana Ejemplo>>"])),
        "word/header2.xml": hdr(
            "hdr", P("word/header2.xml", ["Encabezado primera pagina <<v2.a|ana.ejemplo@example.test>>"])
        ),
        "word/header3.xml": hdr("hdr", P("word/header3.xml", ["Encabezado par"])),
        "word/footer1.xml": hdr("ftr", P("word/footer1.xml", ["Pie default <<v3.a|EjemploNoReal-123>>"])),
        "word/footnotes.xml": hdr(
            "footnotes",
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
            f'<w:footnote w:id="1">{P("word/footnotes.xml", ["Nota al pie <<v1.b|Ana Ejemplo>>"])}</w:footnote>',
        ),
        "word/endnotes.xml": hdr(
            "endnotes",
            '<w:endnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:endnote>'
            '<w:endnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:endnote>'
            f'<w:endnote w:id="1">{P("word/endnotes.xml", ["Nota final <<v2.b|ana.ejemplo@example.test>>"])}</w:endnote>',
        ),
    }
    d.extra_rels = [
        ("rId10", "header", "header1.xml", False),
        ("rId11", "header", "header2.xml", False),
        ("rId12", "header", "header3.xml", False),
        ("rId13", "footer", "footer1.xml", False),
        ("rId14", "footnotes", "footnotes.xml", False),
        ("rId15", "endnotes", "endnotes.xml", False),
    ]
    for n, t in (
        ("header1", "hdr"),
        ("header2", "hdr"),
        ("header3", "hdr"),
        ("footer1", "ftr"),
        ("footnotes", "footnotes"),
        ("endnotes", "endnotes"),
    ):
        d.overrides[f"/word/{n}.xml"] = f"{CT}.{ {'hdr': 'header', 'ftr': 'footer'}.get(t, t) }+xml"
    d.sectpr = (
        '<w:sectPr><w:headerReference w:type="default" r:id="rId10"/><w:headerReference w:type="first" r:id="rId11"/>'
        '<w:headerReference w:type="even" r:id="rId12"/><w:footerReference w:type="default" r:id="rId13"/><w:titlePg/></w:sectPr>'
    )
    d.order = [
        "word/document.xml",
        "word/header1.xml",
        "word/header2.xml",
        "word/header3.xml",
        "word/footer1.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
    ]
    d.selected = {"v1.a", "v2.a", "v3.a", "v1.b", "v2.b", "v1.c"}
    return d


def f_unicode() -> Doc:  # EXP-03
    nfc = "Zoë Café"
    nfd = unicodedata.normalize("NFD", nfc)
    d = Doc(
        "D04_unicode",
        "NFC and NFD forms, emoji outside the BMP, ZWJ sequence, tab, line break, repeated identical value",
    )
    VALUES["v4"] = nfc
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    d.body_xml = (
        P([f"NFC: <<v4.a|{nfc}>> y NFD: <<v4.b|{nfd}>> fin"])
        + P(
            [
                "Emoji \U0001f600 antes de <<v1.a|Ana Ejemplo>> y familia \U0001f468‍\U0001f469‍\U0001f467 luego <<v1.b|Ana Ejemplo>>"
            ]
        )
        + P(["Tab\taqui <<v3.a|EjemploNoReal-123>>\ncon salto"])
        + P(["Con ligadura ﬁ y ﬃ: <<v2.a|ana.ejemplo@example.test>>"])
    )
    d.selected = {"v4.a", "v1.a", "v3.a", "v2.a"}  # v4.b (NFD) and v1.b not selected: precision of localization
    return d


def f_field() -> Doc:  # EXP-05/07: field result is refused for transformation
    d = Doc(
        "D05_field", "simple field and complex field whose result holds a value; instruction text also holds a value"
    )
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    p1 = Para(["Campo simple: <<v1.a|Ana Ejemplo>>"])
    d.parts_units.setdefault("word/document.xml", []).append(p1)
    simple = (
        f'<w:p><w:fldSimple w:instr=" DOCPROPERTY Author "><w:r><w:t>{VALUES["v1"]}</w:t></w:r></w:fldSimple></w:p>'
    )
    p2 = Para(["Compuesto: <<v2.a|ana.ejemplo@example.test>>"])
    d.parts_units["word/document.xml"].append(p2)
    complex_ = (
        '<w:p><w:r><w:t xml:space="preserve">Compuesto: </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> MERGEFIELD Email </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r><w:t>{VALUES["v2"]}</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    # the first unit text is "Campo simple: Ana Ejemplo" -> make the simple field paragraph produce exactly that text
    simple = (
        '<w:p><w:r><w:t xml:space="preserve">Campo simple: </w:t></w:r><w:fldSimple w:instr=" DOCPROPERTY Author ">'
        f"<w:r><w:t>{VALUES['v1']}</w:t></w:r></w:fldSimple></w:p>"
    )
    d.body_xml = simple + complex_ + P(["Texto normal <<v3.a|EjemploNoReal-123>>"])
    d.selected = {"v3.a"}
    d.manifest_extra = {
        "field_occurrences": ["v1.a", "v2.a"],
        "note_expected": "selecting v1.a or v2.a must be refused (field_result_not_transformable)",
    }
    return d


def f_comments_meta() -> Doc:  # EXP-06: authorized removals + auxiliary copies
    d = Doc(
        "D06_comments_meta",
        "comments (+extended, ids, people), core/app/custom properties and thumbnail carry values; body keeps visible text",
    )
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    body = P(["Texto anotado <<v1.a|Ana Ejemplo>> aqui."])
    body = body.replace(
        '<w:r><w:t xml:space="preserve">Texto anotado ',
        '<w:commentRangeStart w:id="0"/><w:r><w:t xml:space="preserve">Texto anotado ',
    ).replace(
        "</w:p>",
        '<w:commentRangeEnd w:id="0"/><w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="0"/></w:r></w:p>',
    )
    d.body_xml = body + P(["Otro parrafo sin valores."])
    d.extra_parts = {
        "word/comments.xml": xml(
            f'<w:comments {NS}><w:comment w:id="0" w:author="Ana Ejemplo" w:date="2026-01-01T00:00:00Z" w:initials="AE">'
            "<w:p><w:r><w:t>Revisar el correo ana.ejemplo@example.test</w:t></w:r></w:p></w:comment></w:comments>"
        ),
        "word/commentsExtended.xml": xml(
            '<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"><w15:commentEx w15:paraId="00000001" w15:done="0"/></w15:commentsEx>'
        ),
        "word/commentsIds.xml": xml(
            '<w16cid:commentsIds xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid"><w16cid:commentId w16cid:paraId="00000001" w16cid:durableId="00000001"/></w16cid:commentsIds>'
        ),
        "word/people.xml": xml(
            '<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"><w15:person w15:author="Ana Ejemplo"><w15:presenceInfo w15:providerId="None" w15:userId="Ana Ejemplo"/></w15:person></w15:people>'
        ),
        "docProps/custom.xml": xml(
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="Responsable">'
            "<vt:lpwstr>Ana Ejemplo</vt:lpwstr></property></Properties>"
        ),
        "docProps/thumbnail.png": b"\x89PNG\r\n\x1a\n" + b"thumbnail-for-Ana Ejemplo" + b"\x00" * 8,
    }
    d.extra_rels = [
        ("rId20", "comments", "comments.xml", False),
        (
            "rId21",
            "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
            "commentsExtended.xml",
            False,
        ),
        ("rId22", "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds", "commentsIds.xml", False),
        ("rId23", "http://schemas.microsoft.com/office/2011/relationships/people", "people.xml", False),
    ]
    d.overrides.update(
        {
            "/word/comments.xml": f"{CT}.comments+xml",
            "/word/commentsExtended.xml": f"{CT}.commentsExtended+xml",
            "/word/commentsIds.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
            "/word/people.xml": f"{CT}.people+xml",
            "/docProps/custom.xml": "application/vnd.openxmlformats-officedocument.custom-properties+xml",
        }
    )
    d.kw = {
        "root_rels_extra": '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/>'
        '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail" Target="docProps/thumbnail.png"/>',
        "defaults_extra": '<Default Extension="png" ContentType="image/png"/>',
    }
    d.core_xml = core(
        title="Informe de Ana Ejemplo",
        creator="Ana Ejemplo",
        last="ana.ejemplo@example.test",
        subject="EjemploNoReal-123",
        keywords="Ana Ejemplo",
    )
    d.app_xml = app(company="Ejemplo SA")
    d.selected = {"v1.a"}
    d.manifest_extra = {
        "removal_expected": {
            "classes": [
                "comments",
                "authorship",
                "descriptive_properties",
                "company",
                "custom_properties",
                "thumbnail",
            ],
            "parts_removed": [
                "word/comments.xml",
                "word/commentsExtended.xml",
                "word/commentsIds.xml",
                "word/people.xml",
                "docProps/custom.xml",
                "docProps/thumbnail.png",
            ],
            "residue_values": ["Ana Ejemplo", "ana.ejemplo@example.test", "EjemploNoReal-123", "Ejemplo SA"],
            "visible_text_kept": ["Texto anotado", "Otro parrafo sin valores."],
        }
    }
    return d


def f_aux_copy() -> Doc:  # CN-05: values copied into auxiliary non-authorized places
    d = Doc(
        "D07_aux_copy",
        "selected value also copied in hyperlink target, image alt text and docVars-like settings; verification must detect",
    )
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    visible = P(["Visible <<v1.a|Ana Ejemplo>> aqui."])
    d.parts_units["word/document.xml"].append(Para(["enlace"]))
    link = '<w:p><w:hyperlink r:id="rId30"><w:r><w:t>enlace</w:t></w:r></w:hyperlink></w:p>'
    pic = (
        '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
        '<wp:docPr id="1" name="Imagen" descr="Foto de Ana Ejemplo"/></wp:inline></w:drawing></w:r></w:p>'
    )
    d.body_xml = visible + link + pic
    d.extra_rels = [("rId30", "hyperlink", "https://example.test/u?n=Ana%20Ejemplo", True)]
    d.selected = {"v1.a"}
    d.manifest_extra = {
        "aux_copies": [
            {"value_id": "v1", "where": "word/_rels/document.xml.rels hyperlink Target (percent-encoded)"},
            {"value_id": "v1", "where": "word/document.xml wp:docPr@descr"},
        ],
        "expected": {"outcome": "transform", "post_verification": "residue_detected"},
    }
    return d


def f_all_selected() -> Doc:  # CN-03
    d = Doc("D08_all_selected", "every occurrence of v1 selected: global absence of v1 is required")
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    d.body_xml = P(["<<v1.a|Ana Ejemplo>> y de nuevo <<v1.b|Ana Ejemplo>>."]) + P(["Final <<v1.c|Ana Ejemplo>>"])
    d.selected = {"v1.a", "v1.b", "v1.c"}
    return d


def f_no_values() -> Doc:  # CN-01
    d = Doc("D09_no_values", "document without the selected values (selection references values that do not occur)")
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    d.body_xml = P(["Texto sin ningun valor sintetico."]) + P(["Otro parrafo neutro."])
    d.manifest_extra = {
        "values_not_present": {"v1": VALUES["v1"]},
        "expected": {"outcome": "transform", "no_occurrences": True},
    }
    return d


def f_sdt() -> Doc:
    d = Doc("D10_sdt", "block and inline content controls with plain text")
    P = lambda *a, **k: d.reg("word/document.xml", Para(*a, **k))  # noqa: E731
    d.body_xml = f'<w:sdt><w:sdtPr><w:alias w:val="Campo"/></w:sdtPr><w:sdtContent>{P(["Control <<v1.a|Ana Ejemplo>> fin"])}</w:sdtContent></w:sdt>'
    d.selected = {"v1.a"}
    return d


OK_FIXTURES = [
    f_runs,
    f_tables,
    f_headers,
    f_unicode,
    f_field,
    f_comments_meta,
    f_aux_copy,
    f_all_selected,
    f_no_values,
    f_sdt,
]


def write_all(outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    mans = []
    for fn in OK_FIXTURES:
        d = fn()
        data, man = d.build()
        (outdir / man["file"]).write_bytes(data)
        (outdir / f"{man['fixture_id']}.manifest.json").write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n")
        mans.append(man)
    return mans


if __name__ == "__main__":
    out = Path(sys.argv[1])
    for m in write_all(out):
        print(m["fixture_id"], len(m["expected_units"]), "units", len(m["occurrences"]), "occs", m["sha256"][:12])
