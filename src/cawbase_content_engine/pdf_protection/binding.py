"""Pure A08 helpers: binding MAC, occurrences of the emitted markers and the restoration check (T5). No I/O."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from typing import Any

from ..docx_protection.binding import canonical
from ..protection.markers import scan, token_of

_BINDING_LABEL = b"cce.pdf.binding.v1"


def binding_payload(row: Mapping[str, Any]) -> bytes:
    return canonical(
        {
            "scope_id": row["scope_id"],
            "derivative_id": row["derivative_id"],
            "context_id": row["context_id"],
            "context_kind": row["context_kind"],
            "sha256": row["sha256"],
            "size_bytes": int(row["size_bytes"]),
            "extractor": [row["extractor_id"], row["extractor_version"], row["extractor_config_sha256"]],
            "occurrences": list(row["occurrences"] or []),
            "layout": list(row["layout"] or []),
        }
    )


def binding_mac(index_key: bytes, row: Mapping[str, Any]) -> bytes:
    """HMAC-SHA256 with the context's equality-index key (as A07): it authenticates scope, context, identity,
    positions and the minimal geometry/font data the restorer uses (D-A08-05)."""
    return hmac.new(index_key, _BINDING_LABEL + b"\x00" + binding_payload(row), hashlib.sha256).digest()


def mac_ok(index_key: bytes, row: Mapping[str, Any], mac: bytes | None) -> bool:
    return mac is not None and hmac.compare_digest(binding_mac(index_key, row), bytes(mac))


def occurrence_at(unit: Mapping[str, Any], start: int, end: int) -> dict[str, Any] | None:
    """[start, end) of a derivative unit drawn by ONE show operator of ONE stream (the marker is whole)."""
    pieces = []
    for seg in unit["segments"]:
        if seg["unit_start"] < end and seg["unit_end"] > start:
            if seg["kind"] != "original" or seg["unit_start"] < start or seg["unit_end"] > end:
                return None
            pieces.extend(seg["pieces"])
    if not pieces:
        return None
    keys = {(p["stream"], p["operator_index"], p["operand_index"]) for p in pieces}
    if len(keys) != 1:
        return None
    stream, op, elem = next(iter(keys))
    return {
        "unit_id": unit["unit_id"],
        "start": start,
        "end": end,
        "stream": stream,
        "operator_index": op,
        "operand_index": elem,
        "code_start": min(p["code_start"] for p in pieces),
        "code_end": max(p["code_end"] for p in pieces),
    }


def _key(o: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        o["unit_id"],
        o["start"],
        o["end"],
        o["stream"],
        o["operator_index"],
        o["operand_index"],
        o["code_start"],
        o["code_end"],
    )


def occurrences(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    """Every strict marker of the derivative, with its exact piece; ``None`` on any alteration or split marker."""
    out: list[dict[str, Any]] = []
    for u in units:
        sc = scan(u["text"])
        if sc.altered:
            return None
        for s, e, marker in sc.strict:
            occ = occurrence_at(u, s, e)
            if occ is None:
                return None
            occ["token"] = token_of(marker)
            out.append(occ)
    return out


def check_positions(units: Sequence[Mapping[str, Any]], bound: Sequence[Mapping[str, Any]]) -> bool:
    """T5 (pure): strict markers exactly at the recorded occurrences, none elsewhere, no R1/R2 anywhere."""
    found = occurrences(units)
    if found is None:
        return False
    return sorted((*_key(o), o["token"]) for o in found) == sorted((*_key(o), o["token"]) for o in bound)
