"""Operator-local bootstrap and administrative recovery (D-26). No HTTP surface, no API key.

Both are serialized through one conditional UPDATE on the ``engine_state`` row, which takes the
row lock on PostgreSQL and the write lock on SQLite.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from .engine import ContentEngine
from .errors import (
    BootstrapNotAllowedError,
    EngineError,
    InternalError,
    InvalidConfigurationError,
    RecoveryNotAllowedError,
    SecretDeliveryError,
    ValidationFailedError,
)
from .models import ApiKeyGrants, IssuedApiKey
from .operations import api_keys as ak
from .operations import clients as cl
from .operations.common import now
from .permissions import ALL_ADMIN_PERMISSIONS

INITIAL_CLIENT_NAME = "initial-admin"


class SecretFileSink:
    """Exclusive (``O_EXCL``), ``0600`` file for the one-time secret.

    ``prepare`` creates the file *before* the database transaction so an unusable path fails
    without issuing anything; ``abort`` removes it if the transaction does not commit.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    def prepare(self) -> None:
        try:
            self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise InvalidConfigurationError("The secret file could not be created exclusively.") from None

    def deliver(self, secret: str) -> None:
        assert self._fd is not None
        fd, self._fd = self._fd, None
        try:
            os.write(fd, (secret + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def abort(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                self._path.unlink()
            except OSError:
                pass


async def _run(engine: ContentEngine, label: str, sink: SecretFileSink | None, initial: bool) -> IssuedApiKey:
    lbl = label.strip()
    if not lbl or len(lbl) > engine.config.limits.max_label:
        raise ValidationFailedError("Invalid label.", field="label")
    t = engine.tables
    if sink is not None:
        sink.prepare()

    async def fn(c: AsyncConnection) -> IssuedApiKey:
        ts = now()
        if initial:
            res = await c.execute(
                sa.update(t.engine_state)
                .where(t.engine_state.c.id == 1, t.engine_state.c.admin_initialized_at.is_(None))
                .values(admin_initialized_at=ts, admin_bootstrap_generation=1, updated_at=ts)
            )
            if res.rowcount != 1:
                raise BootstrapNotAllowedError()
            created = await cl.create_client(
                c,
                t,
                name=INITIAL_CLIENT_NAME,
                display_name="Initial administrator",
                actor_key_id=None,
                actor_label="operator:bootstrap",
            )
            if not created.created:
                raise BootstrapNotAllowedError()
            client_id, generation = created.value.client_id, 1
            action = "bootstrap.admin_key_issued"
        else:
            res = await c.execute(
                sa.update(t.engine_state)
                .where(t.engine_state.c.id == 1, t.engine_state.c.admin_initialized_at.is_not(None))
                .values(admin_bootstrap_generation=t.engine_state.c.admin_bootstrap_generation + 1, updated_at=ts)
            )
            if res.rowcount != 1:
                raise RecoveryNotAllowedError()
            generation = (await c.execute(sa.select(t.engine_state.c.admin_bootstrap_generation))).scalar_one()
            for _ in range(3):  # always a brand-new client; never reuse or re-enable one
                name = f"recovery-{datetime.now(UTC):%Y%m%d}-{secrets.token_hex(4)}"
                created = await cl.create_client(
                    c,
                    t,
                    name=name,
                    display_name="Recovery administrator",
                    actor_key_id=None,
                    actor_label="operator:recovery",
                )
                if created.created:
                    break
            else:
                raise InternalError()
            client_id = created.value.client_id
            action = "admin.recovery_key_issued"
        return await ak.issue_key(
            c,
            t,
            engine.config.limits,
            client_id=client_id,
            request_id=ak.new_request_id(),
            label=lbl,
            expires_at=None,
            grants=ApiKeyGrants(admin=ALL_ADMIN_PERMISSIONS, scopes={}),
            created_by_key_id=None,
            actor_label="operator:bootstrap" if initial else "operator:recovery",
            event_action=action,
            skip_active_limit=True,
            extra_detail={"generation": int(generation)},
        )

    try:
        issued = await engine._run(fn, write=True, tx=None)
    except BaseException:
        if sink is not None:
            sink.abort()
        raise
    if sink is not None:
        assert issued.secret is not None
        delivered = True
        try:
            sink.deliver(issued.secret.get_secret_value())
        except Exception:
            delivered = False
        if not delivered:
            # Raised outside the handler on purpose: no implicit __context__ that could carry the
            # secret inside a driver/OS error message into tracebacks.
            raise SecretDeliveryError(issued.info.key_id)
    return issued


async def create_initial_admin_key(
    engine: ContentEngine, *, label: str = "bootstrap", sink: SecretFileSink | None = None
) -> IssuedApiKey:
    """First administrator. Only while the installation was never initialized (persistent mark)."""
    return await _run(engine, label, sink, initial=True)


async def recover_admin_access(
    engine: ContentEngine, *, confirm: bool, label: str = "recovery", sink: SecretFileSink | None = None
) -> IssuedApiKey:
    """Issue a new full-admin credential in a new client. Revokes and disables nothing."""
    if confirm is not True:
        raise ValidationFailedError("Administrative recovery requires explicit confirmation.", field="confirm")
    return await _run(engine, label, sink, initial=False)


__all__ = ["EngineError", "SecretFileSink", "create_initial_admin_key", "recover_admin_access"]
