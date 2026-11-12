"""Independent PDF verification: three extraction engines other than the transformer's (PDFium, pypdf, Poppler), structure
openers, raw residue scan over inflated streams, geometry/page checks and bounded pixel comparison outside affected regions."""

from __future__ import annotations

import io
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import pypdf
import pypdfium2 as pdfium
from PIL import Image, ImageChops

from a04.common import bounded_inflate
from a04.verify import variants

WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return unicodedata.normalize("NFC", WS.sub("", s))


def expected_after(manifest: dict, selected: list[str], replacement: str) -> list[str]:
    """ORACLE: manifest unit text with the selected occurrence intervals replaced (by position; never from an extractor)."""
    out = []
    for u in manifest["expected_units"]:
        idx = u.get("page", u.get("unit_index"))
        text = u["text"]
        sel = sorted(
            (o for o in manifest["occurrences"] if o["occ"] in selected and o.get("unit_index", o.get("page")) == idx),
            key=lambda o: -o["start"],
        )
        for o in sel:
            text = text[: o["start"]] + replacement + text[o["end"] :]
        out.append(text)
    return out


def engines_text(data: bytes) -> dict[str, list[str]]:
    res: dict[str, list[str]] = {}
    pd = pdfium.PdfDocument(data)
    res["pdfium"] = [pd[i].get_textpage().get_text_range() for i in range(len(pd))]
    rd = pypdf.PdfReader(io.BytesIO(data), strict=True)
    res["pypdf"] = [p.extract_text() for p in rd.pages]
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.pdf"
        f.write_bytes(data)
        r = subprocess.run(["pdftotext", "-raw", str(f), "-"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or r.stderr.strip():
            res["poppler"] = ["__ERROR__" + r.stderr[:80]]
        else:
            res["poppler"] = r.stdout.split("\x0c")[:-1]
    return res


def open_check(data: bytes) -> dict:
    """Structure/openability by three independent parsers; warnings count as repair."""
    out: dict = {}
    try:
        pd = pdfium.PdfDocument(data)
        out["pdfium_pages"] = len(pd)
    except Exception as exc:  # noqa: BLE001
        out["pdfium_error"] = type(exc).__name__
    try:
        rd = pypdf.PdfReader(io.BytesIO(data), strict=True)
        out["pypdf_pages"] = len(rd.pages)
    except Exception as exc:  # noqa: BLE001
        out["pypdf_error"] = f"{type(exc).__name__}: {str(exc)[:60]}"
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.pdf"
        f.write_bytes(data)
        r = subprocess.run(["pdfinfo", str(f)], capture_output=True, text=True, timeout=60)
        out["poppler_rc"] = r.returncode
        out["poppler_stderr"] = r.stderr.strip()[:120]
    out["ok"] = (
        "pdfium_error" not in out
        and "pypdf_error" not in out
        and out["poppler_rc"] == 0
        and not out["poppler_stderr"]
        and out.get("pdfium_pages") == out.get("pypdf_pages")
    )
    return out


def raw_corpus(data: bytes) -> list[tuple[str, bytes]]:
    """Raw file + every stream inflated with the bounded reader (object streams included, since they are streams)."""
    corp = [("file", data)]
    for i, m in enumerate(re.finditer(rb"stream\r?\n", data)):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        chunk = data[start:end].rstrip(b"\r\n")
        try:
            corp.append((f"stream@{m.start()}", bounded_inflate(chunk)))
        except Exception:  # noqa: BLE001  not flate / not inflatable: scanned raw above
            pass
    return corp


def scan_pdf(data: bytes, values: list[str]) -> list[dict]:
    finds = []
    corpus = raw_corpus(data)
    for value in values:
        vs = variants(value)
        hexes = {f"hex-{k}": v.hex().encode() for k, v in vs.items() if k.startswith("utf16be") or k.startswith("utf8")}
        hexes.update(
            {
                f"hexU-{k}": v.hex().upper().encode()
                for k, v in vs.items()
                if k.startswith("utf16be") or k.startswith("utf8")
            }
        )
        for name, buf in corpus:
            for how, needle in {**vs, **hexes}.items():
                if needle and needle in buf:
                    finds.append({"value": value, "where": name, "how": how})
                    break
    return finds


def render(data: bytes, scale: float = 1.5) -> list[Image.Image]:
    pd = pdfium.PdfDocument(data)
    return [pd[i].render(scale=scale).to_pil().convert("L") for i in range(len(pd))]


def poppler_render(data: bytes, dpi: int = 90) -> list[Image.Image]:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "x.pdf"
        f.write_bytes(data)
        subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-gray", "-png", str(f), str(Path(td) / "p")], check=True, timeout=120
        )
        return [Image.open(p).convert("L").copy() for p in sorted(Path(td).glob("p-*.png"))]


def page_geometry(data: bytes) -> list[dict]:
    rd = pypdf.PdfReader(io.BytesIO(data), strict=True)
    return [
        {
            "mediabox": [float(x) for x in p.mediabox],
            "cropbox": [float(x) for x in p.cropbox],
            "rotate": int(p.get("/Rotate", 0) or 0),
        }
        for p in rd.pages
    ]


def to_pixels(
    l: float, b: float, r: float, t: float, w0: float, h0: float, rot: int, scale: float
) -> tuple[int, int, int, int]:
    """User-space box -> pixel box of the rendered page (rotation applied), padded by 3 px."""
    pts = [(l, b), (r, b), (l, t), (r, t)]
    if rot == 0:
        d = [(x, h0 - y) for x, y in pts]
    elif rot == 90:
        d = [(y, x) for x, y in pts]
    elif rot == 180:
        d = [(w0 - x, y) for x, y in pts]
    else:
        d = [(h0 - y, w0 - x) for x, y in pts]
    xs, ys = [p[0] for p in d], [p[1] for p in d]
    return (int(min(xs) * scale) - 3, int(min(ys) * scale) - 3, int(max(xs) * scale) + 3, int(max(ys) * scale) + 3)


def affected_boxes(
    orig: bytes, manifest: dict, selected: list[str], scale: float, extra_user_rects: list | None = None
) -> dict[int, list]:
    """Pixel boxes of the selected occurrences in the ORIGINAL render, from PDFium character boxes aligned to the oracle text
    (plus explicit user-space rectangles of authorized removals, e.g. comment annotations on page 0)."""
    pd = pdfium.PdfDocument(orig)
    out: dict[int, list] = {}
    for u in manifest["expected_units"]:
        pi = u.get("page", u.get("unit_index"))
        page = pd[pi]
        rot = page.get_rotation()
        x0, y0, x1, y1 = page.get_mediabox()
        w0, h0 = x1 - x0, y1 - y0
        tp = page.get_textpage()
        n = tp.count_chars()
        chars = [tp.get_text_range(i, 1) for i in range(n)]
        non_ws_idx = [i for i, c in enumerate(chars) if not c.isspace() and c != ""]
        oracle_nonws = [k for k, c in enumerate(u["text"]) if not c.isspace()]
        pos_of = {k: j for j, k in enumerate(oracle_nonws)}
        boxes = []
        for o in manifest["occurrences"]:
            if o["occ"] not in selected or o.get("unit_index", o.get("page")) != pi:
                continue
            js = [pos_of[k] for k in range(o["start"], o["end"]) if k in pos_of]
            if not js or max(js) >= len(non_ws_idx):
                boxes.append(None)
                continue
            L = B = 10**9
            R = T = -(10**9)
            for j in js:
                l, b, r, t = tp.get_charbox(non_ws_idx[j])
                L, R, B, T = min(L, l), max(R, r), min(B, b), max(T, t)
            boxes.append(to_pixels(L, B, R, T, w0, h0, rot, scale))
        if pi == 0:
            for l, b, r, t in extra_user_rects or []:
                bx = to_pixels(l, b, r, t, w0, h0, rot, scale)
                boxes.append(
                    (bx[0] - 12, bx[1] - 12, bx[2] + 12, bx[3] + 12)
                )  # annotation icons are drawn larger than their Rect
        out[pi] = boxes
    return out


def diff_outside(a: Image.Image, b: Image.Image, boxes: list, *, tol: int = 0) -> dict:
    """Pixels differing by more than ``tol`` outside the (padded) affected boxes; the whole-line band is the mask for each box."""
    if a.size != b.size:
        return {"size_mismatch": [a.size, b.size], "outside_diff_pixels": -1}
    d = ImageChops.difference(a, b).point(lambda v: 255 if v > tol else 0)
    W, H = a.size
    mask = Image.new("L", (W, H), 0)
    for bx in boxes:
        if bx is None:
            return {"outside_diff_pixels": -2, "note": "alignment failed"}
        l, t, r, bt = bx
        if (bt - t) > 1.5 * (r - l):  # vertical text line on screen: full-height band
            mask.paste(255, (max(l, 0), 0, min(r, W), H))
        else:  # horizontal line: full-width band
            mask.paste(255, (0, max(t, 0), W, min(bt, H)))
    outside = ImageChops.multiply(d, ImageChops.invert(mask))
    n_out = sum(1 for v in outside.getdata() if v)
    n_all = sum(1 for v in d.getdata() if v)
    return {
        "outside_diff_pixels": n_out,
        "total_diff_pixels": n_all,
        "mask_fraction": round(sum(1 for v in mask.getdata() if v) / (W * H), 4),
        "tolerance": tol,
    }


def overlay_attack(data: bytes, boxes_user: list[tuple[float, float, float, float]]) -> bytes:
    """CN-04: draw white rectangles over the text WITHOUT removing it (must fail verification)."""
    import pikepdf

    pdf = pikepdf.open(io.BytesIO(data))
    page = pdf.pages[0]
    rects = b"q 1 1 1 rg " + b" ".join(b"%f %f %f %f re f" % (x, y, w, h) for x, y, w, h in boxes_user) + b" Q\n"
    old = b"".join(
        s.read_bytes() for s in ([page.Contents] if isinstance(page.Contents, pikepdf.Stream) else list(page.Contents))
    )
    page.Contents = pdf.make_stream(old + b"\n" + rects)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()
