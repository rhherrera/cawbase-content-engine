"""Backend identification and fixed-namespace naming."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.engine import make_url

from ..constants import SCHEMA_NAME, SQLITE_PREFIX
from ..errors import InvalidConfigurationError


class Backend(StrEnum):
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"


SUPPORTED_DRIVERS = {"postgresql+asyncpg": Backend.POSTGRESQL, "sqlite+aiosqlite": Backend.SQLITE}


def backend_from_driver(drivername: str) -> Backend:
    try:
        return SUPPORTED_DRIVERS[drivername]
    except KeyError:
        raise InvalidConfigurationError(
            "Unsupported database driver; use 'postgresql+asyncpg' or 'sqlite+aiosqlite'."
        ) from None


def backend_from_url(url: str) -> Backend:
    try:
        return backend_from_driver(make_url(url).drivername)
    except InvalidConfigurationError:
        raise
    except Exception:
        raise InvalidConfigurationError("The database URL could not be parsed.") from None


@dataclass(frozen=True)
class Namespace:
    """Names of engine-owned objects for one backend."""

    backend: Backend

    @property
    def schema(self) -> str | None:
        return SCHEMA_NAME if self.backend is Backend.POSTGRESQL else None

    def table(self, logical: str) -> str:
        return logical if self.backend is Backend.POSTGRESQL else SQLITE_PREFIX + logical

    def index(self, logical: str) -> str:
        return self.table(logical) if self.backend is Backend.SQLITE else logical

    @property
    def version_table(self) -> str:
        return self.table("alembic_version")

    def qualified(self, logical: str) -> str:
        name = self.table(logical)
        return f"{SCHEMA_NAME}.{name}" if self.schema else name
