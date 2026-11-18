"""Generates the HTML sources (T1..T4) for the Chromium family and for M3 (WebKit). Explicit A4 pages.

    .venv/bin/python -m a08.gen_html out_dir fonts_dir
"""

from __future__ import annotations

import html
import os
import sys
from pathlib import Path

from .manifest import load, runs_of


def _runs(runs) -> str:
    out = []
    for text, style in runs:
        t = html.escape(text, quote=False)
        if style == "b":
            out.append(f"<b>{t}</b>")
        elif style.startswith("link:"):
            out.append(f'<a href="{html.escape(style[5:])}">{t}</a>')
        else:
            out.append(t)
    return "".join(out)


def _block(b, m, notes: list[str]) -> str:
    ty = b["type"]
    if ty == "header":
        return ""
    if ty == "heading":
        lvl = b["level"]
        return f'<h{lvl}{" class=span" if b.get("span") else ""}>{html.escape(b["text"])}</h{lvl}>'
    if ty == "paragraph":
        al = "j" if b.get("align", "justify") == "justify" else "l"
        ref = ""
        if "note" in b:
            notes.append(b["note"]["text"])
            ref = f"<sup>{len(notes)}</sup>"
        return f'<p class="{al}">{_runs(runs_of(b, m))}{ref}</p>'
    if ty == "table":
        rows = []
        for i, row in enumerate(b["rows"]):
            tag = "th" if i == 0 and b.get("header_row") else "td"
            rows.append("<tr>" + "".join(f"<{tag}>{_runs(c)}</{tag}>" for c in row) + "</tr>")
        return f'<table class="{"bd" if b.get("borders") else ""}">{"".join(rows)}</table>'
    if ty == "columns":
        cols = "".join("<div class=col>" + "".join(_block(x, m, notes) for x in col) + "</div>" for col in b["columns"])
        return f"<div class=cols>{cols}</div>"
    if ty == "shaded_box":
        return f'<div class=box style="background:{b["fill"]}">' + "".join(_block(x, m, notes) for x in b["content"]) + "</div>"
    if ty == "shaded_band":
        return f'<div class=band style="background:{b["fill"]}">' + "".join(_block(x, m, notes) for x in b["content"]) + "</div>"
    if ty in ("bullet_list", "numbered_list"):
        tag = "ul" if ty == "bullet_list" else "ol"
        return f"<{tag}>" + "".join(f"<li>{_runs(it)}</li>" for it in b["items"]) + f"</{tag}>"
    if ty == "horizontal_rule":
        return "<hr>"
    raise ValueError(ty)


def build(tid: str, m: dict, fonts_dir: str) -> str:
    t = m["templates"][tid]
    default_font = t.get("font") == "exporter_default"
    faces = ""
    if not default_font:
        for fam, base in (("Noto Serif", "NotoSerif"), ("Noto Sans", "NotoSans")):
            for w, st, suf in (("normal", "normal", "Regular"), ("bold", "normal", "Bold"), ("normal", "italic", "Italic")):
                faces += f'@font-face{{font-family:"{fam} A08";src:url("{fonts_dir}/{base}-{suf}.ttf");font-weight:{w};font-style:{st}}}\n'
    body_font = "" if default_font else 'font-family:"Noto Serif A08";'
    head_font = "" if default_font else 'font-family:"Noto Sans A08";'
    css = f"""{faces}
@page {{ size: A4; margin: 0 }}
html, body {{ margin: 0; padding: 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
body {{ {body_font} font-size: 11pt; line-height: 1.15; color: #000; }}
.page {{ width: 210mm; height: 297mm; padding: 25mm; box-sizing: border-box; position: relative; overflow: hidden; break-after: page; }}
.page:last-child {{ break-after: auto; }}
.hdr {{ position: absolute; top: 12mm; right: 25mm; font-size: 9pt; {head_font} }}
.ftr {{ position: absolute; bottom: 12mm; left: 0; right: 0; text-align: center; font-size: 9pt; {head_font} }}
.note {{ position: absolute; bottom: 20mm; left: 25mm; right: 25mm; font-size: 9pt; border-top: 0.5pt solid #000; padding-top: 2pt; }}
h1 {{ font-size: 16pt; margin: 0 0 6pt; {head_font} }}
h2 {{ font-size: 13pt; margin: 10pt 0 6pt; {head_font} }}
p {{ margin: 0 0 6pt; }} p.j {{ text-align: justify; }} p.l {{ text-align: left; }}
table {{ border-collapse: collapse; width: 100%; margin: 0 0 8pt; {head_font} }}
table.bd td, table.bd th {{ border: 0.5pt solid #000; }}
td, th {{ padding: 2pt 4pt; text-align: left; vertical-align: top; }}
.cols {{ display: flex; gap: 10mm; }} .col {{ flex: 1; }}
.box {{ border: 0.5pt solid #000; padding: 4pt; margin-top: 8pt; }}
.band {{ padding: 4pt; margin-bottom: 8pt; }} .band h1 {{ margin: 0; }}
hr {{ border: 0; border-top: 0.75pt solid #000; margin: 8pt 0; }}
a {{ color: #1F4E9A; }}
"""
    pages = []
    total = len(t["pages"])
    for i, page in enumerate(t["pages"], 1):
        notes: list[str] = []
        inner = "".join(_block(b, m, notes) for b in page)
        hdr = next((b["text"] for b in page if b.get("type") == "header"), None)
        extra = f'<div class=hdr>{html.escape(hdr)}</div>' if hdr else ""
        if notes:
            extra += "<div class=note>" + "".join(f"<sup>{k}</sup> {html.escape(n)}<br>" for k, n in enumerate(notes, 1)) + "</div>"
        if t.get("footer"):
            extra += f"<div class=ftr>{html.escape(t['footer'].format(n=i, total=total))}</div>"
        pages.append(f"<div class=page>{extra}{inner}</div>")
    title = html.escape(f"Corpus A08 {tid} (sintético)")
    return (
        f'<!doctype html><html lang="es"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta name="author" content="Autoría de ejemplo"><style>{css}</style></head><body>{"".join(pages)}</body></html>'
    )


def main(out: str, fonts_dir: str) -> None:
    m = load()
    o = Path(out)
    o.mkdir(parents=True, exist_ok=True)
    for tid in ("T1", "T2", "T3", "T4"):
        (o / f"{tid}.html").write_text(build(tid, m, os.path.relpath(Path(fonts_dir).resolve(), o.resolve())), encoding="utf-8")
        print(o / f"{tid}.html")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
