"""Input validation shared by Python and HTTP (single business logic)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .config import Limits
from .errors import LimitExceededError, ValidationFailedError

SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CLIENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def scope_id(value: Any, field: str = "scope_id") -> str:
    if not isinstance(value, str) or not SCOPE_ID_RE.fullmatch(value):
        raise ValidationFailedError("Invalid scope_id.", field=field)
    return value


def client_name(value: Any) -> str:
    if not isinstance(value, str) or not CLIENT_NAME_RE.fullmatch(value):
        raise ValidationFailedError("Invalid client name.", field="name")
    return value


def text(value: Any, field: str, maximum: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    if len(value) > maximum:
        raise LimitExceededError(f"{field} is too long.", field=field)
    if any(ord(c) < 32 for c in value):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    return value


def metadata(value: Any, limits: Limits) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationFailedError("metadata must be an object.", field="metadata")
    try:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationFailedError("metadata must be JSON-serializable.", field="metadata") from None
    if len(raw.encode()) > limits.max_metadata_bytes:
        raise LimitExceededError("metadata is too large.", field="metadata")
    return value


def request_id(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ValidationFailedError("request_id must be a UUID.", field="request_id") from None


def aware_datetime(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationFailedError(f"{field} must be a timezone-aware datetime.", field=field)
    return value.astimezone(UTC)


# ---- A02: profiles, categories and sensitive types -------------------------------------------------
import math  # noqa: E402
import unicodedata  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LANGUAGES = ("es", "en")
PROFILE_STATUSES = ("active", "disabled", "deleted")
ELEMENT_STATES = ("enabled", "disabled", "deleted")
MODES = ("ranking", "resolution")
INITIAL_CATALOGS = ("starter", "empty")


def slug(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    return value


def choice(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    return value


def int_in(value: Any, field: str, low: int, high: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < low or (high is not None and value > high):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    return value


def revision(value: Any, field: str = "expected_revision", *, low: int = 1) -> int:
    return int_in(value, field, low)


def unit_float(value: Any, field: str) -> float:
    """Finite number in [0, 1]; always returned as ``float`` so every layer serializes it identically."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    return number


def content_text(value: Any, field: str, maximum: int, *, multiline: bool = False) -> str:
    """Consumer text: NFC, trimmed, non-empty, no control characters (``\\n``/``\\t`` only if ``multiline``)."""
    if not isinstance(value, str):
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    text_value = unicodedata.normalize("NFC", value).strip()
    if not text_value:
        raise ValidationFailedError(f"Invalid {field}.", field=field)
    if len(text_value) > maximum:
        raise LimitExceededError(f"{field} is too long.", field=field)
    for ch in text_value:
        if ord(ch) < 32 and not (multiline and ch in "\n\t") or ord(ch) == 127:
            raise ValidationFailedError(f"Invalid {field}.", field=field)
    return text_value


def name_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def examples(value: Any, limits: Limits) -> dict[str, list[str]]:
    if value is None:
        return {"positive": [], "negative": []}
    if not isinstance(value, dict) or set(value) - {"positive", "negative"}:
        raise ValidationFailedError("Invalid examples.", field="examples")
    out: dict[str, list[str]] = {}
    for side in ("positive", "negative"):
        items = value.get(side, [])
        if not isinstance(items, (list, tuple)):
            raise ValidationFailedError("Invalid examples.", field="examples")
        if len(items) > limits.max_examples_per_list:
            raise LimitExceededError("Too many examples.", field="examples")
        out[side] = [content_text(i, "examples", limits.max_example, multiline=True) for i in items]
    return out


def translations(value: Any, profile_language: str, limits: Limits) -> dict[str, dict[str, str]] | None:
    """At most one entry, for the language that is not the profile's, with name and definition."""
    if value is None:
        return None
    other = "en" if profile_language == "es" else "es"
    if not isinstance(value, dict) or not value or set(value) - {other}:
        raise ValidationFailedError("Invalid translations.", field="translations")
    entry = value[other]
    if not isinstance(entry, dict) or set(entry) != {"name", "definition"}:
        raise ValidationFailedError("Invalid translations.", field="translations")
    return {
        other: {
            "name": content_text(entry["name"], "translations", limits.max_element_name),
            "definition": content_text(
                entry["definition"], "translations", limits.max_element_definition, multiline=True
            ),
        }
    }


def element_bytes(
    name: str, definition: str, ex: dict[str, list[str]], tr: dict[str, dict[str, str]] | None, limits: Limits
) -> None:
    total = len(name.encode()) + len(definition.encode())
    total += sum(len(e.encode()) for side in ex.values() for e in side)
    if tr:
        total += sum(len(x.encode()) for entry in tr.values() for x in entry.values())
    if total > limits.max_element_text_bytes:
        raise LimitExceededError("The element text is too large.", field="definition")
