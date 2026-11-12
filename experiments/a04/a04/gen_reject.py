"""Fixtures for rejection families / partial coverage (EXP-07, CN-07). Expected outcomes come from the generator, not the tool."""

from __future__ import annotations

import io
import json
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import pikepdf

from a04.common import sha256_bytes
from a04.ooxml import CT, NS, Para, build_docx, xml
from a04.pdfgen import Pdf, text_obj

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
body = lambda *t: "".join(Para([x]).xml() for x in t)  # noqa: E731
BASE = body("Texto base sin valores sensibles.", "Segundo parrafo.")


def docx_case(fid, note, expected, **kw):
    kw.setdefault("body_xml", BASE)
    data = kw.pop("raw", None) or build_docx(**kw)
    return fid, data, {"kind": "docx", "expected": expected, "note": note}


def dx() -> list:
    out = []
    ins = '<w:p><w:ins w:id="1" w:author="Ana Ejemplo" w:date="2026-01-01T00:00:00Z"><w:r><w:t>Insertado</w:t></w:r></w:ins><w:del w:id="2" w:author="Ana Ejemplo"><w:r><w:delText>borrado</w:delText></w:r></w:del></w:p>'
    out.append(
        docx_case("R01_revisions", "pending tracked changes (w:ins / w:del)", {"reject": ["revisions"]}, body_xml=ins)
    )
    out.append(
        docx_case(
            "R02_hidden_text",
            "w:vanish run",
            {"reject": ["hidden_text"]},
            body_xml="<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>oculto</w:t></w:r></w:p>",
        )
    )
    tb = (
        '<w:p><w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Choice Requires="wps">'
        "<w:txbxContent><w:p><w:r><w:t>en cuadro</w:t></w:r></w:p></w:txbxContent></mc:Choice><mc:Fallback><w:pict><w:txbxContent><w:p><w:r><w:t>en cuadro</w:t></w:r></w:p></w:txbxContent></w:pict></mc:Fallback></mc:AlternateContent></w:r></w:p>"
    )
    out.append(
        docx_case("R03_text_box", "text box with Choice/Fallback duplicate", {"reject": ["text_box"]}, body_xml=tb)
    )
    out.append(
        docx_case(
            "R04_custom_xml",
            "customXml part",
            {"reject": ["unknown_relationship", "embedded_or_unknown_part"]},
            extra_parts={"customXml/item1.xml": xml("<root>dato</root>")},
            extra_rels=[("rId40", "customXml", "../customXml/item1.xml", False)],
        )
    )
    out.append(
        docx_case(
            "R05_ole_object",
            "embedded OLE payload",
            {"reject": ["unknown_relationship", "embedded_or_unknown_part"]},
            extra_parts={"word/embeddings/oleObject1.bin": b"\xd0\xcf\x11\xe0" + b"\x00" * 32},
            extra_rels=[("rId41", "oleObject", "embeddings/oleObject1.bin", False)],
            defaults_extra='<Default Extension="bin" ContentType="application/vnd.openxmlformats-officedocument.oleObject"/>',
        )
    )
    chart = (
        '<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart"/></a:graphic></wp:inline></w:drawing></w:r></w:p>'
    )
    out.append(docx_case("R06_chart", "chart graphic", {"reject": ["chart_or_diagram"]}, body_xml=chart))
    out.append(
        docx_case(
            "R07_external_rel",
            "external attachedTemplate relationship",
            {"reject": ["unknown_relationship", "external_reference"]},
            extra_rels=[("rId42", "attachedTemplate", "file:///C:/plantilla.dotm", True)],
        )
    )
    fld = '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> INCLUDETEXT "otro.docx" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>res</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    out.append(docx_case("R08_external_field", "INCLUDETEXT field", {"reject": ["external_field"]}, body_xml=fld))
    out.append(
        docx_case(
            "R09_encrypted",
            "OLE compound file (EncryptedPackage container) instead of a ZIP",
            {"reject": ["encrypted_or_legacy"]},
            raw=bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 512,
        )
    )
    out.append(
        docx_case(
            "R10_signed",
            "digital signature origin + signature part",
            {"reject": ["signed"]},
            extra_parts={"_xmlsignatures/origin.sigs": b"", "_xmlsignatures/sig1.xml": xml("<Signature/>")},
            root_rels_extra='<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/package/2006/relationships/digital-signature/origin" Target="_xmlsignatures/origin.sigs"/>',
            defaults_extra='<Default Extension="sigs" ContentType="application/vnd.openxmlformats-package.digital-signature-origin"/>',
        )
    )
    out.append(
        docx_case(
            "R11_macros",
            "vbaProject.bin",
            {"reject": ["unknown_relationship", "embedded_or_unknown_part"]},
            extra_parts={"word/vbaProject.bin": b"\xd0\xcf\x11\xe0" + b"\x00" * 16},
            extra_rels=[
                ("rId43", "http://schemas.microsoft.com/office/2006/relationships/vbaProject", "vbaProject.bin", False)
            ],
            defaults_extra='<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>',
        )
    )
    good = build_docx(BASE)
    out.append(
        docx_case(
            "R12_corrupt_zip",
            "truncated ZIP (central directory lost)",
            {"reject": ["corrupt_or_unreadable"]},
            raw=good[: len(good) // 2],
        )
    )
    # bombs: ~60 MB of zeros deflate to ~60 KB (ratio ~1000:1)
    zeros = b"\x00" * (60 * 1024 * 1024)
    out.append(
        docx_case(
            "R13_zip_bomb_declared",
            "member declares and produces 60 MiB (declared-size check)",
            {"reject": ["limit_exceeded"]},
            extra_parts={"word/media/bomb.png": PNG + zeros},
            defaults_extra='<Default Extension="png" ContentType="image/png"/>',
        )
    )
    out.append(
        docx_case(
            "R14_zip_bomb_lying_header",
            "declared sizes patched to 1000 bytes; real output is 60 MiB (produced-bytes check)",
            {"reject": ["limit_exceeded"]},
            raw=lying_zip(PNG + zeros),
        )
    )
    dt = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>'
    out.append(
        docx_case(
            "R15_xml_entity_bomb",
            "DOCTYPE with nested entities in document.xml",
            {"reject": ["xml_doctype"]},
            raw=replace_part(good, "word/document.xml", dt),
        )
    )
    out.append(
        docx_case(
            "R16_orphan_header",
            "header part present but not referenced by any section",
            {"reject": ["orphan_header_footer"]},
            extra_parts={"word/header9.xml": xml(f"<w:hdr {NS}><w:p><w:r><w:t>huerfano</w:t></w:r></w:p></w:hdr>")},
            extra_rels=[("rId44", "header", "header9.xml", False)],
            overrides={"/word/header9.xml": f"{CT}.header+xml"},
        )
    )
    out.append(
        docx_case(
            "R17_unknown_part",
            "vendor part with no rule",
            {"reject": ["unknown_part"]},
            extra_parts={"word/vendor.xml": xml("<v/>")},
        )
    )
    sdt = '<w:sdt><w:sdtPr><w:dataBinding w:xpath="/a/b" w:storeItemID="{00000000-0000-0000-0000-000000000000}"/></w:sdtPr><w:sdtContent><w:p><w:r><w:t>enlazado</w:t></w:r></w:p></w:sdtContent></w:sdt>'
    out.append(
        docx_case(
            "R18_data_bound_sdt", "content control bound to customXml", {"reject": ["data_bound_control"]}, body_xml=sdt
        )
    )
    out.append(
        docx_case(
            "R19_settings_docvars",
            "settings.xml carries w:docVars",
            {"reject": ["settings_payload_or_external"]},
            raw=replace_part(
                good,
                "word/settings.xml",
                xml(f'<w:settings {NS}><w:docVars><w:docVar w:name="k" w:val="valor libre"/></w:docVars></w:settings>'),
            ),
        )
    )
    num = xml(
        f'<w:numbering {NS}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:lvlText w:val="Articulo %1 del Sr. Ejemplo"/></w:lvl></w:abstractNum></w:numbering>'
    )
    out.append(
        docx_case(
            "R20_numbering_free_text",
            "numbering lvlText with free text: P inspection reclassifies to N (partial coverage, not reject)",
            {"partial": ["word/numbering.xml"]},
            extra_parts={"word/numbering.xml": num},
            extra_rels=[("rId45", "numbering", "numbering.xml", False)],
            overrides={"/word/numbering.xml": f"{CT}.numbering+xml"},
        )
    )
    out.append(
        docx_case(
            "R21_image_partial",
            "valid PNG image (N): processable, document_coverage_complete must be false",
            {"partial": ["word/media/image1.png"]},
            extra_parts={"word/media/image1.png": PNG},
            extra_rels=[("rId46", "image", "media/image1.png", False)],
            defaults_extra='<Default Extension="png" ContentType="image/png"/>',
        )
    )
    out.append(
        docx_case(
            "R22_equation_partial",
            "OMML equation (N)",
            {"partial": ["equation"]},
            body_xml='<w:p><m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>x=1</m:t></m:r></m:oMath></w:p>',
        )
    )
    out.append(
        docx_case(
            "R23_media_type_mismatch",
            "word/media/x.png that is not an image",
            {"reject": ["media_type_mismatch"]},
            extra_parts={"word/media/x.png": b"MZ\x90\x00not an image"},
            extra_rels=[("rId47", "image", "media/x.png", False)],
            defaults_extra='<Default Extension="png" ContentType="image/png"/>',
        )
    )
    return out


def replace_part(zip_bytes: bytes, name: str, new: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(zip_bytes))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in src.infolist():
            zi = zipfile.ZipInfo(i.filename, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, new if i.filename == name else src.read(i.filename))
    return buf.getvalue()


def lying_zip(member: bytes) -> bytes:
    base = build_docx(
        BASE,
        extra_parts={"word/media/bomb.png": member},
        defaults_extra='<Default Extension="png" ContentType="image/png"/>',
    )
    z = zipfile.ZipFile(io.BytesIO(base))
    info = z.getinfo("word/media/bomb.png")
    b = bytearray(base)
    lh = info.header_offset
    struct.pack_into("<I", b, lh + 22, 1000)  # local header uncompressed size
    cd = base.find(b"PK\x01\x02" + b"", 0)
    while cd != -1:
        nlen = struct.unpack_from("<H", base, cd + 28)[0]
        name = base[cd + 46 : cd + 46 + nlen].decode()
        if name == "word/media/bomb.png":
            struct.pack_into("<I", b, cd + 24, 1000)  # central directory uncompressed size
        cd = base.find(b"PK\x01\x02", cd + 4)
    return bytes(b)


# ----------------------------------------------------------------------------- PDF
def pdf_case(fid, note, expected, builder):
    return fid, builder(), {"kind": "pdf", "expected": expected, "note": note}


def base_pdf(extra_catalog=b"", content_extra=b"", page_extra=b"", text=True, xo=None, info=False):
    p = Pdf()
    f = p.fonts()
    c = (text_obj("F1", 12, 30, 250, ["Texto base sin valores."]) if text else b"") + content_extra
    p.add_page(c, f, xobjects=xo)
    if page_extra:
        p.objs[p.pages[0]] = p.objs[p.pages[0]].replace(b" /Contents", page_extra + b" /Contents")
    p.extra_catalog = extra_catalog
    return p


def px() -> list:
    out = []
    b = base_pdf().build()
    q = pikepdf.open(io.BytesIO(b))
    buf = io.BytesIO()
    q.save(buf, encryption=pikepdf.Encryption(user="", owner="o", R=4))
    out.append(
        (
            "Q01_encrypted",
            buf.getvalue(),
            {
                "kind": "pdf",
                "expected": {"reject": ["encrypted"]},
                "note": "RC4/AES with empty user password: openable by the library, still rejected",
            },
        )
    )
    sig = base_pdf(extra_catalog=b"/AcroForm << /SigFlags 3 /Fields [] >> ").build()
    out.append(
        (
            "Q02_signed",
            sig,
            {"kind": "pdf", "expected": {"reject": ["signed"]}, "note": "AcroForm with SigFlags (signature present)"},
        )
    )
    p = base_pdf()
    p.info = b"<< /Title (v1) >>"
    inc = p.build()
    # incremental update appended: new version of the Info object with /Prev
    prev = int(inc.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    off = len(inc)
    n_info = len(p.objs) - 1
    new = inc + b"%d 0 obj\n<< /Title (v2) >>\nendobj\n" % n_info
    xo = len(new)
    new += b"xref\n%d 1\n%010d 00000 n \ntrailer\n<< /Size %d /Root 0 0 R /Prev %d >>\nstartxref\n%d\n%%%%EOF\n" % (
        n_info,
        off,
        len(p.objs),
        prev,
        xo,
    )
    root_n = [i for i in range(1, len(p.objs)) if p.objs[i] and b"/Type /Catalog" in p.objs[i]][0]
    new = new.replace(b"/Root 0 0 R /Prev", b"/Root %d 0 R /Info %d 0 R /Prev" % (root_n, n_info))
    out.append(
        (
            "Q03_incremental",
            new,
            {
                "kind": "pdf",
                "expected": {"reject": ["incremental_update"]},
                "note": "valid incremental update (older revision still in the file)",
            },
        )
    )
    broken = b.replace(b"startxref\n", b"startxref\n9")
    out.append(
        (
            "Q04_repaired_xref",
            broken,
            {
                "kind": "pdf",
                "expected": {"reject": ["repaired_by_parser", "corrupt_or_unreadable"]},
                "note": "wrong startxref: the parser reconstructs the xref table",
            },
        )
    )
    out.append(
        pdf_case(
            "Q05_javascript",
            "OpenAction JavaScript",
            {"reject": ["actions"]},
            lambda: base_pdf(extra_catalog=b"/OpenAction << /S /JavaScript /JS (app.alert\\(1\\)) >> ").build(),
        )
    )
    out.append(
        pdf_case(
            "Q06_attachment",
            "EmbeddedFiles name tree",
            {"reject": ["attachments"]},
            lambda: base_pdf(
                extra_catalog=b"/Names << /EmbeddedFiles << /Names [(a.txt) << /Type /Filespec /F (a.txt) >>] >> >> "
            ).build(),
        )
    )
    out.append(
        pdf_case(
            "Q07_form",
            "AcroForm with a text widget",
            {"reject": ["forms"]},
            lambda: base_pdf(extra_catalog=b"/AcroForm << /Fields [] >> ").build(),
        )
    )
    out.append(
        pdf_case(
            "Q08_layers",
            "optional content (OCProperties)",
            {"reject": ["optional_content"]},
            lambda: base_pdf(extra_catalog=b"/OCProperties << /OCGs [] /D << >> >> ").build(),
        )
    )

    def tagged():
        p = base_pdf()
        se = p.add(b"<< /Type /StructElem /S /P /ActualText (Ana Ejemplo) >>")
        root = p.add(b"<< /Type /StructTreeRoot /K %d 0 R >>" % se)
        p.extra_catalog = b"/StructTreeRoot %d 0 R " % root
        return p.build()

    out.append(
        pdf_case(
            "Q09_tagged_actualtext", "structure element with /ActualText", {"reject": ["tagged_structure_text"]}, tagged
        )
    )
    out.append(
        pdf_case(
            "Q10_invisible_text",
            "text render mode 3",
            {"reject": ["invisible_or_clipped_text"]},
            lambda: base_pdf(content_extra=b"BT /F1 12 Tf 3 Tr 30 100 Td (oculto) Tj ET\n").build(),
        )
    )

    def image_pdf(with_text):
        p = Pdf()
        f = p.fonts()
        img = p.stream(
            bytes(range(16)),
            b"/Type /XObject /Subtype /Image /Width 4 /Height 4 /ColorSpace /DeviceGray /BitsPerComponent 8 ",
            compress=False,
        )
        c = b"q 100 0 0 100 30 100 cm /Im1 Do Q\n" + (
            text_obj("F1", 12, 30, 250, ["Texto junto a la imagen."]) if with_text else b""
        )
        p.add_page(c, f, xobjects={"Im1": img})
        return p.build()

    out.append(
        pdf_case(
            "Q11_scanned", "page that is only an image", {"reject": ["scanned_document"]}, lambda: image_pdf(False)
        )
    )
    out.append(
        pdf_case(
            "Q12_mixed",
            "text plus image: processable with the image declared N",
            {"partial": ["image"]},
            lambda: image_pdf(True),
        )
    )
    out.append(
        pdf_case(
            "Q13_freetext_annot",
            "FreeText annotation with appearance",
            {"reject": ["annotation_unsupported"]},
            lambda: base_pdf(
                page_extra=b" /Annots [<< /Type /Annot /Subtype /FreeText /Rect [10 10 100 40] /Contents (nota) >>]"
            ).build(),
        )
    )

    def nofont():
        p = Pdf()
        f1 = p.add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /Type /Encoding /Differences [65 /g123 /g124] >> /FirstChar 65 /LastChar 66 /Widths [500 500] >>"
        )
        p.add_page(b"BT /F1 12 Tf 30 250 Td (AB) Tj ET\n", {"F1": f1})
        return p.build()

    out.append(
        pdf_case(
            "Q14_font_without_mapping",
            "custom glyph names and no ToUnicode",
            {"reject": ["font_mapping_insufficient"]},
            nofont,
        )
    )

    def type0():
        p = Pdf()
        cid = p.add(
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /X /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>"
        )
        f1 = p.add(
            b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H /DescendantFonts [%d 0 R] >>" % cid
        )
        p.add_page(b"BT /F1 12 Tf 30 250 Td <00410042> Tj ET\n", {"F1": f1})
        return p.build()

    out.append(
        pdf_case(
            "Q15_type0_font", "Type0/CID font (outside the experiment subset)", {"reject": ["font_unsupported"]}, type0
        )
    )

    def flate_bomb():
        p = Pdf(compress=False)
        f = p.fonts()
        data = zlib.compress(b"\x00" * (60 * 1024 * 1024), 9)
        cs = p.add(b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
        page = p.add(
            b"<< /Type /Page /Parent PAGES 0 R /MediaBox [0 0 400 300] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (f["F1"], cs)
        )
        p.pages.append(page)
        return p.build()

    out.append(
        pdf_case(
            "Q16_flate_bomb",
            "content stream inflating to 60 MiB from ~60 KiB",
            {"reject": ["limit_exceeded"]},
            flate_bomb,
        )
    )
    out.append(
        pdf_case(
            "Q17_vector_paths_partial",
            "text plus painted paths (text-as-paths cannot be verified): N",
            {"partial": ["vector_graphics"]},
            lambda: base_pdf(content_extra=b"30 100 40 8 re f 80 100 6 20 re f\n").build(),
        )
    )
    out.append(
        pdf_case(
            "Q18_unknown_catalog",
            "unknown catalog entry",
            {"reject": ["unknown_catalog_entry"]},
            lambda: base_pdf(extra_catalog=b"/Foo << /Bar (x) >> ").build(),
        )
    )
    out.append(
        pdf_case(
            "Q19_text_state",
            "horizontal scaling Tz",
            {"reject": ["text_state_unsupported"]},
            lambda: base_pdf(content_extra=b"BT /F1 12 Tf 150 Tz 30 100 Td (x) Tj ET\n").build(),
        )
    )
    out.append(
        (
            "Q20_truncated",
            b[: len(b) // 2],
            {
                "kind": "pdf",
                "expected": {"reject": ["corrupt_or_unreadable", "repaired_by_parser"]},
                "note": "truncated file",
            },
        )
    )
    return out


def write_all(outdir: Path) -> list[dict]:
    outdir.mkdir(parents=True, exist_ok=True)
    mans = []
    for fid, data, meta in dx() + px():
        ext = meta["kind"]
        (outdir / f"{fid}.{ext}").write_bytes(data)
        man = {"fixture_id": fid, "file": f"{fid}.{ext}", "sha256": sha256_bytes(data), **meta}
        (outdir / f"{fid}.manifest.json").write_text(json.dumps(man, indent=2) + "\n")
        mans.append(man)
    return mans


if __name__ == "__main__":
    for m in write_all(Path(sys.argv[1])):
        print(m["fixture_id"], m["expected"])
