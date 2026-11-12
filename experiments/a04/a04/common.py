"""Shared helpers: hashing, limits, bounded reading of ZIP/Flate data, Unicode utilities (contract sections 5 and 16)."""

from __future__ import annotations

import hashlib
import zipfile
import zlib
from pathlib import Path

LIMITS = {
    "max_file_bytes": 2 * 1024 * 1024,
    "max_pdf_pages": 20,
    "max_zip_parts": 100,
    "max_total_uncompressed": 50 * 1024 * 1024,
    "max_ratio": 100,
    "max_unit_chars": 8192,
    "max_unit_bytes": 24576,
    "max_units": 500,
    "max_selected": 100,
    "max_nested_table_depth": 3,
    "max_doc_seconds": 60,
}


class Reject(Exception):
    """A document (or unit) is rejected; ``code`` is a closed, content-free reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: str | Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def read_zip_bounded(data: bytes) -> dict[str, bytes]:
    """Read every ZIP member. Declared sizes are checked first, then the member is inflated from the raw
    compressed bytes with a counter on the bytes ACTUALLY produced (it never trusts the headers); it aborts
    as soon as a limit is exceeded and rejects any mismatch between declared and produced size."""
    import io
    import struct

    if len(data) > LIMITS["max_file_bytes"]:
        raise Reject("limit_exceeded", "file size")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise Reject("corrupt_or_unreadable", "zip") from exc
    if len(infos) > LIMITS["max_zip_parts"]:
        raise Reject("limit_exceeded", "parts")
    if sum(i.file_size for i in infos) > LIMITS["max_total_uncompressed"]:
        raise Reject("limit_exceeded", "declared total")
    out: dict[str, bytes] = {}
    produced = 0
    for i in infos:
        if i.filename.startswith("/") or ".." in i.filename.split("/"):
            raise Reject("unsafe_part_name")
        if i.flag_bits & 0x1:
            raise Reject("encrypted")
        if i.compress_size and i.file_size > 65536 and i.file_size / i.compress_size > LIMITS["max_ratio"]:
            raise Reject("limit_exceeded", "declared ratio")
        hdr = data[i.header_offset : i.header_offset + 30]
        if len(hdr) < 30 or hdr[:4] != b"PK\x03\x04":
            raise Reject("corrupt_or_unreadable", "local header")
        n, m = struct.unpack("<HH", hdr[26:30])
        start = i.header_offset + 30 + n + m
        raw = data[start : start + i.compress_size]
        budget = LIMITS["max_total_uncompressed"] - produced
        if i.compress_type == zipfile.ZIP_STORED:
            body = raw
        elif i.compress_type == zipfile.ZIP_DEFLATED:
            d = zlib.decompressobj(-15)
            buf = bytearray()
            rest = raw
            try:
                while rest:
                    buf += d.decompress(rest, 65536)
                    rest = d.unconsumed_tail
                    if len(buf) > budget or (len(buf) > 65536 and len(buf) > LIMITS["max_ratio"] * max(len(raw), 1)):
                        raise Reject("limit_exceeded", "produced bytes")
                buf += d.flush()
            except zlib.error as exc:
                raise Reject("corrupt_or_unreadable", "deflate") from exc
            body = bytes(buf)
        else:
            raise Reject("unsupported_compression")
        produced += len(body)
        if produced > LIMITS["max_total_uncompressed"]:
            raise Reject("limit_exceeded", "produced total")
        if len(body) != i.file_size or (zlib.crc32(body) & 0xFFFFFFFF) != i.CRC:
            raise Reject("corrupt_or_unreadable", "size or crc mismatch")
        out[i.filename] = body
    return out


def bounded_inflate(raw: bytes, limit: int | None = None) -> bytes:
    """Inflate a zlib stream producing at most ``limit`` bytes (counting real output, not metadata)."""
    limit = LIMITS["max_total_uncompressed"] if limit is None else limit
    d = zlib.decompressobj()
    out = bytearray()
    data = raw
    try:
        while data:
            out += d.decompress(data, 65536)
            data = d.unconsumed_tail
            if len(out) > limit:
                raise Reject("limit_exceeded", "inflate produced bytes")
        out += d.flush()
    except zlib.error as exc:
        raise Reject("corrupt_or_unreadable", "flate") from exc
    if len(out) > limit:
        raise Reject("limit_exceeded", "inflate produced bytes")
    if len(raw) > 4096 and len(out) > LIMITS["max_ratio"] * len(raw):
        raise Reject("limit_exceeded", "inflate ratio")
    return bytes(out)


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def check_unit_limits(text: str) -> None:
    if len(text) > LIMITS["max_unit_chars"] or len(text.encode("utf-8")) > LIMITS["max_unit_bytes"]:
        raise Reject("limit_exceeded", "unit text")
