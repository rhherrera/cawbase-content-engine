"""Composition of standard primitives from ``cryptography`` (docs/text-protection.md, section 9). No own crypto.

Imported lazily: never when the base package is imported (D-33). Per record: HKDF-SHA256 subkey from the
versioned master key and a random salt, AES-256-GCM with a vinculating AAD that includes the equality index.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import struct
from typing import Any

from ..errors import InvalidConfigurationError, MappingIntegrityError, ProtectionKeyUnavailableError
from .types import KEY_BYTES, KEY_VERSION_RE, KeyProvider

PAD = 64
SALT_LEN, NONCE_LEN = 16, 12
_INFO_RECORD = b"cce.protection.record.v1"
_INFO_INDEX_KEY = b"cce.protection.index-key.v1"
_LABEL_MAP = b"cce.protection.map.v1"
_LABEL_INDEX_KEY = b"cce.protection.indexkey.v1"
_EQ = b"cce.protection.eq.v1"


def _lib() -> Any:
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        raise InvalidConfigurationError(
            "Text protection needs the 'protection' extra (the 'cryptography' package)."
        ) from None

    class _L:
        pass

    lib = _L()
    lib.hashes, lib.AESGCM, lib.HKDF, lib.InvalidTag = hashes, AESGCM, HKDF, InvalidTag  # type: ignore[attr-defined]
    return lib


def lp(*parts: bytes | str) -> bytes:
    """Length-prefixed encoding: unambiguous, so no two different tuples share an encoding."""
    out = bytearray()
    for p in parts:
        b = p if isinstance(p, bytes) else p.encode("utf-8")
        out += struct.pack(">I", len(b)) + b
    return bytes(out)


def _subkey(lib: Any, master: bytes, salt: bytes, info: bytes) -> bytes:
    return bytes(lib.HKDF(algorithm=lib.hashes.SHA256(), length=KEY_BYTES, salt=salt, info=info).derive(master))


def self_test() -> None:
    """Known-answer checks at start (docs 9.4): the library loads and primitives behave as specified."""
    lib = _lib()
    # RFC 5869, test case 1 (HKDF-SHA256)
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    okm = lib.HKDF(algorithm=lib.hashes.SHA256(), length=42, salt=salt, info=info).derive(ikm)
    if okm.hex() != "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865":
        raise InvalidConfigurationError("The cryptography library failed its known-answer test.")
    # NIST/McGrew-Viega GCM test case 3 (AES-256 is checked through a round trip with a tag mismatch)
    key = bytes(range(32))
    nonce = bytes(range(12))
    ct = lib.AESGCM(key).encrypt(nonce, b"known answer", b"aad")
    if lib.AESGCM(key).decrypt(nonce, ct, b"aad") != b"known answer":
        raise InvalidConfigurationError("The cryptography library failed its known-answer test.")
    try:
        lib.AESGCM(key).decrypt(nonce, ct, b"other")
    except lib.InvalidTag:
        return
    raise InvalidConfigurationError("The cryptography library failed its known-answer test.")


# ---- key access ---------------------------------------------------------------------------------------
def master_key(keys: KeyProvider, version: str) -> bytes:
    """The key of ``version`` or ``protection_key_unavailable``. Never logs or echoes the material."""
    try:
        key = keys.key(version) if KEY_VERSION_RE.fullmatch(version) else None
    except Exception:
        key = None
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise ProtectionKeyUnavailableError()
    return bytes(key)


def active_version(keys: KeyProvider) -> tuple[str, bytes]:
    try:
        version = keys.active_version()
    except Exception:
        raise ProtectionKeyUnavailableError() from None
    if not isinstance(version, str) or not KEY_VERSION_RE.fullmatch(version):
        raise ProtectionKeyUnavailableError()
    return version, master_key(keys, version)


# ---- mappings -----------------------------------------------------------------------------------------
def _record_aad(scope_id: str, context_id: str, kind: str, token: str, version: str, eq_index: bytes) -> bytes:
    return lp(_LABEL_MAP, scope_id, context_id, kind, token, version, eq_index)


def seal_fragment(
    master: bytes,
    *,
    scope_id: str,
    context_id: str,
    kind: str,
    token: str,
    version: str,
    eq_index: bytes,
    fragment: str,
) -> tuple[bytes, bytes, bytes]:
    """Returns ``(salt, nonce, ciphertext)``. Plaintext = u32 length || UTF-8 || zeros, padded to 64 bytes."""
    lib = _lib()
    raw = fragment.encode("utf-8")
    body = struct.pack(">I", len(raw)) + raw
    body += b"\x00" * (-len(body) % PAD)
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    aad = _record_aad(scope_id, context_id, kind, token, version, eq_index)
    ct = lib.AESGCM(_subkey(lib, master, salt, _INFO_RECORD)).encrypt(nonce, body, aad)
    return salt, nonce, ct


def open_fragment(
    master: bytes,
    *,
    scope_id: str,
    context_id: str,
    kind: str,
    token: str,
    version: str,
    eq_index: bytes,
    salt: bytes,
    nonce: bytes,
    ciphertext: bytes,
) -> bytes:
    """Authenticates and decrypts; returns the UTF-8 bytes of the fragment. Any failure is
    ``mapping_integrity_failed`` (the AAD includes the equality index stored in the row)."""
    lib = _lib()
    try:
        if len(salt) != SALT_LEN or len(nonce) != NONCE_LEN:
            raise MappingIntegrityError()
        aad = _record_aad(scope_id, context_id, kind, token, version, bytes(eq_index))
        body = lib.AESGCM(_subkey(lib, master, bytes(salt), _INFO_RECORD)).decrypt(bytes(nonce), bytes(ciphertext), aad)
    except MappingIntegrityError:
        raise
    except Exception:  # InvalidTag, wrong lengths, anything the library rejects
        raise MappingIntegrityError() from None
    if len(body) < 4 or len(body) % PAD:
        raise MappingIntegrityError()
    (n,) = struct.unpack(">I", body[:4])
    if n > len(body) - 4 or any(body[4 + n :]):
        raise MappingIntegrityError()
    return bytes(body[4 : 4 + n])


def same_fragment(decrypted: bytes, fragment: str) -> bool:
    """Constant-time comparison of the decrypted original with the exact fragment being protected."""
    return bool(hmac.compare_digest(decrypted, fragment.encode("utf-8")))


# ---- context index key ---------------------------------------------------------------------------------
def new_index_key() -> bytes:
    return os.urandom(KEY_BYTES)


def wrap_index_key(master: bytes, *, scope_id: str, context_id: str, version: str, raw: bytes) -> bytes:
    lib = _lib()
    salt, nonce = os.urandom(SALT_LEN), os.urandom(NONCE_LEN)
    aad = lp(_LABEL_INDEX_KEY, scope_id, context_id, "index-key", version)
    return bytes(salt + nonce + lib.AESGCM(_subkey(lib, master, salt, _INFO_INDEX_KEY)).encrypt(nonce, raw, aad))


def unwrap_index_key(master: bytes, *, scope_id: str, context_id: str, version: str, wrapped: bytes) -> bytes:
    lib = _lib()
    try:
        wrapped = bytes(wrapped)
        salt, nonce, ct = wrapped[:SALT_LEN], wrapped[SALT_LEN : SALT_LEN + NONCE_LEN], wrapped[SALT_LEN + NONCE_LEN :]
        aad = lp(_LABEL_INDEX_KEY, scope_id, context_id, "index-key", version)
        raw = lib.AESGCM(_subkey(lib, master, salt, _INFO_INDEX_KEY)).decrypt(nonce, ct, aad)
    except Exception:
        raise MappingIntegrityError() from None
    if len(raw) != KEY_BYTES:
        raise MappingIntegrityError()
    return bytes(raw)


def equality_index(index_key: bytes, fragment: str) -> bytes:
    """HMAC-SHA256 under the per-context index key: keyed (nobody with only the database can test guesses)."""
    return bytes(hmac.new(index_key, _EQ + fragment.encode("utf-8"), hashlib.sha256).digest())


_ = re
