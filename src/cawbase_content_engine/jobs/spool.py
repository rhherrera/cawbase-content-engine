"""Private, bounded spool for verified downloads (docs 3.7).

Each process owns ``spool/<instance>/`` and holds an exclusive ``flock`` on ``.owner`` for its whole life. The
directory is built under a hidden name, locked, and only then renamed into place, so a concurrently starting
sweeper can never see (and delete) a live spool that is not yet locked. Residue of dead processes is removed
at startup and by ``run_cleanup`` of the node.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from ..errors import StorageUnavailableError


class Spool:
    def __init__(self, root: str | os.PathLike[str], max_bytes: int) -> None:
        self._base = Path(root) / "spool"
        self._base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._max = max_bytes
        self._used = 0
        self._lock_fd = -1
        self._dir: Path | None = None
        self._n = 0

    def start(self) -> Path:
        if self._dir is not None:
            return self._dir
        ident = os.urandom(8).hex()
        hidden = self._base / f".new-{ident}"
        hidden.mkdir(mode=0o700)
        fd = os.open(hidden / ".owner", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)  # locked BEFORE it becomes visible to sweepers
        final = self._base / ident
        os.rename(hidden, final)
        self._lock_fd, self._dir = fd, final
        self.sweep()
        return final

    def close(self) -> None:
        if self._dir is None:
            return
        shutil.rmtree(self._dir, ignore_errors=True)
        os.close(self._lock_fd)
        self._lock_fd, self._dir = -1, None

    def sweep(self) -> int:
        """Removes spool directories whose owner process is dead (its flock can be taken without waiting)."""
        removed = 0
        for entry in self._base.iterdir():
            if entry.name.startswith(".") or not entry.is_dir() or entry == self._dir:
                continue
            owner = entry / ".owner"
            try:
                fd = os.open(owner, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # a live process owns it
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            finally:
                os.close(fd)
        return removed

    def reserve(self, size: int) -> None:
        if self._used + size > self._max:
            raise StorageUnavailableError("The download spool is full.")
        self._used += size

    def release(self, size: int) -> None:
        self._used = max(0, self._used - size)

    def new_path(self) -> Path:
        d = self.start()
        self._n += 1
        return d / f"dl-{self._n}-{os.urandom(4).hex()}"


class SpooledStream:
    """Async iterator over a verified spool file; deleting it (and releasing its budget) on close."""

    def __init__(self, spool: Spool, path: Path, size: int) -> None:
        self.spool, self.path, self.size = spool, path, size
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            with open(self.path, "rb") as fh:  # noqa: ASYNC230
                while True:
                    chunk = await asyncio.to_thread(fh.read, 256 * 1024)
                    if not chunk:
                        return
                    yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.path.unlink()
        except OSError:
            pass
        self.spool.release(self.size)
