"""Column types that behave the same on PostgreSQL and SQLite."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


class UtcDateTime(sa.types.TypeDecorator[datetime]):
    """Always timezone-aware UTC in Python; ``timestamptz`` on PostgreSQL, naive UTC text on SQLite."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: Any, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        result: datetime = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return result


def json_type() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def big_pk() -> sa.types.TypeEngine[int]:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")
