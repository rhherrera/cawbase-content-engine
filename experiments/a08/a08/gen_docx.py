"""Generates the four source .docx of the manifest (T1..T4) with python-docx + raw OOXML where python-docx has no API.

    .venv/bin/python -m a08.gen_docx out_dir

Deterministic: fixed core properties (so W/G/L sources are byte-identical across runs) and fixed zip timestamps.
"""

from __future__ import annotations

import copy
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Mm, Pt, RGBColor

from .manifest import load, runs_of

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
FOOTNOTES_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"


def _font(run_or_style, name: str | None) -> None:
    if not name:
        return
    f = run_or_style.font
    f.name = name
    rpr = run_or_style.element.get_or_add_rPr() if hasattr(run_or_style.element, "get_or_add_rPr") else None
    if rpr is not None:
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = parse_xml(f"<w:rFonts {nsdecls('w')}/>")
            rpr.insert(0, rf)
        for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rf.set(qn(a), name)


def _style_font(doc, style: str, name: str | None, size: float | None = None, bold: bool | None = None) -> None:
    st = doc.styles[style]
    if name:
        st.font.name = name
        rpr = st.element.get_or_add_rPr()
        rf = rpr.find(qn("w:rFonts"))
        if rf is None:
            rf = parse_xml(f"<w:rFonts {nsdecls('w')}/>")
            rpr.insert(0, rf)
        for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rf.set(qn(a), name)
        for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
            if rf.get(qn(a)) is not None:
                del rf.attrib[qn(a)]
    if size:
        st.font.size = Pt(size)
    if bold is not None:
        st.font.bold = bold
    st.font.color.rgb = RGBColor(0, 0, 0)


def _add_field(par, instr: str) -> None:
    for kind, text in (("begin", None), ("instr", instr), ("separate", None), ("result", "1"), ("end", None)):
        r = par.add_run()
        if kind == "instr":
            el = parse_xml(f'<w:instrText {nsdecls("w")} xml:space="preserve"> {text} </w:instrText>')
            r._r.append(el)
        elif kind == "result":
            r.text = text
        else:
            r._r.append(parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="{kind}"/>'))


def _runs(par, runs, link_parts: list) -> None:
    for text, style in runs:
        if style.startswith("link:"):
            url = style[5:]
            rid = par.part.relate_to(url, RT.HYPERLINK, is_external=True)
            h = parse_xml(
                f'<w:hyperlink {nsdecls("w", "r")} r:id="{rid}"><w:r><w:rPr><w:color w:val="1F4E9A"/><w:u w:val="single"/></w:rPr>'
                f'<w:t xml:space="preserve">{text}</w:t></w:r></w:hyperlink>'
            )
            par._p.append(h)
            continue
        r = par.add_run(text)
        if style == "b":
            r.bold = True


def _borders(table) -> None:
    tblPr = table._tbl.tblPr
    tblPr.append(
        parse_xml(
            f"<w:tblBorders {nsdecls('w')}>"
            + "".join(f'<w:{e} w:val="single" w:sz="4" w:space="0" w:color="000000"/>' for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
            + "</w:tblBorders>"
        )
    )


def _shade_cell(cell, fill: str) -> None:
    cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill.lstrip("#")}"/>'))


def _shade_par(par, fill: str) -> None:
    par._p.get_or_add_pPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill.lstrip("#")}"/>'))


def _cols(section, n: int) -> None:
    sectPr = section._sectPr
    for c in sectPr.findall(qn("w:cols")):
        sectPr.remove(c)
    sectPr.append(parse_xml(f'<w:cols {nsdecls("w")} w:num="{n}" w:space="567"/>'))


class _Footnotes:
    def __init__(self, doc) -> None:
        self.doc = doc
        self.font: str | None = None
        self.notes: list[str] = []

    def add(self, par, text: str) -> None:
        nid = len(self.notes) + 1
        self.notes.append(text)
        r = par.add_run()
        r._r.append(parse_xml(f'<w:rPr {nsdecls("w")}><w:vertAlign w:val="superscript"/></w:rPr>'))
        r._r.append(parse_xml(f'<w:footnoteReference {nsdecls("w")} w:id="{nid}"/>'))

    def _rf(self) -> str:
        f = self.font
        return f'<w:rFonts w:ascii="{f}" w:hAnsi="{f}" w:cs="{f}" w:eastAsia="{f}"/>' if f else ""

    def finish(self) -> None:
        if not self.notes:
            return
        body = (
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>'
        )
        for i, t in enumerate(self.notes, 1):
            body += (
                f'<w:footnote w:id="{i}"><w:p><w:pPr><w:pStyle w:val="Normal"/><w:spacing w:after="0"/></w:pPr>'
                f'<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r>'
                f'<w:r><w:rPr>{self._rf()}<w:sz w:val="18"/></w:rPr><w:t xml:space="preserve"> {t}</w:t></w:r></w:p></w:footnote>'
            )
        xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:footnotes xmlns:w="{W_NS}">{body}</w:footnotes>'
        part = Part(PackURI("/word/footnotes.xml"), FOOTNOTES_CT, xml.encode("utf-8"), self.doc.part.package)
        self.doc.part.relate_to(part, RT.FOOTNOTES)


def _all_paragraphs(doc):
    yield from doc.paragraphs
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                yield from c.paragraphs


def _in_table(par) -> bool:
    return par._p.getparent() is not None and par._p.getparent().tag == qn("w:tc")


def build(template_id: str, m: dict) -> bytes:
    t = m["templates"][template_id]
    default_font = t.get("font") == "exporter_default"
    body_font = None if default_font else "Noto Serif"
    head_font = None if default_font else "Noto Sans"
    doc = Document()
    _style_font(doc, "Normal", body_font, 11)
    pf = doc.styles["Normal"].paragraph_format
    pf.line_spacing = 1.15
    pf.space_after = Pt(6)
    for st, size in (("Heading 1", 16), ("Heading 2", 13)):
        _style_font(doc, st, head_font, size, True)
    for st in ("List Bullet", "List Number"):
        _style_font(doc, st, body_font, 11)
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    for k in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(sec, k, Mm(25))
    sec.header_distance = sec.footer_distance = Mm(12)
    fn = _Footnotes(doc)
    header = next((b for p in t["pages"] for b in p if b.get("type") == "header"), None)
    if header:
        hp = sec.header.paragraphs[0]
        hp.text = header["text"]
        hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for r in hp.runs:
            _font(r, head_font)
            r.font.size = Pt(9)
    if t.get("footer"):
        fp = sec.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp.add_run("Página ")
        _add_field(fp, "PAGE")
        fp.add_run(" de ")
        _add_field(fp, "NUMPAGES")
        for r in fp.runs:
            r.font.size = Pt(9)
            _font(r, head_font)
    links: list = []
    first = True
    for page in t["pages"]:
        if not first:
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        first = False
        for b in page:
            _block(doc, b, m, fn, links, head_font)
    if body_font:  # explicit run-level fonts: AppKit (family M) ignores style-level fonts; Word/Docs/Writer render the same
        for par in _all_paragraphs(doc):
            name = head_font if (par.style is not None and par.style.name.startswith("Heading")) or _in_table(par) else body_font
            for r in par.runs:
                if r.element.rPr is None or r.element.rPr.find(qn("w:rFonts")) is None:
                    _font(r, name)
        fn.font = body_font
    fn.finish()
    doc.core_properties.author = "Autoría de ejemplo"
    doc.core_properties.title = f"Corpus A08 {template_id} (sintético)"
    doc.core_properties.subject = "Documento sintético de pruebas"
    doc.core_properties.keywords = "a08, sintético"
    doc.core_properties.last_modified_by = "Autoría de ejemplo"
    fixed = datetime(2026, 11, 18, 12, 0, 0)
    doc.core_properties.created = doc.core_properties.modified = fixed
    doc.core_properties.revision = 1
    buf = io.BytesIO()
    doc.save(buf)
    return _normalize_zip(buf.getvalue())


def _block(doc, b, m, fn, links, head_font) -> None:
    ty = b["type"]
    if ty == "header":
        return
    if ty == "heading":
        doc.add_heading(b["text"], level=b["level"])
    elif ty == "paragraph":
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY if b.get("align", "justify") == "justify" else WD_ALIGN_PARAGRAPH.LEFT
        _runs(p, runs_of(b, m), links)
        if "note" in b:
            fn.add(p, b["note"]["text"])
    elif ty == "table":
        rows = b["rows"]
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        if b.get("borders"):
            _borders(table)
        for i, row in enumerate(rows):
            for j, cell_runs in enumerate(row):
                c = table.cell(i, j)
                p = c.paragraphs[0]
                p.paragraph_format.space_after = Pt(2)
                _runs(p, [list(x) for x in cell_runs], links)
                if i == 0 and b.get("header_row"):
                    for r in p.runs:
                        r.bold = True
                for r in p.runs:
                    _font(r, head_font)
        doc.add_paragraph()
    elif ty == "columns":
        s = doc.add_section(WD_SECTION.CONTINUOUS)
        _cols(s, b["count"])
        for ci, col in enumerate(b["columns"]):
            if ci:
                doc.add_paragraph().add_run().add_break(WD_BREAK.COLUMN)
            for cb in col:
                _block(doc, cb, m, fn, links, head_font)
        s2 = doc.add_section(WD_SECTION.CONTINUOUS)
        _cols(s2, 1)
    elif ty == "shaded_box":
        table = doc.add_table(rows=1, cols=1)
        _borders(table)
        c = table.cell(0, 0)
        _shade_cell(c, b["fill"])
        first = True
        for cb in b["content"]:
            p = c.paragraphs[0] if first else c.add_paragraph()
            first = False
            _runs(p, runs_of(cb, m), links)
        doc.add_paragraph()
    elif ty == "shaded_band":
        for cb in b["content"]:
            if cb["type"] == "heading":
                h = doc.add_heading(cb["text"], level=cb["level"])
                _shade_par(h, b["fill"])
    elif ty == "bullet_list" or ty == "numbered_list":
        st = "List Bullet" if ty == "bullet_list" else "List Number"
        for item in b["items"]:
            p = doc.add_paragraph(style=st)
            _runs(p, item, links)
    elif ty == "horizontal_rule":
        p = doc.add_paragraph()
        p._p.get_or_add_pPr().append(
            parse_xml(f'<w:pBdr {nsdecls("w")}><w:bottom w:val="single" w:sz="6" w:space="1" w:color="000000"/></w:pBdr>')
        )
    else:
        raise ValueError(ty)


def _normalize_zip(data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=(2026, 11, 18, 12, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            z.writestr(zi, src.read(info.filename))
    return out.getvalue()


_ = copy  # keep import list stable


def main(out: str) -> None:
    m = load()
    o = Path(out)
    o.mkdir(parents=True, exist_ok=True)
    for tid in ("T1", "T2", "T3", "T4"):
        (o / f"{tid}.docx").write_bytes(build(tid, m))
        print(tid, (o / f"{tid}.docx"))


if __name__ == "__main__":
    main(sys.argv[1])
