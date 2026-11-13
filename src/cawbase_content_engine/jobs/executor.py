"""Reference executor: polls eligible jobs and runs them; explicit start/close, never implicit (docs 14.2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from ..errors import EngineError
from .worker import JobWorker

if TYPE_CHECKING:
    from ..engine import ContentEngine

log = logging.getLogger("cawbase_content_engine.jobs")


class ReferenceExecutor:
    def __init__(
        self,
        engine: ContentEngine,
        *,
        concurrency: int = 4,
        poll_interval_s: float = 5.0,
        cleanup_interval_s: float | None = None,
        worker_id: str = "reference-executor",
    ) -> None:
        self._e, self._conc, self._poll, self._cleanup = engine, concurrency, poll_interval_s, cleanup_interval_s
        self._worker = JobWorker(engine, worker_id)
        self._tasks: set[asyncio.Task[None]] = set()
        self._main: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._main is not None:
            return
        self._stop.clear()
        self._main = asyncio.create_task(self._loop())
        if self._cleanup:
            self._tasks.add(asyncio.create_task(self._cleanup_loop()))

    async def aclose(self) -> None:
        self._stop.set()
        tasks = [t for t in (self._main, *self._tasks) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._main, self._tasks = None, set()

    async def _loop(self) -> None:
        running: set[asyncio.Task[Any]] = set()
        while not self._stop.is_set():
            free = self._conc - len(running)
            if free > 0:
                try:
                    for c in await self._worker.claim(limit=free):
                        t = asyncio.create_task(self._worker.run_claimed(c))
                        running.add(t)
                        t.add_done_callback(running.discard)
                except EngineError:
                    log.warning("executor_claim_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self._poll)
        for t in running:
            t.cancel()

    async def _cleanup_loop(self) -> None:
        from .maintenance import run_cleanup

        assert self._cleanup
        while not self._stop.is_set():
            try:
                await run_cleanup(self._e)
            except EngineError:
                log.warning("executor_cleanup_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self._cleanup)
