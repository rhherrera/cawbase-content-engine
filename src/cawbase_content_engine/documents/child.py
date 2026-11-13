"""Extraction child process: reads ``<json params>\\n<raw bytes>`` from stdin, writes one JSON document to stdout.

Runs with an empty environment, no credentials and bounded resources (see ``runner``). The parent validates the output.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .common import Limits, Reject


def detect(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        return "docx"
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return "ole"
    return "unknown"


def process(data: bytes, params: dict[str, Any]) -> dict[str, Any]:
    lim = Limits.from_params(params)
    if len(data) > lim.max_file_bytes:
        return {"rejected": "structure_limit"}
    kind = detect(data)
    try:
        if kind == "pdf":
            from . import pdf

            return pdf.extract(data, lim)
        if kind == "docx":
            from . import docx

            return docx.extract(data, lim)
        if kind == "ole":
            return {"rejected": "encrypted"}
        return {"rejected": "unsupported_format"}
    except Reject as r:
        return {"rejected": r.reason}


def main() -> None:
    raw = sys.stdin.buffer
    header = raw.readline()
    params = json.loads(header)
    data = raw.read()
    if params.get("canary"):
        from .canary import run_canary

        out: dict[str, Any] = run_canary()
    else:
        try:
            out = process(data, params)
        except MemoryError:
            sys.exit(3)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
