"""Shared pieces of the extraction child: rejection signalling and bounded readers."""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from typing import Any


class Reject(Exception):
    """The document is refused with a closed reason code (docs 10.4)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Limits:
    max_file_bytes: int
    max_pdf_pages: int
    max_zip_entries: int
    max_docx_parts_processed: int
    max_decompressed_total_bytes: int
    max_decompressed_entry_bytes: int
    max_compression_ratio: int
    max_xml_depth: int
    max_xml_nodes_total: int
    max_table_nesting: int
    max_pdf_objects: int
    max_pdf_content_bytes_per_page: int
    max_form_xobject_depth: int
    max_text_operators_per_page: int
    max_units_per_document: int = 5000
    max_document_text_chars: int = 2_000_000
    max_extraction_output_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> Limits:
        return cls(**{k: params[k] for k in cls.__dataclass_fields__})


@dataclass
class Unit:
    unit_id: str
    kind: str
    ordinal: int
    text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    empty: bool = False

    def add(self, text: str, *, kind: str = "original", pieces: list[dict[str, Any]] | None = None) -> None:
        if not text:
            return
        start = len(self.text)
        self.text += text
        self.segments.append({"unit_start": start, "unit_end": start + len(text), "kind": kind, "pieces": pieces or []})

    def finish(self) -> dict[str, Any]:
        self.empty = not any(s["kind"] == "original" for s in self.segments)
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "ordinal": self.ordinal,
            "text": self.text,
            "segments": self.segments,
            "empty": self.empty,
        }


def inflate_limited(raw: bytes, limit: int) -> bytes:
    """FlateDecode with a hard cap on the bytes actually produced (never trusts declared sizes)."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, limit + 1)
    except zlib.error:
        raise Reject("parser_repair") from None
    if len(out) > limit or d.unconsumed_tail:
        raise Reject("decompression_limit")
    return out
