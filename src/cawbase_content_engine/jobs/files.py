"""Files: streams, opaque external references, five-step consistency with the storage, verified downloads."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ..errors import (
    ContentMismatchError,
    EngineError,
    FileNotUsableError,
    IdempotencyConflictError,
    LimitExceededError,
    PayloadTooLargeError,
    ResourceNotFoundError,
    ScopeDisabledError,
    StorageNotConfiguredError,
    StorageUnavailableError,
    StoredObjectUnavailableError,
    UploadAbortedError,
    UploadInProgressError,
    ValidationFailedError,
)
from ..models import UpsertResult
from ..operations.common import insert_ignore
from . import db as jdb
from .ident import validation_identity
from .models import StoredFile
from .setup import (
    ExternalSourceResolver,
    JobsLimits,
    ManagedStorage,
    ObjectMissing,
    Retention,
    StorageError,
    StorageKeyExists,
    StorageLimitExceeded,
)
from .spool import Spool, SpooledStream
from .storage import new_key

if TYPE_CHECKING:
    from ..engine import ContentEngine

_IDEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FILE_ID = re.compile(r"^fil_[0-9a-f]{32}$")


def check_idem_key(value: Any) -> str:
    if not isinstance(value, str) or not _IDEM.match(value):
        raise ValidationFailedError("Invalid idempotency key.", field="idempotency_key")
    return value


def check_file_id(value: Any) -> str:
    if not isinstance(value, str) or not _FILE_ID.match(value):
        raise ValidationFailedError("Invalid file_id.", field="file_id")
    return value


def new_file_id() -> str:
    return "fil_" + uuid.uuid4().hex


class Ctx:
    """What every A05 operation needs from the engine (resolved once)."""

    def __init__(self, engine: ContentEngine) -> None:
        self.e = engine
        self.t = engine.tables
        self.js = engine.jobs_setup
        self.lim: JobsLimits = self.js.limits
        self.ret: Retention = self.js.retention
        self.backend = engine.backend

    @property
    def storage(self) -> ManagedStorage:
        s = self.e.storage
        if s is None:
            raise StorageNotConfiguredError()
        return s.managed

    @property
    def factor(self) -> int:
        return int(self.storage.descriptor.transient_overhead_factor)

    @property
    def backend_id(self) -> str:
        return self.storage.descriptor.backend_id

    async def run(self, fn: Any, *, write: bool = True, tx: Any = None) -> Any:
        return await self.e._run(fn, write=write, tx=tx)


_spools: dict[int, Spool] = {}


def spool_for(ctx: Ctx) -> Spool:
    setup = ctx.e.storage
    assert setup is not None
    key = id(ctx.e)
    sp = _spools.get(key)
    if sp is None:
        root = setup.spool_dir or str(getattr(setup.managed, "root", None) or "/tmp")  # noqa: S108
        sp = _spools[key] = Spool(root, ctx.lim.max_spool_bytes)
        sp.start()
    return sp


async def active_scope(conn: AsyncConnection, t: Any, scope_id: str, *, exclusive: bool = False) -> None:
    row = await jdb.lock_scope(conn, t, scope_id, exclusive=exclusive)
    if row.status != "active":
        raise ScopeDisabledError()


# ---- row <-> model -----------------------------------------------------------------------------
async def file_model(conn: AsyncConnection, ctx: Ctx, row: Any, *, now: datetime) -> StoredFile:
    m = row._mapping
    t = ctx.t
    vals: list[dict[str, Any]] = []
    dv = "pending"
    if m["sha256"] is not None:
        vr = (
            await conn.execute(
                sa.select(t.file_validations).where(
                    t.file_validations.c.scope_id == m["scope_id"], t.file_validations.c.file_id == m["file_id"]
                )
            )
        ).all()
        cur = validation_identity(bytes(m["sha256"]).hex(), ctx.lim)
        for v in vr:
            vals.append(
                {
                    "validation_identity": v.validation_identity,
                    "result": v.result,
                    "reason": v.reason,
                    "validated_at": v.validated_at,
                }
            )
            if v.validation_identity == cur:
                dv = v.result
    return StoredFile(
        scope_id=m["scope_id"],
        file_id=m["file_id"],
        origin=m["origin"],
        state=m["state"],
        integrity_failure=m["integrity_failure"],
        size_bytes=m["size_bytes"],
        sha256=bytes(m["sha256"]).hex() if m["sha256"] is not None else None,
        detected_format=m["detected_format"],
        received_at=m["received_at"],
        expires_at=m["expires_at"],
        created_at=m["created_at"],
        download_validation=dv,
        validations=tuple(vals),
    )


async def get_file_row(conn: AsyncConnection, t: Any, scope_id: str, file_id: str, *, lock: bool = False) -> Any:
    q = sa.select(t.stored_files).where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == file_id)
    return (await conn.execute(q.with_for_update() if lock else q)).first()


# ---- reservation / finalization / abandonment (steps 1 and 4) ----------------------------------
async def reserve_file(
    conn: AsyncConnection,
    ctx: Ctx,
    now: datetime,
    *,
    scope_id: str,
    file_id: str,
    origin: str,
    managed_bytes: int,
    generation: int = 1,
    deadline_s: float | None = None,
    link_deadline_s: float | None = None,
    link_job_id: str | None = None,
    resolver_id: str | None = None,
    external_ref: str | None = None,
) -> str | None:
    """Usage rows must be locked. Returns the storage key (None for a non-managed registration)."""
    t, lim = ctx.t, ctx.lim
    s, e = await jdb.lock_usage(conn, t, scope_id, now)
    if s.live_files + 1 > lim.max_live_files_per_scope:
        from ..errors import QuotaExceededError

        raise QuotaExceededError("Live files quota exceeded.")
    key: str | None = None
    charge = 0
    if managed_bytes > 0:
        charge = managed_bytes * ctx.factor
        jdb.check_storage(s, e, charge, lim)
        key = new_key()
    await conn.execute(
        sa.insert(t.stored_files).values(
            scope_id=scope_id,
            file_id=file_id,
            origin=origin,
            state="reserved",
            storage_backend_id=ctx.backend_id if key else None,
            storage_key=key,
            write_state="unknown" if key else None,
            resolver_id=resolver_id,
            external_ref=external_ref,
            reserved_bytes=charge,
            upload_generation=generation,
            upload_deadline_at=jdb.plus(now, deadline_s) if deadline_s else None,
            link_job_id=link_job_id,
            link_deadline_at=jdb.plus(now, link_deadline_s) if link_deadline_s else None,
            reserved_at=now,
            created_at=now,
        )
    )
    await jdb.bump(conn, t, scope_id, now, reserved_bytes=charge, open_reservations=1 if key else 0, live_files=1)
    return key


async def finalize_file(
    conn: AsyncConnection,
    ctx: Ctx,
    *,
    scope_id: str,
    file_id: str,
    generation: int,
    size: int | None,
    sha256: bytes | None,
    external_version: str | None = None,
) -> bool:
    """Step 4: only the current owner (same generation, still ``reserved``, deadline not passed) may register."""
    t = ctx.t
    t0 = await jdb.db_now(conn, ctx.backend)  # bookkeeping only
    await active_scope(conn, t, scope_id)
    await jdb.lock_usage(conn, t, scope_id, t0)
    row = await get_file_row(conn, t, scope_id, file_id, lock=True)
    now = await jdb.db_now(conn, ctx.backend)  # decision time (upload deadline): after every lock wait
    if row is None or row.state != "reserved" or row.upload_generation != generation:
        return False
    if row.upload_deadline_at is not None and row.upload_deadline_at <= now:
        return False
    await conn.execute(
        sa.update(t.stored_files)
        .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == file_id)
        .values(
            state="stored",
            write_state="completed" if row.storage_key else None,
            size_bytes=size,
            sha256=sha256,
            external_version=external_version,
            received_at=now,
            retention_s=ctx.ret.originals_s,
            expires_at=jdb.plus(now, ctx.ret.originals_s),
            upload_deadline_at=None,
        )
    )
    if row.storage_key:
        await jdb.bump(
            conn,
            t,
            scope_id,
            now,
            reserved_bytes=-int(row.reserved_bytes),
            stored_bytes=int(size or 0),
            open_reservations=-1,
        )
    return True


async def abandon_file(ctx: Ctx, *, scope_id: str, file_id: str, generation: int) -> None:
    """A failed or interrupted write: the object may still exist, so the charge becomes *uncertain* (never freed)."""

    async def fn(conn: AsyncConnection) -> None:
        t = ctx.t
        now = await jdb.db_now(conn, ctx.backend)
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, scope_id, now)
        row = await get_file_row(conn, t, scope_id, file_id, lock=True)
        if row is not None and row.state == "reserved" and row.upload_generation == generation:
            await jdb.retire_object(
                conn, t, now, ctx.ret, t.stored_files, row, "abandoned", delay_s=ctx.ret.temporary_s
            )

    await ctx.run(fn)


# ---- streaming helpers --------------------------------------------------------------------------
class _Counting:
    """Wraps a byte stream: bounds the real byte count and the idle time (the declared size is never trusted)."""

    def __init__(
        self, source: AsyncIterable[bytes], limit: int, idle_s: float, total_s: float, *, mismatch: bool
    ) -> None:
        self._it = source.__aiter__()
        self.size, self.limit, self.idle_s, self.total_s, self.mismatch = 0, limit, idle_s, total_s, mismatch
        self.sha = hashlib.sha256()
        self._started = asyncio.get_running_loop().time()

    def __aiter__(self) -> _Counting:
        return self

    async def __anext__(self) -> bytes:
        try:
            async with asyncio.timeout(
                min(self.idle_s, max(0.001, self.total_s - (asyncio.get_running_loop().time() - self._started)))
            ):
                chunk = await self._it.__anext__()
        except StopAsyncIteration:
            raise
        except TimeoutError:
            raise UploadAbortedError() from None
        except EngineError:
            raise
        except Exception:  # the source stream failed (client gone, resolver error): the transfer is aborted
            raise UploadAbortedError() from None
        self.size += len(chunk)
        if self.size > self.limit:
            raise (ContentMismatchError() if self.mismatch else PayloadTooLargeError())
        self.sha.update(chunk)
        return chunk


def _check_digest(value: Any) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationFailedError("Invalid expected_sha256.", field="expected_sha256")
    return bytes.fromhex(value)


# ---- upload --------------------------------------------------------------------------------------
async def upload_file(
    engine: ContentEngine,
    scope_id: str,
    chunks: AsyncIterable[bytes],
    *,
    idempotency_key: str,
    expected_sha256: str | None = None,
    declared_size: int | None = None,
    actor: str | None = None,
) -> UpsertResult[StoredFile]:
    ctx = Ctx(engine)
    ctx.storage  # noqa: B018  (raises storage_not_configured)
    key = check_idem_key(idempotency_key)
    expected = _check_digest(expected_sha256)
    lim = ctx.lim
    if declared_size is not None:
        if isinstance(declared_size, bool) or not isinstance(declared_size, int) or declared_size < 0:
            raise ValidationFailedError("Invalid declared_size.", field="declared_size")
        if declared_size > lim.max_file_bytes:
            raise PayloadTooLargeError()
    bound = declared_size if declared_size is not None else lim.max_file_bytes
    kh = jdb.sha256_hex(key)
    plan = await ctx.run(
        lambda c: _plan_upload(c, ctx, scope_id, kh, bound, "file_upload", "managed_upload", None, None)
    )
    kind = plan["kind"]
    if kind == "replay_view":  # a terminal file is shown as it is
        return UpsertResult(value=plan["file"], created=False)
    counter = _Counting(
        chunks,
        bound,
        lim.upload_idle_timeout_s,
        lim.upload_timeout_s,
        mismatch=declared_size is not None,
    )
    if kind == "replay_check":  # hash only: nothing is written, no quota is taken
        async for _ in counter:
            pass
        row_sha, row_size = plan["sha256"], plan["size"]
        if counter.sha.digest() != row_sha or counter.size != row_size:
            raise IdempotencyConflictError()
        return UpsertResult(value=plan["file"], created=False)
    file_id, gen, skey = plan["file_id"], plan["generation"], plan["key"]
    abort_after = plan["deadline"]
    try:
        receipt = await ctx.storage.put_new(skey, counter, max_bytes=bound, abort_after=abort_after)
        if declared_size is not None and receipt.size != declared_size:
            raise ContentMismatchError()
        if expected is not None and receipt.sha256 != expected:
            raise ContentMismatchError()
        if receipt.size != counter.size or receipt.sha256 != counter.sha.digest():
            raise StoredObjectUnavailableError()
        info = await ctx.storage.stat(skey)
        if info is None or info.size != receipt.size:
            raise StoredObjectUnavailableError()
    except StorageKeyExists:
        await abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=gen)
        raise StorageUnavailableError() from None
    except StorageLimitExceeded:
        await abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=gen)
        raise (ContentMismatchError() if declared_size is not None else PayloadTooLargeError()) from None
    except StorageError:
        await abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=gen)
        raise StorageUnavailableError() from None
    except BaseException:
        await asyncio.shield(abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=gen))
        raise

    async def fin(conn: AsyncConnection) -> bool:
        ok = await finalize_file(
            conn=conn,
            ctx=ctx,
            scope_id=scope_id,
            file_id=file_id,
            generation=gen,
            size=receipt.size,
            sha256=receipt.sha256,
        )
        if ok:
            await conn.execute(
                sa.update(ctx.t.idempotency_records)
                .where(
                    ctx.t.idempotency_records.c.scope_id == scope_id,
                    ctx.t.idempotency_records.c.operation == "file_upload",
                    ctx.t.idempotency_records.c.key_hash == kh,
                    ctx.t.idempotency_records.c.upload_generation == gen,
                )
                .values(request_fingerprint=jdb.sha256_hex(str(receipt.size), receipt.sha256.hex()))
            )
        return bool(ok)

    if not await ctx.run(fin):
        raise UploadAbortedError()

    async def read(conn: AsyncConnection) -> StoredFile:
        now = await jdb.db_now(conn, ctx.backend)
        return await file_model(conn, ctx, await get_file_row(conn, ctx.t, scope_id, file_id), now=now)

    return UpsertResult(value=await ctx.run(read, write=False), created=True)


async def _plan_upload(
    conn: AsyncConnection,
    ctx: Ctx,
    scope_id: str,
    key_hash: str,
    bound: int,
    operation: str,
    origin: str,
    resolver_id: str | None,
    external_ref: str | None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Transaction 1 of an upload / external registration: idempotency record + reservation (levels 1,3,4,5,6)."""
    t, lim = ctx.t, ctx.lim
    await active_scope(conn, t, scope_id)
    now = await jdb.db_now(conn, ctx.backend)
    await jdb.lock_usage(conn, t, scope_id, now)
    file_id = new_file_id()
    inserted = await insert_ignore(
        conn,
        t,
        t.idempotency_records,
        {
            "scope_id": scope_id,
            "operation": operation,
            "key_hash": key_hash,
            "request_fingerprint": fingerprint,
            "file_id": file_id,
            "upload_generation": 1,
            "created_at": now,
        },
    )
    generation = 1
    if not inserted:
        rec = (
            await conn.execute(
                sa.select(t.idempotency_records)
                .where(
                    t.idempotency_records.c.scope_id == scope_id,
                    t.idempotency_records.c.operation == operation,
                    t.idempotency_records.c.key_hash == key_hash,
                )
                .with_for_update()
            )
        ).one()
        if fingerprint is not None and rec.request_fingerprint is not None and rec.request_fingerprint != fingerprint:
            raise IdempotencyConflictError()
        row = await get_file_row(conn, t, scope_id, rec.file_id, lock=True)
        now = await jdb.db_now(conn, ctx.backend)  # decision time (upload deadline): after every lock wait
        if row is None:  # purged metadata with a surviving record: start again under the same key
            generation = rec.upload_generation + 1
        elif row.state == "stored":
            model = await file_model(conn, ctx, row, now=now)
            if operation == "external_file":
                return {"kind": "replay_view", "file": model}
            return {"kind": "replay_check", "file": model, "sha256": bytes(row.sha256), "size": row.size_bytes}
        elif row.state == "reserved":
            if row.upload_deadline_at is not None and row.upload_deadline_at > now:
                raise UploadInProgressError()
            generation = rec.upload_generation + 1
            await jdb.retire_object(
                conn, t, now, ctx.ret, t.stored_files, row, "abandoned", delay_s=ctx.ret.temporary_s
            )
        elif row.state == "abandoned":
            generation = rec.upload_generation + 1
        else:  # deleted / expired / lost: shown as is, nothing new is created while the record exists
            return {"kind": "replay_view", "file": await file_model(conn, ctx, row, now=now)}
        await conn.execute(
            sa.update(t.idempotency_records)
            .where(
                t.idempotency_records.c.scope_id == scope_id,
                t.idempotency_records.c.operation == operation,
                t.idempotency_records.c.key_hash == key_hash,
            )
            .values(file_id=file_id, upload_generation=generation)
        )
    managed = bound if origin in ("managed_upload", "external_materialized") else 0
    skey = await reserve_file(
        conn,
        ctx,
        now,
        scope_id=scope_id,
        file_id=file_id,
        origin=origin,
        managed_bytes=managed,
        generation=generation,
        deadline_s=lim.upload_timeout_s,
        resolver_id=resolver_id,
        external_ref=external_ref,
    )
    return {
        "kind": "new",
        "file_id": file_id,
        "generation": generation,
        "key": skey,
        "deadline": jdb.plus(now, lim.upload_timeout_s),
    }


# ---- external references (Python only) -----------------------------------------------------------
async def register_external_file(
    engine: ContentEngine, scope_id: str, *, resolver_id: str, ref: str, idempotency_key: str
) -> UpsertResult[StoredFile]:
    ctx = Ctx(engine)
    ctx.storage  # noqa: B018
    setup = engine.storage
    assert setup is not None
    key = check_idem_key(idempotency_key)
    if not isinstance(resolver_id, str) or resolver_id not in setup.external_resolvers:
        raise ValidationFailedError("Unknown resolver.", field="resolver_id")
    if not isinstance(ref, str) or not ref or len(ref.encode("utf-8")) > 1024:
        raise ValidationFailedError("Invalid reference.", field="ref")
    resolver: ExternalSourceResolver = setup.external_resolvers[resolver_id]
    stable = resolver.descriptor.stable_versions
    origin = "external_reference" if stable else "external_materialized"
    lim = ctx.lim
    kh = jdb.sha256_hex(key)
    fp = jdb.sha256_hex(resolver_id, jdb.sha256_hex(ref))
    plan = await ctx.run(
        lambda c: _plan_upload(c, ctx, scope_id, kh, lim.max_file_bytes, "external_file", origin, resolver_id, ref, fp)
    )
    if plan["kind"] != "new":  # replay: describe is NOT called again (the version stays pinned)
        return UpsertResult(value=plan["file"], created=False)
    file_id, gen, skey = plan["file_id"], plan["generation"], plan["key"]
    try:
        try:
            info = await resolver.describe(ref)
        except Exception:
            raise StorageUnavailableError() from None
        if info.size > lim.max_file_bytes:
            raise PayloadTooLargeError()
        counter = _Counting(
            resolver.open_read(ref, info.version_id),
            lim.max_file_bytes,
            lim.upload_idle_timeout_s,
            lim.upload_timeout_s,
            mismatch=False,
        )
        if stable:
            try:
                async for _ in counter:
                    pass
            except (LimitExceededError, UploadAbortedError):
                raise
            except Exception:
                raise StorageUnavailableError() from None
            size, digest = counter.size, counter.sha.digest()
        else:
            assert skey is not None
            try:
                receipt = await ctx.storage.put_new(
                    skey, counter, max_bytes=lim.max_file_bytes, abort_after=plan["deadline"]
                )
            except StorageLimitExceeded:
                raise PayloadTooLargeError() from None
            except StorageError:
                raise StorageUnavailableError() from None
            size, digest = receipt.size, receipt.sha256
    except BaseException:
        await asyncio.shield(abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=gen))
        raise

    async def fin(conn: AsyncConnection) -> bool:
        return await finalize_file(
            conn=conn,
            ctx=ctx,
            scope_id=scope_id,
            file_id=file_id,
            generation=gen,
            size=size,
            sha256=digest,
            external_version=info.version_id,
        )

    if not await ctx.run(fin):
        raise UploadAbortedError()

    async def read(conn: AsyncConnection) -> StoredFile:
        now = await jdb.db_now(conn, ctx.backend)
        return await file_model(conn, ctx, await get_file_row(conn, ctx.t, scope_id, file_id), now=now)

    return UpsertResult(value=await ctx.run(read, write=False), created=True)


# ---- internal text objects (text_analysis input and auxiliary context) ----------------------------
async def store_text_object(ctx: Ctx, scope_id: str, data: bytes) -> str:
    """Stores a small ``text_input`` object through the same five-step protocol, unlinked until accepted."""
    lim = ctx.lim
    file_id = new_file_id()
    box: dict[str, Any] = {}

    async def reserve(conn: AsyncConnection) -> None:
        await active_scope(conn, ctx.t, scope_id)
        now = await jdb.db_now(conn, ctx.backend)
        box["key"] = await reserve_file(
            conn,
            ctx,
            now,
            scope_id=scope_id,
            file_id=file_id,
            origin="text_input",
            managed_bytes=max(1, len(data)),
            deadline_s=lim.upload_timeout_s,
            link_deadline_s=lim.submit_link_timeout_s,
        )
        box["deadline"] = jdb.plus(now, lim.upload_timeout_s)

    await ctx.run(reserve)

    async def one() -> AsyncIterator[bytes]:
        yield data

    try:
        receipt = await ctx.storage.put_new(box["key"], one(), max_bytes=max(1, len(data)), abort_after=box["deadline"])
    except StorageError:
        await abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=1)
        raise StorageUnavailableError() from None
    except BaseException:
        await asyncio.shield(abandon_file(ctx, scope_id=scope_id, file_id=file_id, generation=1))
        raise

    async def fin(conn: AsyncConnection) -> bool:
        return await finalize_file(
            conn=conn,
            ctx=ctx,
            scope_id=scope_id,
            file_id=file_id,
            generation=1,
            size=receipt.size,
            sha256=receipt.sha256,
        )

    if not await ctx.run(fin):
        raise UploadAbortedError()
    return file_id


async def read_verified(ctx: Ctx, row: Any, *, limit: int | None = None) -> bytes:
    """Reads a whole managed or external object verifying size and hash (used for text inputs and parsers)."""
    chunks: list[bytes] = []
    async for chunk in open_verified(ctx, row):
        chunks.append(chunk)
        if limit is not None and sum(map(len, chunks)) > limit:
            raise PayloadTooLargeError()
    return b"".join(chunks)


async def open_verified(ctx: Ctx, row: Any) -> AsyncIterator[bytes]:
    """Yields the object's bytes; hash and size are checked before the last chunk is released to the caller of
    ``read_verified`` (callers that stream to a client must use the spool, which verifies *before* sending)."""
    m = row._mapping
    digest, size = hashlib.sha256(), 0
    try:
        if m["storage_key"] is not None:
            source = ctx.storage.open_read(m["storage_key"])
        else:
            setup = ctx.e.storage
            assert setup is not None
            resolver = setup.external_resolvers.get(m["resolver_id"])
            if resolver is None:
                raise StoredObjectUnavailableError()
            source = resolver.open_read(m["external_ref"], m["external_version"])
        async for chunk in source:
            size += len(chunk)
            digest.update(chunk)
            yield chunk
    except ObjectMissing:
        raise _Lost("missing") from None
    except StorageError:
        raise StorageUnavailableError() from None
    except (StorageUnavailableError, StoredObjectUnavailableError, _Lost):
        raise
    except Exception:
        raise StorageUnavailableError() from None
    if size != m["size_bytes"] or digest.digest() != bytes(m["sha256"]):
        raise _Lost("hash_mismatch")


class _Lost(Exception):
    def __init__(self, why: str) -> None:
        self.why = why


async def mark_lost(ctx: Ctx, scope_id: str, file_id: str, why: str) -> None:
    async def fn(conn: AsyncConnection) -> None:
        t = ctx.t
        now = await jdb.db_now(conn, ctx.backend)
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, scope_id, now)
        row = await get_file_row(conn, t, scope_id, file_id, lock=True)
        if row is not None and row.state == "stored":
            await jdb.retire_object(conn, t, now, ctx.ret, t.stored_files, row, "lost", delay_s=ctx.lim.purge_grace_s)
            await conn.execute(
                sa.update(t.stored_files)
                .where(t.stored_files.c.scope_id == scope_id, t.stored_files.c.file_id == file_id)
                .values(integrity_failure=why)
            )

    await ctx.run(fn)


# ---- download -------------------------------------------------------------------------------------
async def open_file_content(engine: ContentEngine, scope_id: str, file_id: str) -> SpooledStream:
    """Verified BEFORE the first byte is returned: the object is read into the private spool and checked."""
    ctx = Ctx(engine)
    ctx.storage  # noqa: B018
    check_file_id(file_id)

    async def read(conn: AsyncConnection) -> Any:
        now = await jdb.db_now(conn, ctx.backend)
        row = await get_file_row(conn, ctx.t, scope_id, file_id)
        if (
            row is None
            or row.origin == "text_input"
            or row.state != "stored"
            or (row.expires_at is not None and row.expires_at <= now)
        ):
            raise ResourceNotFoundError()
        model = await file_model(conn, ctx, row, now=now)
        if model.download_validation != "passed":
            raise ResourceNotFoundError()
        return row

    row = await ctx.run(read, write=False)
    spool = spool_for(ctx)
    size = int(row.size_bytes)
    spool.reserve(size)
    path = spool.new_path()
    try:
        with open(path, "xb") as fh:  # noqa: ASYNC230
            try:
                async for chunk in open_verified(ctx, row):
                    await asyncio.to_thread(fh.write, chunk)
            except _Lost as lost:
                await mark_lost(ctx, scope_id, file_id, lost.why)
                raise StoredObjectUnavailableError() from None
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        spool.release(size)
        raise
    return SpooledStream(spool, path, size)


# ---- reads and deletion ---------------------------------------------------------------------------
async def get_file(engine: ContentEngine, scope_id: str, file_id: str, *, tx: Any = None) -> StoredFile:
    ctx = Ctx(engine)
    check_file_id(file_id)

    async def fn(conn: AsyncConnection) -> StoredFile:
        now = await jdb.db_now(conn, ctx.backend)
        row = await get_file_row(conn, ctx.t, scope_id, file_id)
        if row is None or row.origin == "text_input":
            raise ResourceNotFoundError()
        return await file_model(conn, ctx, row, now=now)

    return await ctx.run(fn, write=False, tx=tx)  # type: ignore[no-any-return]


async def list_files(
    engine: ContentEngine, scope_id: str, *, state: str | None, limit: int, cursor: str | None, tx: Any = None
) -> tuple[list[StoredFile], str | None]:
    ctx = Ctx(engine)
    f = ctx.t.stored_files

    async def fn(conn: AsyncConnection) -> tuple[list[StoredFile], str | None]:
        now = await jdb.db_now(conn, ctx.backend)
        q = sa.select(f).where(f.c.scope_id == scope_id, f.c.origin != "text_input")
        if state:
            q = q.where(f.c.state == state)
        if cursor:
            q = q.where(f.c.file_id > cursor)
        rows = (await conn.execute(q.order_by(f.c.file_id).limit(limit + 1))).all()
        items = [await file_model(conn, ctx, r, now=now) for r in rows[:limit]]
        return items, (rows[limit - 1].file_id if len(rows) > limit else None)

    return await ctx.run(fn, write=False, tx=tx)  # type: ignore[no-any-return]


async def delete_file(engine: ContentEngine, scope_id: str, file_id: str, *, tx: Any = None) -> StoredFile:
    """Logical deletion (``data:delete``). Order 1 -> 3 -> 4 -> 6; serializes with publication (6 share vs 6 update)."""
    ctx = Ctx(engine)
    check_file_id(file_id)

    async def fn(conn: AsyncConnection) -> StoredFile:
        t = ctx.t
        now = await jdb.db_now(conn, ctx.backend)
        await jdb.lock_scope(conn, t, scope_id, exclusive=False)
        await jdb.lock_usage(conn, t, scope_id, now)
        row = await get_file_row(conn, t, scope_id, file_id, lock=True)
        if row is None or row.origin == "text_input":
            raise ResourceNotFoundError()
        if row.state == "stored":
            await jdb.retire_object(
                conn, t, now, ctx.ret, t.stored_files, row, "deleted", delay_s=ctx.lim.purge_grace_s
            )
            row = await get_file_row(conn, t, scope_id, file_id)
        elif row.state == "reserved":
            raise FileNotUsableError()
        return await file_model(conn, ctx, row, now=now)

    return await ctx.run(fn, tx=tx)  # type: ignore[no-any-return]
