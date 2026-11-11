"""API key format, hashing and verification (D-25).

``cce1.<key_id>.<secret_text>``: ``key_id`` = ``k_`` + 24 lowercase hex chars; the secret is 32
random bytes whose canonical text is unpadded base64url (43 chars). The stored hash is SHA-256 of
the 32 *raw* bytes (not of the text, prefix or key_id). Comparison is constant-time; this does not
make the whole request constant-time and no such promise is made.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from .constants import KEY_PREFIX

KEY_ID_RE = re.compile(r"^k_[0-9a-f]{24}$")
SECRET_TEXT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
HASH_ALGORITHM = "sha256"
_DUMMY_HASH = hashlib.sha256(b"\x00" * 32).digest()


@dataclass(frozen=True)
class GeneratedKey:
    key_id: str
    full_key: str
    secret_hash: bytes


def encode_secret(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_secret(text: str) -> bytes | None:
    """Strict, canonical decode; ``None`` for anything non-canonical."""
    if not SECRET_TEXT_RE.fullmatch(text):
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=")
    except (binascii.Error, ValueError):
        return None
    if len(raw) != 32 or encode_secret(raw) != text:
        return None
    return raw


def hash_secret(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()


def generate_key() -> GeneratedKey:
    key_id = "k_" + secrets.token_hex(12)
    raw = secrets.token_bytes(32)
    return GeneratedKey(key_id, f"{KEY_PREFIX}.{key_id}.{encode_secret(raw)}", hash_secret(raw))


@dataclass(frozen=True)
class ParsedKey:
    key_id: str
    secret_hash: bytes


def parse_key(value: str) -> ParsedKey | None:
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != KEY_PREFIX or not KEY_ID_RE.fullmatch(parts[1]):
        return None
    raw = decode_secret(parts[2])
    if raw is None:
        return None
    return ParsedKey(parts[1], hash_secret(raw))


def verify(stored_hash: bytes | None, presented_hash: bytes) -> bool:
    """Constant-time compare; an unknown key compares against a dummy so the work is similar."""
    return hmac.compare_digest(stored_hash if stored_hash is not None else _DUMMY_HASH, presented_hash) and (
        stored_hash is not None
    )
