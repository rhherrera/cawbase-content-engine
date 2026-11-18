"""Stage 2B contact sheets: every zone of every processed case (original | protected | restored), PDFium 300 dpi, with
zone kind, attempt and factor. Visual evidence for human review; never changes a result.

    .venv/bin/python -m a08.sheets2b <env>
"""

from __future__ import annotations

import json
import sys

import numpy as np
from PIL import Image, ImageDraw

from .manifest import MANIFEST_2B, ROOT, load
from .run2b import CORPUS, case_pdf
from .sheets import DPI, render
from .verify import zones_of


def main(env: str) -> None:
    out = ROOT / "outputs" / env
    m = load(MANIFEST_2B)
    res = json.loads((out / "results.json").read_text())["results"]
    (out / "sheets").mkdir(exist_ok=True)
    for r in res:
        cid = r.get("case_id")
        prot = out / "cases" / f"{cid}.protected.pdf"
        if not prot.exists():
            continue
        rest = out / "cases" / f"{cid}.restored.pdf"
        binding = json.loads((out / "cases" / f"{cid}.binding.json").read_text())
        orc = json.loads((CORPUS / cid / "oracle.json").read_text())
        orig = case_pdf(next(c for c in m["cases"] if c["case_id"] == cid))
        rows = []
        for z in zones_of(binding["lines"], orc):
            ln = next(ln for ln in binding["lines"] for s in ln["sels"] if s["sid"] == z["sids"][0])
            crops = []
            for p in (orig, prot, rest):
                if not p.exists():
                    crops.append(np.full((10, 10), 255, np.uint8))
                    continue
                img, h = render(p, z["page"])
                s = DPI / 72
                x0, y0, x1, y1 = z["rect"]
                pad = 6 * s
                crops.append(img[max(int((h - y1) * s - pad), 0):int((h - y0) * s + pad), max(int(x0 * s - pad), 0):int(x1 * s + pad)])
            w = max(c.shape[1] for c in crops)
            label = f"{cid} {z['sids']} {z['kind']} f={ln['f']:.3f} " + ", ".join(f"{s['sid']}: intento {s['attempt']} {s['font']}" for s in ln["sels"] if s["sid"] in z["sids"])
            lab = Image.new("L", (w, 40), 255)
            ImageDraw.Draw(lab).text((4, 12), label + "   [original | protegido | restaurado]", fill=0)
            block = [np.array(lab)]
            for c in crops:
                pad_c = np.full((c.shape[0], w), 255, np.uint8)
                pad_c[:, :c.shape[1]] = c
                block += [pad_c, np.full((6, w), 200, np.uint8)]
            rows.append(np.vstack(block))
        if rows:
            W = max(r_.shape[1] for r_ in rows)
            sheet = np.vstack([np.pad(r_, ((0, 12), (0, W - r_.shape[1])), constant_values=255) for r_ in rows])
            Image.fromarray(sheet).save(out / "sheets" / f"{cid}.png", optimize=True)
            print(cid, len(rows), "zones")


if __name__ == "__main__":
    main(sys.argv[1])
