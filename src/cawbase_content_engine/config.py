"""Explicit, validated configuration. Constructing it performs no I/O and reads no environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from .errors import InvalidConfigurationError
from .persistence.dialect import Backend, backend_from_driver, backend_from_url

ENV_PREFIX = "CAWBASE_CONTENT_ENGINE_"


@dataclass(frozen=True)
class Limits:
    """Operational limits (initial values, see docs/installation-and-operations.md)."""

    max_body_bytes: int = 64 * 1024
    max_metadata_bytes: int = 4096
    max_display_name: int = 200
    max_description: int = 1000
    max_label: int = 100
    max_scope_grants_per_key: int = 100
    max_active_keys_per_client: int = 100
    default_page_size: int = 50
    max_page_size: int = 200
    # A02 (profiles, categories, sensitive types)
    max_profiles_per_scope: int = 50
    max_profile_rows_per_scope: int = 500
    max_elements_per_profile: int = 50
    max_element_rows_per_profile: int = 250
    max_versions_per_profile: int = 500
    max_snapshot_bytes: int = 1024 * 1024
    max_element_name: int = 100
    max_element_definition: int = 1500
    max_example: int = 300
    max_examples_per_list: int = 5
    max_element_text_bytes: int = 8192
    max_priority: int = 10000

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, int) or value <= 0:
                raise InvalidConfigurationError(f"Limit '{name}' must be a positive integer.")
        if self.default_page_size > self.max_page_size:
            raise InvalidConfigurationError("default_page_size cannot exceed max_page_size.")


_UNSET: Any = None


@dataclass(frozen=True)
class EngineConfig:
    """Exactly one of ``database_url`` / ``async_engine`` must be given (D-12)."""

    database_url: SecretStr | None = field(default=None, repr=False)
    async_engine: AsyncEngine | None = field(default=None, repr=False)
    pool_size: int | None = None
    max_overflow: int | None = None
    pool_timeout_s: float | None = None
    connect_timeout_s: float = 10.0
    statement_timeout_ms: int = 5000
    lock_timeout_s: float = 60.0
    migration_mode: Literal["external", "on_start"] = "external"
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        url = self.database_url
        if isinstance(url, str):
            object.__setattr__(self, "database_url", SecretStr(url))
            url = self.database_url
        if url is not None and self.async_engine is not None:
            raise InvalidConfigurationError(
                "Ambiguous connection: provide either 'database_url' or 'async_engine', not both."
            )
        if url is None and self.async_engine is None:
            raise InvalidConfigurationError("Provide either 'database_url' or 'async_engine'.")
        if self.async_engine is not None:
            if not isinstance(self.async_engine, AsyncEngine):
                raise InvalidConfigurationError("'async_engine' must be a SQLAlchemy AsyncEngine.")
            backend_from_driver(self.async_engine.sync_engine.url.drivername)
            if any(v is not None for v in (self.pool_size, self.max_overflow, self.pool_timeout_s)):
                raise InvalidConfigurationError(
                    "Pool options do not apply to an injected engine; the host owns its pool."
                )
        assert url is None or isinstance(url, SecretStr)
        if url is not None:
            backend = backend_from_url(url.get_secret_value())
            if backend is Backend.SQLITE and ":memory:" in url.get_secret_value():
                raise InvalidConfigurationError("In-memory SQLite is not supported; use a file.")
        for name in ("pool_size", "max_overflow"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, int) or v < 0 or (name == "pool_size" and v < 1)):
                raise InvalidConfigurationError(f"'{name}' is out of range.")
        for name in ("pool_timeout_s", "connect_timeout_s", "lock_timeout_s"):
            v = getattr(self, name)
            if v is not None and not (0 < v <= 3600):
                raise InvalidConfigurationError(f"'{name}' is out of range.")
        if not (0 < self.statement_timeout_ms <= 3_600_000):
            raise InvalidConfigurationError("'statement_timeout_ms' is out of range.")
        if self.migration_mode not in ("external", "on_start"):
            raise InvalidConfigurationError("'migration_mode' must be 'external' or 'on_start'.")

    @property
    def backend(self) -> Backend:
        if self.async_engine is not None:
            return backend_from_driver(self.async_engine.sync_engine.url.drivername)
        assert self.database_url is not None
        return backend_from_url(self.database_url.get_secret_value())

    @property
    def owns_engine(self) -> bool:
        return self.async_engine is None

    def with_url(self, url: str | SecretStr) -> EngineConfig:
        return replace(self, database_url=SecretStr(url) if isinstance(url, str) else url, async_engine=None)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> EngineConfig:
        """Explicit environment reader. Precedence: explicit overrides > environment > defaults."""
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {}
        url = read_secret(env, "DATABASE_URL")
        if url is not None:
            values["database_url"] = SecretStr(url)
        for key, cast in (
            ("POOL_SIZE", int),
            ("MAX_OVERFLOW", int),
            ("POOL_TIMEOUT_S", float),
            ("CONNECT_TIMEOUT_S", float),
            ("STATEMENT_TIMEOUT_MS", int),
            ("LOCK_TIMEOUT_S", float),
        ):
            raw = env.get(ENV_PREFIX + key)
            if raw is not None:
                try:
                    values[key.lower()] = cast(raw)
                except ValueError:
                    raise InvalidConfigurationError(f"'{ENV_PREFIX}{key}' is not a valid number.") from None
        values.update(overrides)
        return cls(**values)


def read_secret(env: Mapping[str, str], name: str) -> str | None:
    """Read ``NAME`` or ``NAME_FILE`` (mutually exclusive). Never logs or echoes the value."""
    direct = env.get(ENV_PREFIX + name)
    file_ = env.get(ENV_PREFIX + name + "_FILE")
    if direct is not None and file_ is not None:
        raise InvalidConfigurationError(f"Provide either {ENV_PREFIX}{name} or {ENV_PREFIX}{name}_FILE, not both.")
    if file_ is not None:
        try:
            return Path(file_).read_text(encoding="utf-8").strip()
        except OSError:
            raise InvalidConfigurationError(f"The file in {ENV_PREFIX}{name}_FILE could not be read.") from None
    return direct
