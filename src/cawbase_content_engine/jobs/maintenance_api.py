"""Thin object over the maintenance functions (trusted local surface)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import maintenance as m
from .models import CleanupSummary, PurgeSummary

if TYPE_CHECKING:
    from ..engine import ContentEngine


class Maintenance:
    def __init__(self, engine: ContentEngine) -> None:
        self._e = engine

    async def list_runnable_jobs(self, *, limit: int = 100, older_than_s: float = 60.0) -> list[tuple[str, str]]:
        return await m.list_runnable_jobs(self._e, limit=limit, older_than_s=older_than_s)

    async def recover_expired_leases(self, *, limit: int = 100) -> int:
        return await m.recover_expired_leases(self._e, limit=limit)

    async def run_cleanup(self, *, batch_size: int | None = None) -> CleanupSummary:
        return await m.run_cleanup(self._e, batch_size=batch_size)

    async def run_protection_cleanup(self, *, batch_size: int | None = None) -> Any:
        """A06: expire due contexts and purge old tombstones (explicit, bounded, repeatable; needs no keys)."""
        from ..protection import maintenance as pm

        return await pm.run_protection_cleanup(self._e, batch_size=batch_size)

    async def protection_key_usage(self) -> Any:
        """A06: live contexts per key version, to decide when a version can be retired (counts only)."""
        from ..protection import maintenance as pm

        return await pm.protection_key_usage(self._e)

    async def purge_admin_events(
        self, *, older_than: datetime, batch_size: int | None = None, dry_run: bool = True
    ) -> PurgeSummary:
        return await m.purge_admin_events(self._e, older_than=older_than, batch_size=batch_size, dry_run=dry_run)
