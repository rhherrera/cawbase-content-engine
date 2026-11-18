"""Pure A07 helpers: mock provenance (S10b), oracle, occurrences, binding MAC. No I/O, no permissions."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

MOCK_ADAPTER_ID = "cce-mock-fixtures"
MOCK_ADAPTER_VERSION = "1"
_FXS = re.compile(r"fxs-[0-9a-f]{16}")
_BINDING_LABEL = b"cce.docx.binding.v1"


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def projection(descriptor: Mapping[str, Any]) -> dict[str, Any]:
    """``inference/build.py``: the A03 ``Provenance`` an ``AdapterDescriptor`` produces."""
    model = None
    if descriptor.get("model_id") is not None or descriptor.get("model_version") is not None:
        model = {"id": descriptor.get("model_id"), "version": descriptor.get("model_version")}
    return {
        "kind": descriptor.get("kind"),
        "simulated": descriptor.get("kind") == "mock",
        "adapter_id": descriptor.get("adapter_id"),
        "adapter_version": descriptor.get("adapter_version"),
        "configuration_id": descriptor.get("configuration_id"),
        "model": model,
        "inference_contract_version": descriptor.get("inference_contract_version"),
    }


def provenance_ok(header: Any, pinned: Any, units: Sequence[Mapping[str, Any]]) -> bool:
    """S10b. P1: manifest header == pinned descriptor (field by field) and that descriptor is the A03 mock's.
    P2: every analysed unit's ``Provenance`` == the projection of the pinned descriptor."""
    if not isinstance(header, Mapping) or not isinstance(pinned, Mapping) or dict(header) != dict(pinned):
        return False
    d = pinned
    if not (
        d.get("kind") == "mock"
        and d.get("adapter_id") == MOCK_ADAPTER_ID
        and d.get("adapter_version") == MOCK_ADAPTER_VERSION
        and isinstance(d.get("configuration_id"), str)
        and _FXS.fullmatch(d["configuration_id"])
        and d.get("model_id") is None
        and d.get("model_version") is None
        and d.get("inference_contract_version") == 1
    ):
        return False
    expected = projection(d)
    for u in units:
        if u.get("status") != "analyzed":
            continue
        if dict(u.get("analysis", {}).get("provenance") or {}) != expected:
            return False
    return True


def oracle_text(text: str, groups: Sequence[tuple[int, int, str]]) -> tuple[str, list[tuple[int, int]]]:
    """``text`` with each (start, end, substitute) replaced (groups sorted, disjoint); returns the output spans."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    pos = 0
    length = 0
    for s, e, sub in groups:
        out.append(text[pos:s])
        length += s - pos
        out.append(sub)
        spans.append((length, length + len(sub)))
        length += len(sub)
        pos = e
    out.append(text[pos:])
    return "".join(out), spans


def occurrence_at(unit: Mapping[str, Any], start: int, end: int) -> dict[str, Any] | None:
    """Locates [start, end) of a derivative unit in ONE ``w:t`` piece (the marker is whole in one node)."""
    for seg in unit["segments"]:
        if seg["kind"] == "original" and seg["unit_start"] <= start and end <= seg["unit_end"]:
            if len(seg["pieces"]) != 1:
                return None
            piece = seg["pieces"][0]
            off = piece["char_start"] + start - seg["unit_start"]
            return {
                "unit_id": unit["unit_id"],
                "start": start,
                "end": end,
                "part_name": piece["part_name"],
                "node_path": piece["node_path"],
                "char_start": off,
                "char_end": off + end - start,
            }
    return None


def binding_payload(row: Mapping[str, Any]) -> bytes:
    return canonical(
        {
            "scope_id": row["scope_id"],
            "derivative_id": row["derivative_id"],
            "context_id": row["context_id"],
            "context_kind": row["context_kind"],
            "sha256": row["sha256"],
            "size_bytes": int(row["size_bytes"]),
            "extractor": [row["extractor_id"], row["extractor_version"]],
            "occurrences": list(row["occurrences"] or []),
        }
    )


def binding_mac(index_key: bytes, row: Mapping[str, Any]) -> bytes:
    """HMAC-SHA256 with the context's equality-index key (E-A07-14): standard composition, no own cryptography."""
    return hmac.new(index_key, _BINDING_LABEL + b"\x00" + binding_payload(row), hashlib.sha256).digest()


def mac_ok(index_key: bytes, row: Mapping[str, Any], mac: bytes | None) -> bool:
    return mac is not None and hmac.compare_digest(binding_mac(index_key, row), bytes(mac))
