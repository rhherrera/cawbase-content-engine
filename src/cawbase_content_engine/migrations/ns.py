"""Helpers for migration scripts (dialect-aware names)."""

from __future__ import annotations

from sqlalchemy.engine import Connection

from ..persistence.dialect import Backend, Namespace


def namespace(bind: Connection) -> Namespace:
    return Namespace(Backend(bind.dialect.name))
