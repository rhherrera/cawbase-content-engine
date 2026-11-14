"""Fixed namespace and compatibility constants (D-15, D-24)."""

from __future__ import annotations

import hashlib

SCHEMA_NAME = "cawbase_content_engine"
SQLITE_PREFIX = "cawbase_content_engine_"

#: Schema level this runtime requires (bumped by every migration that this release needs).
SCHEMA_LEVEL = 3
#: Level of this runtime; a schema whose ``min_runtime_level`` is higher is rejected.
RUNTIME_LEVEL = 1

#: PostgreSQL advisory-lock key for migrations (stable, derived from a fixed name).
MIGRATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"cawbase_content_engine.migrations").digest()[:8], "big", signed=True
)

KEY_PREFIX = "cce1"
