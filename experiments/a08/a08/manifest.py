"""Loads the frozen corpus manifest (data) and resolves template blocks into plain structures."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "corpus" / "manifest.json"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def runs_of(block: dict, m: dict) -> list[list[str]]:
    if "filler" in block:
        return [[m["filler"][block["filler"]], ""]]
    return block["runs"]


def selections(m: dict, template: str) -> list[dict]:
    out = []
    for s in m["occurrences"][template]["selected"]:
        v = m["values"][s["value_id"]]
        out.append({**s, "value": v["value"], "type_id": v["type_id"], "action": v["action"]})
    return out


def permitted(m: dict, template: str) -> list[dict]:
    out = []
    for s in m["occurrences"][template]["not_selected"]:
        v = m["values"][s["value_id"]]
        out.append({**s, "value": v["value"], "type_id": v["type_id"]})
    return out
