"""Relative links and anchors of the project's own Markdown (docs/, README, CLAUDE.md). Exit code 1 if any is broken.

    python scripts/check_doc_links.py            # prints "bad 0" when everything resolves

Heading anchors follow the GitHub rule (lowercase, punctuation removed, spaces to hyphens). The local reference copy
docs/STAGE_3_PLAYBOOK.md and the A04 experiments are not checked. It does not validate the CONTENT of the contract.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def own_markdown() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CLAUDE.md"] + sorted((ROOT / "docs").rglob("*.md"))
    return [p for p in files if p.exists() and "STAGE_3" not in p.name]


def slug(heading: str) -> str:
    h = heading.strip().lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", h, flags=re.UNICODE).replace(" ", "-")


def check() -> list[str]:
    files = own_markdown()
    anchors: dict[Path, set[str]] = {}
    for f in files:
        text = f.read_text(encoding="utf-8")
        found = {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.*)$", text, re.M)}
        found |= set(re.findall(r'<a id="([^"]+)"', text))
        anchors[f.resolve()] = found
    problems: list[str] = []
    for f in files:
        for m in re.finditer(r"\]\(([^)\s]+)\)", f.read_text(encoding="utf-8")):
            link = m.group(1)
            if link.startswith(("http", "mailto")):
                continue
            path, _, anchor = link.partition("#")
            target = (f.parent / path).resolve() if path else f.resolve()
            if path and not target.exists():
                problems.append(f"MISSING {f.relative_to(ROOT)} -> {link}")
            elif anchor and target.suffix == ".md" and target in anchors and anchor not in anchors[target]:
                problems.append(f"ANCHOR {f.relative_to(ROOT)} -> {link}")
    return problems


if __name__ == "__main__":
    found = check()
    print("\n".join(found))
    print("bad", len(found))
    sys.exit(1 if found else 0)
