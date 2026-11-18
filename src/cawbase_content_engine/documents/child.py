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
    mode = params.get("mode")
    if mode is not None and str(mode).startswith("pdf_"):
        return _a08(str(mode), data, params, lim)
    if mode is not None:
        return _a07(mode, data, params, lim)
    if len(data) > lim.max_file_bytes:
        return {"rejected": "structure_limit"}
    kind = detect(data)
    try:
        if kind == "pdf":
            if params.get("pdf_extractor") == "v2":  # A08 extractor (docs/pdf-protection.md section 4)
                from . import pdf_extract

                return pdf_extract.extract(data, lim)
            from . import pdf  # A05 extractor, frozen for jobs accepted before A08

            return pdf.extract(data, lim)
        if kind == "docx":
            from . import docx

            return docx.extract(data, lim)
        if kind == "ole":
            return {"rejected": "encrypted"}
        return {"rejected": "unsupported_format"}
    except Reject as r:
        return {"rejected": r.reason}


def _a07(mode: str, data: bytes, params: dict[str, Any], lim: Limits) -> dict[str, Any]:
    """A07 modes (docs/docx-protection.md): the same isolated child, the same limits, untrusted input."""
    if len(data) > 2 * lim.max_file_bytes + 16:
        return {"rejected": "structure_limit"}
    a07 = params.get("a07") or {}
    if mode in ("docx_plan", "docx_protect", "docx_restore"):
        if detect(data) != "docx":
            return {"rejected": "unsupported_format"}
        from . import docx_transform as tx

        if mode == "docx_restore":
            return tx.restore(data, lim, list(a07.get("occurrences", [])))
        return tx.protect(data, lim, list(a07.get("selections", [])), check_only=mode == "docx_plan")
    if mode == "docx_verify":
        from . import docx_verify

        n = int.from_bytes(data[:8], "big")
        return docx_verify.verify(data[8 : 8 + n], data[8 + n :], a07)
    return {"rejected": "unsupported_format"}


def _a08(mode: str, data: bytes, params: dict[str, Any], lim: Limits) -> dict[str, Any]:
    """A08 modes (docs/pdf-protection.md): the same isolated child, the same limits, untrusted input."""
    if len(data) > 2 * lim.max_file_bytes + 16:
        return {"rejected": "structure_limit"}
    a08 = params.get("a08") or {}
    if mode in ("pdf_plan", "pdf_protect", "pdf_restore"):
        if detect(data) != "pdf":
            return {"rejected": "unsupported_format"}
        from . import pdf_transform as tx

        try:
            if mode == "pdf_restore":
                return tx.restore(data, lim, list(a08.get("layout", [])), dict(a08.get("values", {})))
            return tx.protect(data, lim, list(a08.get("selections", [])), check_only=mode == "pdf_plan")
        except Reject as r:
            return {"rejected": r.reason}
        except tx.Failure as f:
            return {"failure": f.code}
    if mode == "pdf_verify":
        from . import pdf_verify

        n = int.from_bytes(data[:8], "big")
        return pdf_verify.verify(str(a08.get("mode")), data[8 : 8 + n], data[8 + n :], a08)
    return {"rejected": "unsupported_format"}


def main() -> None:
    raw = sys.stdin.buffer
    header = raw.readline()
    params = json.loads(header)
    data = raw.read()
    if params.get("canary_memory"):
        from .canary import probe_memory

        try:
            probe_memory(int(params["canary_memory"]))
        except MemoryError:
            sys.exit(3)
        out: dict[str, Any] = {"allocated": True}
    elif params.get("canary"):
        from .canary import run_canary

        out = run_canary(params)
    else:
        try:
            out = process(data, params)
        except MemoryError:
            sys.exit(3)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
