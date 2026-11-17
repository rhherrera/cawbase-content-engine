"""Marker grammar and the bounded parser (docs/text-protection.md, sections 6.1-6.3). Pure: no I/O, no state."""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

from ..errors import ValidationFailedError

REDACTED = "[REDACTED]"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 without I, L, O, U
TOKEN_LEN = 16
MARKER_LEN = 25  # "[[CCE1:" + 16 + "]]"

# Every expression is compiled with re.ASCII (no Unicode equivalences: e.g. the Kelvin sign is not "k").
#: S - strict marker (case sensitive).
STRICT = re.compile(r"\[\[CCE1:[0-9A-HJKMNP-TV-Z]{16}\]\]", re.ASCII)
#: R1 - the *core* of a marker (identifier + numeric version + ":" + exactly 16 alphanumerics) with another
#: capitalization, spacing, one or no bracket, a letter outside the alphabet or another version.
_CORE = re.compile(r"(?<![0-9a-z])cce[0-9]{1,3}\s*:\s*[0-9a-z]{16}(?![0-9a-z])", re.ASCII | re.IGNORECASE)
#: R2 - the double-bracket marker *prefix* with any continuation (truncated or lengthened token, ...).
_PREFIX = re.compile(r"\[\s*\[\s*cce[0-9]{1,3}\s*:", re.ASCII | re.IGNORECASE)


@dataclass(frozen=True)
class Scan:
    strict: tuple[tuple[int, int, str], ...]  # (start, end, marker) in order of appearance, with repeats
    altered: bool  # a recognizable alteration (R1 or R2) outside any strict marker


def scan(text: str) -> Scan:
    """Classifies the marker-like content of ``text``. Prose that merely mentions "CCE" matches nothing."""
    spans = [(m.start(), m.end(), m.group(0)) for m in STRICT.finditer(text)]
    altered = False
    for rx in (_CORE, _PREFIX):
        for m in rx.finditer(text):
            if not any(a <= m.start() and m.end() <= b for a, b, _ in spans):
                altered = True
                break
        if altered:
            break
    return Scan(tuple(spans), altered)


def find_markers(text: str) -> tuple[str, ...]:
    """Strict markers in order of first appearance, without duplicates (pure; no permissions, no I/O)."""
    if not isinstance(text, str):
        raise ValidationFailedError("Invalid text.", field="text")
    seen: dict[str, None] = {}
    for m in STRICT.finditer(text):
        seen.setdefault(m.group(0), None)
    return tuple(seen)


def markers_of_all(texts: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for t in texts:
        out.update(m.group(0) for m in STRICT.finditer(t))
    return out


def is_strict(marker: object) -> bool:
    return isinstance(marker, str) and STRICT.fullmatch(marker) is not None


def new_token() -> str:
    """80 random bits as 16 Crockford characters: not derived from the value nor from any key."""
    n = int.from_bytes(secrets.token_bytes(10), "big")
    return "".join(ALPHABET[(n >> (5 * i)) & 31] for i in range(TOKEN_LEN))


def marker_of(token: str) -> str:
    return f"[[CCE1:{token}]]"


def token_of(marker: str) -> str:
    return marker[7:-2]
