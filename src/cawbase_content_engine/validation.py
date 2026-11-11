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
