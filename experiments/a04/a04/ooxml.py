"""Raw OOXML writer used ONLY to generate synthetic DOCX fixtures (independent of the transformer under test)."""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = f'xmlns:w="{W}" xmlns:r="{R}"'
CT = "application/vnd.openxmlformats-officedocument.wordprocessingml"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

MARK = re.compile(r"<<([A-Za-z0-9_.]+)\|(.*?)>>", re.S)


def xml(body: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body).encode("utf-8")


class Para:
    """A paragraph built from run strings; ``<<occ|text>>`` markers may span runs and define occurrences."""

    def __init__(
        self,
        runs: list[str],
        *,
        fmt: list[str] | None = None,
        style: str | None = None,
        extra_run_xml: dict | None = None,
    ):
        self.runs, self.fmt, self.style = runs, fmt or [""] * len(runs), style
        joined = "".join(runs)
        self.text, self.occs = "", []
        # strip markers from the concatenated text, recording [start,end) in the clean text
        idx = 0
        clean = []
        for m in MARK.finditer(joined):
            clean.append(joined[idx : m.start()])
            start = sum(len(c) for c in clean)
            clean.append(m.group(2))
            self.occs.append(
                {
                    "occ": m.group(1),
                    "value_id": m.group(1).split(".")[0],
                    "start": start,
                    "end": start + len(m.group(2)),
                    "text": m.group(2),
                }
            )
            idx = m.end()
        clean.append(joined[idx:])
        self.text = "".join(clean)

    def _clean_runs(self) -> list[str]:
        # remove markers run by run (markers may open in one run and close in another)
        joined_runs = self.runs
        out, open_ = [], False
        for r in joined_runs:
            s, res, i = r, "", 0
            while i < len(s):
                if s.startswith("<<", i) and (m := re.match(r"<<[A-Za-z0-9_.]+\|", s[i:])):
                    open_ = True
                    i += m.end()
                elif s.startswith(">>", i) and open_:
                    open_ = False
                    i += 2
                else:
                    res += s[i]
                    i += 1
            out.append(res)
        return out

    def xml(self) -> str:
        ppr = f'<w:pPr><w:pStyle w:val="{self.style}"/></w:pPr>' if self.style else ""
        rs = ""
        for t, f in zip(self._clean_runs(), self.fmt):
            rs += run_xml(t, f)
        return f"<w:p>{ppr}{rs}</w:p>"


def run_xml(text: str, fmt: str = "") -> str:
    rpr = ""
    if fmt:
        rpr = "<w:rPr>" + ("<w:b/>" if "b" in fmt else "") + ("<w:i/>" if "i" in fmt else "") + "</w:rPr>"
    parts = re.split(r"(\t|\n)", text)
    body = ""
    for p in parts:
        if p == "\t":
            body += "<w:tab/>"
        elif p == "\n":
            body += "<w:br/>"
        elif p:
            body += f'<w:t xml:space="preserve">{escape(p)}</w:t>'
    return f"<w:r>{rpr}{body}</w:r>"


STYLES = xml(
    f'<w:styles {NS}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Liberation Sans" w:hAnsi="Liberation Sans"/>'
    '<w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>'
    '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
    '<w:top w:val="single" w:sz="4"/><w:left w:val="single" w:sz="4"/><w:bottom w:val="single" w:sz="4"/><w:right w:val="single" w:sz="4"/>'
    '<w:insideH w:val="single" w:sz="4"/><w:insideV w:val="single" w:sz="4"/></w:tblBorders></w:tblPr></w:style></w:styles>'
)
SETTINGS = xml(f'<w:settings {NS}><w:zoom w:percent="100"/><w:defaultTabStop w:val="720"/></w:settings>')
FONTTABLE = xml(f'<w:fonts {NS}><w:font w:name="Liberation Sans"><w:charset w:val="00"/></w:font></w:fonts>')


def core(title="", creator="", last="", subject="", keywords="", desc="") -> bytes:
    return xml(
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title><dc:subject>{escape(subject)}</dc:subject><dc:creator>{escape(creator)}</dc:creator>"
        f"<cp:keywords>{escape(keywords)}</cp:keywords><dc:description>{escape(desc)}</dc:description>"
        f"<cp:lastModifiedBy>{escape(last)}</cp:lastModifiedBy><cp:revision>1</cp:revision>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:modified></cp:coreProperties>'
    )


def app(company="") -> bytes:
    return xml(
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f"<Application>Synthetic</Application><Company>{escape(company)}</Company><AppVersion>16.0000</AppVersion></Properties>"
    )


def build_docx(
    body_xml: str,
    *,
    extra_parts: dict[str, bytes] | None = None,
    extra_rels: list[tuple[str, str, str, bool]] | None = None,  # (id, type-suffix, target, external)
    overrides: dict[str, str] | None = None,
    sectpr: str = "<w:sectPr/>",
    core_xml: bytes | None = None,
    app_xml: bytes | None = None,
    root_rels_extra: str = "",
    raw_content_types_extra: str = "",
    defaults_extra: str = "",
) -> bytes:
    parts: dict[str, bytes] = {}
    ov = {
        "/word/document.xml": f"{CT}.document.main+xml",
        "/word/styles.xml": f"{CT}.styles+xml",
        "/word/settings.xml": f"{CT}.settings+xml",
        "/word/fontTable.xml": f"{CT}.fontTable+xml",
        "/docProps/core.xml": "application/vnd.openxmlformats-package.core-properties+xml",
        "/docProps/app.xml": "application/vnd.openxmlformats-officedocument.extended-properties+xml",
    }
    ov.update(overrides or {})
    rels = [
        ("rId1", "styles", "styles.xml", False),
        ("rId2", "settings", "settings.xml", False),
        ("rId3", "fontTable", "fontTable.xml", False),
    ] + list(extra_rels or [])
    parts["word/document.xml"] = xml(f"<w:document {NS}><w:body>{body_xml}{sectpr}</w:body></w:document>")
    parts["word/styles.xml"] = STYLES
    parts["word/settings.xml"] = SETTINGS
    parts["word/fontTable.xml"] = FONTTABLE
    parts["word/_rels/document.xml.rels"] = xml(
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="{i}" Type="{t if t.startswith("http") else REL + "/" + t}" Target="{escape(tg)}"'
            + (' TargetMode="External"' if ext else "")
            + "/>"
            for i, t, tg, ext in rels
        )
        + "</Relationships>"
    )
    parts["docProps/core.xml"] = core_xml or core()
    parts["docProps/app.xml"] = app_xml or app()
    parts["_rels/.rels"] = xml(
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        f'<Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/>'
        + root_rels_extra
        + "</Relationships>"
    )
    parts.update(extra_parts or {})
    ct = (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + defaults_extra
        + "".join(f'<Override PartName="{k}" ContentType="{v}"/>' for k, v in ov.items())
        + raw_content_types_extra
        + "</Types>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", xml(ct))
        for name in ["_rels/.rels"] + [n for n in parts if n != "_rels/.rels"]:
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, parts[name])
    return buf.getvalue()
