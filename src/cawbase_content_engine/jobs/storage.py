"""Reference storage: private filesystem with create-only writes, durable seals and writer-release evidence.

Layout under ``root``: ``objects/<ab>/<hex32>`` (final), ``tmp/<ab>_<hex32>.part`` (temporary),
``tmp/<ab>_<hex32>.lock`` (one ``flock`` held for the whole write). A seal is an empty *directory* in the
final and temporary paths: ``O_EXCL`` opens and ``link`` fail on it, ``delete`` never removes it.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import os
import re
import time
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path

from .setup import (
    ObjectInfo,
    ObjectMissing,
    PutReceipt,
    StorageDescriptor,
    StorageError,
    StorageKeyExists,
    StorageLimitExceeded,
)

_KEY = re.compile(r"^cce/([0-9a-f]{2})/([0-9a-f]{32})$")
CHUNK = 256 * 1024


def new_key() -> str:
    h = os.urandom(16).hex()
    return f"cce/{h[:2]}/{h}"


class FilesystemStorage:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        for sub in ("objects", "tmp"):
            (self._root / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        self._descriptor = StorageDescriptor(
            backend_id="fs:" + hashlib.sha256(str(self._root.resolve()).encode()).hexdigest()[:12]
        )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def descriptor(self) -> StorageDescriptor:
        return self._descriptor

    # ---- paths ----------------------------------------------------------------------------
    def _paths(self, key: str) -> tuple[Path, Path, Path]:
        m = _KEY.match(key)
        if not m:
            raise StorageError("invalid key")
        ab, h = m.groups()
        return (
            self._root / "objects" / ab / h,
            self._root / "tmp" / f"{ab}_{h}.part",
            self._root / "tmp" / f"{ab}_{h}.lock",
        )

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    # ---- write ----------------------------------------------------------------------------
    async def put_new(
        self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int, abort_after: datetime
    ) -> PutReceipt:
        final, tmp, lock = self._paths(key)
        deadline = abort_after.timestamp()
        lock_fd = await asyncio.to_thread(os.open, lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        tmp_fd = -1
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StorageKeyExists from None
            try:
                tmp_fd = await asyncio.to_thread(
                    os.open, tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError:
                raise StorageKeyExists from None
            digest, size = hashlib.sha256(), 0
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes or time.time() > deadline:
                    raise StorageLimitExceeded
                digest.update(chunk)
                await asyncio.to_thread(os.write, tmp_fd, chunk)
            await asyncio.to_thread(os.fsync, tmp_fd)
            final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                await asyncio.to_thread(os.link, tmp, final)  # by path: fails on a seal, never replaces
            except FileExistsError:
                raise StorageKeyExists from None
            except OSError as exc:  # the temporary became a seal directory (EPERM/EISDIR/ENOENT)
                raise StorageKeyExists from exc
            try:
                os.unlink(tmp)
            except OSError:
                pass
            await asyncio.to_thread(self._fsync_dir, final.parent)
            await asyncio.to_thread(self._fsync_dir, tmp.parent)
            try:
                os.unlink(lock)
            except OSError:
                pass
            return PutReceipt(size=size, sha256=digest.digest())
        except StorageError:
            self._drop_partial(tmp)
            raise
        except OSError as exc:
            self._drop_partial(tmp)
            raise StorageError from exc
        except BaseException:
            self._drop_partial(tmp)
            raise
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)
            os.close(lock_fd)  # releases the flock

    @staticmethod
    def _drop_partial(tmp: Path) -> None:
        try:
            if tmp.is_file() and not tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass

    # ---- read -----------------------------------------------------------------------------
    async def open_read(self, key: str) -> AsyncIterator[bytes]:
        final, _, _ = self._paths(key)
        try:
            fd = await asyncio.to_thread(os.open, final, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except (FileNotFoundError, NotADirectoryError):
            raise ObjectMissing from None
        except OSError as exc:
            raise StorageError from exc
        try:
            if not _is_regular(fd):
                raise ObjectMissing
            while True:
                data = await asyncio.to_thread(os.read, fd, CHUNK)
                if not data:
                    return
                yield data
        finally:
            os.close(fd)

    async def stat(self, key: str) -> ObjectInfo | None:
        final, _, _ = self._paths(key)
        try:
            st = await asyncio.to_thread(os.lstat, final)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            raise StorageError from exc
        import stat as _stat

        return ObjectInfo(size=st.st_size) if _stat.S_ISREG(st.st_mode) else None

    # ---- delete / seal --------------------------------------------------------------------
    async def delete(self, key: str) -> None:
        final, tmp, _ = self._paths(key)
        for path in (final, tmp):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise StorageError from exc

    async def delete_and_seal(self, key: str) -> None:
        final, tmp, _ = self._paths(key)
        await asyncio.to_thread(self._seal, tmp)
        final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        await asyncio.to_thread(self._seal, final)
        await asyncio.to_thread(self._fsync_dir, tmp.parent)
        await asyncio.to_thread(self._fsync_dir, final.parent)

    @staticmethod
    def _seal(path: Path) -> None:
        """Idempotent and safe under concurrent sealers: a seal is an (empty) directory and is never removed."""
        for _ in range(16):  # a late writer may re-create a file between our unlink and mkdir
            try:
                st = path.lstat()
            except FileNotFoundError:
                st = None
            except OSError as exc:
                raise StorageError from exc
            try:
                if st is not None:
                    import stat as _stat

                    if _stat.S_ISDIR(st.st_mode):
                        return  # already sealed (possibly by a concurrent cleaner)
                    path.unlink()
                path.mkdir(mode=0o700)
                return
            except (FileExistsError, FileNotFoundError, IsADirectoryError, PermissionError):
                continue  # somebody else changed it meanwhile: look again
            except OSError as exc:
                raise StorageError from exc
        raise StorageError

    async def writers_released(self, key: str) -> bool:
        final, tmp, lock = self._paths(key)
        if not (tmp.is_dir() and final.is_dir()):  # without the seal the evidence is meaningless
            return False
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return True
        except OSError as exc:
            raise StorageError from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                os.unlink(lock)  # while holding it
            except OSError:
                pass
            return True
        finally:
            os.close(fd)

    async def list_keys(self, prefix: str, *, after: str | None, limit: int) -> Sequence[str]:
        raise NotImplementedError


def _is_regular(fd: int) -> bool:
    import stat as _stat

    return _stat.S_ISREG(os.fstat(fd).st_mode)


_ = errno
