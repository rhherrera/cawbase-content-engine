"""Acceptance evidence of A08 renders (NOT a runtime control; docs/pdf-protection.md section 10, approved criterion).

For L1, C1, M1, L5 and M5 the original, the protected and the restored PDF are rendered at 150 dpi in grey with
PDFium (``pypdfium2``) and Poppler (``pdftoppm``). Outside the authorized zones (dilated 3 px):

* Poppler: 0 differing pixels.
* PDFium: every differing pixel must be EXPLAINED by a glyph whose text and extracted box did not change (+-0.01 pt)
  and whose raster, translated rigidly by ONE shift s in [-1, 1]^2 px for the whole glyph (integer or a sub-pixel
  fraction in steps of 1/8, bilinear), reproduces the reference at that pixel (|delta| <= 32). The region of a
  glyph is its PDFium tight box + 1 px; the shift is the one that best reproduces that glyph's differing pixels.
  A pixel merely inside an enlarged character box is NOT explained. Anything else fails.

Requires the dev dependencies ``pypdfium2``, ``numpy``, ``pillow`` and Poppler's ``pdftoppm`` (skipped, and reported,
if Poppler is absent). With ``CCE_A08_EVIDENCE_DIR`` the PDFs and a JSON summary per case are written there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pdfium = pytest.importorskip("pypdfium2")
Image = pytest.importorskip("PIL.Image")

from . import a08_synth as S  # noqa: E402
from .a08_helpers import corpus  # noqa: E402
from .test_a08_pure import corpus_picks  # noqa: E402

DPI, DILATE, TOL = 150, 3, 32
CASES = ["L1", "C1", "M1", "L5", "M5"]
POPPLER = shutil.which("pdftoppm")


def render_pdfium(data: bytes) -> list[Any]:
    doc = pdfium.PdfDocument(data)
    return [
        np.array(doc[i].render(scale=DPI / 72, grayscale=True).to_pil().convert("L")).astype(int)
        for i in range(len(doc))
    ]


def render_poppler(data: bytes) -> list[Any]:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "in.pdf"
        f.write_bytes(data)
        r = subprocess.run(
            [POPPLER, "-r", str(DPI), "-gray", "-png", str(f), str(Path(td) / "p")], capture_output=True, check=False
        )
        assert r.returncode == 0 and not r.stderr.strip(), r.stderr
        files = sorted(Path(td).glob("p-*.png"), key=lambda q: int(q.stem.split("-")[-1]))
        return [np.array(Image.open(x).convert("L")).astype(int) for x in files]


def chars(data: bytes) -> list[list[dict[str, Any]]]:
    import pypdfium2.raw as c

    doc = pdfium.PdfDocument(data)
    out = []
    for i in range(len(doc)):
        tp = doc[i].get_textpage()
        page = []
        for k in range(tp.count_chars()):
            u = chr(c.FPDFText_GetUnicode(tp.raw, k))
            if u.isspace() or c.FPDFText_IsGenerated(tp.raw, k):
                continue
            page.append({"u": u, "box": list(tp.get_charbox(k))})
        out.append(page)
    return out


def zones(layout: list[dict[str, Any]], excess: float = 0.0) -> list[tuple[int, list[float]]]:
    out = []
    for ln in layout:
        if ln["kind"] == "Z1":
            for s in ln["sels"]:
                out.append((ln["page"], [s["left"], s["band"][0], s["zone_r"], s["band"][1]]))
        else:
            out.append((ln["page"], [ln["left"], ln["band"][0], ln["sels"][0]["zone_r"] + excess, ln["band"][1]]))
    return out


def mask(zs: list[tuple[int, list[float]]], page: int, shape: tuple[int, int], h: float) -> Any:
    m = np.zeros(shape, dtype=bool)
    s = DPI / 72.0
    for p, (x0, y0, x1, y1) in zs:
        if p != page:
            continue
        c0, c1 = int(x0 * s) - DILATE, int(np.ceil(x1 * s)) + DILATE
        r0, r1 = int((h - y1) * s) - DILATE, int(np.ceil((h - y0) * s)) + DILATE
        m[max(r0, 0) : min(r1, shape[0]), max(c0, 0) : min(c1, shape[1])] = True
    return m


def _shifted(a: Any, r0: int, r1: int, c0: int, c1: int, dx: float, dy: float) -> Any:
    """The reference raster of the region translated rigidly by (dx, dy) pixels (bilinear for fractions)."""
    fx, fy = int(np.floor(dx)), int(np.floor(dy))
    tx, ty = dx - fx, dy - fy

    def win(ox: int, oy: int) -> Any:
        return a[r0 - oy : r1 - oy, c0 - ox : c1 - ox]

    return (
        (1 - tx) * (1 - ty) * win(fx, fy)
        + tx * (1 - ty) * win(fx + 1, fy)
        + (1 - tx) * ty * win(fx, fy + 1)
        + tx * ty * win(fx + 1, fy + 1)
    )


SHIFTS = [(i / 8, j / 8) for i in range(-8, 9) for j in range(-8, 9)]


def explained(a: Any, b: Any, diff: Any, ref: list[dict[str, Any]], out: list[dict[str, Any]], h: float) -> Any:
    """Pixels reproduced by the WHOLE raster of an UNCHANGED glyph (same text, same extracted box +-0.01 pt) translated
    rigidly by at most 1 px (integer or sub-pixel, one translation per glyph: the best one for that glyph)."""
    sc = DPI / 72.0
    H, W = a.shape
    ok = np.zeros(a.shape, dtype=bool)
    same = [
        ch
        for ch in ref
        if any(
            o["u"] == ch["u"] and max(abs(p - q) for p, q in zip(o["box"], ch["box"], strict=True)) <= 0.01 for o in out
        )
    ]
    for ch in same:
        x0, y0, x1, y1 = ch["box"]
        r0, r1 = max(int((h - y1) * sc) - 1, 2), min(int(np.ceil((h - y0) * sc)) + 1, H - 2)
        c0, c1 = max(int(x0 * sc) - 1, 2), min(int(np.ceil(x1 * sc)) + 1, W - 2)
        dd = diff[r0:r1, c0:c1]
        if r1 <= r0 or c1 <= c0 or not dd.any():
            continue
        reg = b[r0:r1, c0:c1]
        # ONE rigid translation for this glyph, the one that best reproduces its differing pixels
        best = min(SHIFTS, key=lambda sh: float(np.abs(reg - _shifted(a, r0, r1, c0, c1, *sh))[dd].max()))
        ok[r0:r1, c0:c1] |= dd & (np.abs(reg - _shifted(a, r0, r1, c0, c1, *best)) <= TOL)
    return ok


def compare(ref: bytes, out: bytes, zs: list[tuple[int, list[float]]]) -> dict[str, Any]:
    import io

    import pikepdf

    heights = [float(p.mediabox[3]) for p in pikepdf.open(io.BytesIO(ref)).pages]
    res: dict[str, Any] = {"poppler_px": 0, "pdfium_px": 0, "pdfium_unexplained_px": 0}
    for name, fn in (("pdfium", render_pdfium), ("poppler", render_poppler)):
        ra, rb = fn(ref), fn(out)
        assert len(ra) == len(rb)
        rc, oc = (chars(ref), chars(out)) if name == "pdfium" else (None, None)
        for i, (a, b) in enumerate(zip(ra, rb, strict=True)):
            assert a.shape == b.shape
            d = (a != b) & ~mask(zs, i, a.shape, heights[i])
            res[f"{name}_px"] += int(d.sum())
            if name == "pdfium" and d.any():
                ex = explained(a, b, d, rc[i], oc[i], heights[i])
                res["pdfium_unexplained_px"] += int((d & ~ex).sum())
    return res


@pytest.mark.skipif(
    POPPLER is None, reason="Poppler (pdftoppm) not installed: render evidence pending in this environment"
)
@pytest.mark.parametrize("case", CASES)
def test_render_acceptance_criterion(case):
    data = corpus(case)
    r = S.cycle(data, corpus_picks(case))
    assert r["stage"] == "cycle", (r["stage"], r["reason"])
    layout = r["protect"]["layout"]
    excess = 0.0
    for ln in layout:
        for s in ln["sels"]:
            if ln["kind"] == "Z2" and s["action"] == "redact":
                from cawbase_content_engine.documents.pdf_transform import redaction_scale

                excess = max(excess, redaction_scale(s["span"], s["nat"])[1])
    protected = compare(data, r["derivative"], zones(layout))
    restored = compare(r["derivative"], r["restored"], zones(layout, excess))
    summary = {
        "case": case,
        "protected": protected,
        "restored": restored,
        "zones": [(ln["kind"], round(ln["f"], 4)) for ln in layout],
    }
    out_dir = os.environ.get("CCE_A08_EVIDENCE_DIR")
    if out_dir:
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{case}.protected.pdf").write_bytes(r["derivative"])
        (d / f"{case}.restored.pdf").write_bytes(r["restored"])
        (d / f"{case}.json").write_text(json.dumps(summary, indent=1) + "\n")
    for stage in ("protected", "restored"):
        assert summary[stage]["poppler_px"] == 0, summary
        assert summary[stage]["pdfium_unexplained_px"] == 0, summary
