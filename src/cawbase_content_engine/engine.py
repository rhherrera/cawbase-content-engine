"""``ContentEngine``: explicit lifecycle, ownership and operation facades."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from . import validation as v
from ._version import __version__
from .config import EngineConfig
from .constants import RUNTIME_LEVEL, SCHEMA_LEVEL
from .contexts import AdminContext, AuthenticatedPrincipal, ScopeContext
from .errors import (
    DatabaseUnavailableError,
    EngineError,
    EngineStateError,
    InvalidConfigurationError,
    PermissionDeniedError,
    SchemaIncompatibleError,
    SchemaMissingError,
    ValidationFailedError,
)
from .models import (
    ApiKeyGrants,
    ApiKeyInfo,
    Client,
    Diagnostics,
    IssuedApiKey,
    LivenessReport,
    Page,
    ReadinessReport,
    SchemaStatus,
    Scope,
    UpsertResult,
)
from .operations import api_keys as ak
from .operations import clients as cl
from .operations import scopes as sc
from .operations.analysis import AnalysisOperations
from .operations.common import page_size
from .operations.profile_facade import ProfileOperations
from .permissions import AdminPermission, ScopePermission
from .persistence.dialect import Backend
from .persistence.engines import create_owned_engine
from .persistence.sqlite_support import driver_in_transaction
from .persistence.tables import Tables, tables_for
from .schema import read_schema_status
from .transactions import HostTransaction, run_in_host, run_own, translate

if TYPE_CHECKING:
    from .inference.adapter import InferenceSetup, InferenceStatus

T = TypeVar("T")

#: Floor required by the features used (JSON, RETURNING, ON CONFLICT). Only the version linked by the
#: test platform is exercised; the floor itself is not covered by a test run (see the report).
MIN_SQLITE_VERSION = (3, 38, 0)


class ContentEngine:
    """Construct (no I/O) -> ``await start()`` -> operations -> ``await aclose()``."""

    def __init__(self, config: EngineConfig, *, inference: InferenceSetup | None = None) -> None:
        if inference is not None:
            from .inference.adapter import InferenceSetup as _Setup

            if not isinstance(inference, _Setup):
                raise InvalidConfigurationError("'inference' must be an InferenceSetup.")
        self._inference = inference  # the adapter belongs to the host: never started or closed here
        self._config = config
        self._backend = config.backend
        self._tables: Tables = tables_for(self._backend)
        self._engine: AsyncEngine | None = config.async_engine
        self._token = object()
        self._started = False
        self._closed = False

    # ---- lifecycle -------------------------------------------------------------------------
    @property
    def config(self) -> EngineConfig:
        return self._config

    @property
    def backend(self) -> Backend:
        return self._backend

    @property
    def inference(self) -> InferenceSetup | None:
        return self._inference

    async def inference_status(self) -> InferenceStatus:
        """Capability status of the inference adapter (distinct from ``readiness()``, which is engine+database).

        Read-only: never starts anything and never waits for readiness.
        """
        import asyncio

        from .inference.adapter import AdapterState, InferenceStatus

        setup = self._inference
        if setup is None:
            return InferenceStatus(configured=False, state="not_configured")
        try:
            async with asyncio.timeout(min(setup.timeout_s, 5.0)):
                state = await setup.adapter.probe()
            name = AdapterState(state).value
        except Exception:  # timeout, transport failure or a value outside the closed set
            name = "unknown"
        return InferenceStatus(configured=True, state=name, descriptor=setup.descriptor)

    @property
    def tables(self) -> Tables:
        return self._tables

    async def __aenter__(self) -> ContentEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Opens the pool (URL mode) and verifies the schema. Never migrates."""
        if self._closed:
            raise EngineStateError("The engine is closed.")
        if self._started:
            return
        if self._backend is Backend.SQLITE:
            import sqlite3

            if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
                major, minor = MIN_SQLITE_VERSION[:2]
                raise InvalidConfigurationError(
                    f"SQLite {major}.{minor} or newer is required (this runtime links {sqlite3.sqlite_version})."
                )
        created_here = False
        try:
            if self._engine is None:
                self._engine = create_owned_engine(self._config)
                created_here = True
            await self._verify_engine_preconditions()
            status = await self.schema_status()
            if not status.present:
                raise SchemaMissingError()
            if not status.compatible:
                raise SchemaIncompatibleError()
        except BaseException:
            if created_here and self._engine is not None:
                await self._engine.dispose()
                self._engine = None
            raise
        self._started = True

    async def aclose(self) -> None:
        """Disposes the pool only if the engine created it. An injected engine is never touched."""
        if self._closed:
            return
        self._closed = True
        self._started = False
        if self._config.owns_engine and self._engine is not None:
            await self._engine.dispose()
        self._engine = None

    async def _verify_engine_preconditions(self) -> None:
        assert self._engine is not None
        try:
            async with self._engine.connect() as conn:
                if self._backend is Backend.POSTGRESQL:
                    level = await conn.get_isolation_level()
                    if str(level).upper() != "READ COMMITTED":
                        raise InvalidConfigurationError(
                            "The injected PostgreSQL engine must use READ COMMITTED isolation."
                        )
                else:
                    await conn.begin()
                    ok = await driver_in_transaction(conn)
                    await conn.rollback()
                    if not ok:
                        raise InvalidConfigurationError(
                            "The injected SQLite engine does not emit a real BEGIN; "
                            "call install_transaction_control(engine) before first use."
                        )
        except EngineError:
            raise
        except Exception as exc:
            raise translate(exc) from None

    def _require_started(self) -> AsyncEngine:
        if not self._started or self._closed or self._engine is None:
            raise EngineStateError()
        return self._engine

    async def _run(
        self, fn: Callable[[AsyncConnection], Awaitable[T]], *, write: bool, tx: HostTransaction | None
    ) -> T:
        engine = self._require_started()
        if tx is not None:
            return await run_in_host(self._config.async_engine, self._backend, tx, fn)
        return await run_own(engine, self._backend, fn, write=write)

    # ---- status ----------------------------------------------------------------------------
    async def schema_status(self) -> SchemaStatus:
        if self._engine is None:
            raise EngineStateError()
        return await run_own(self._engine, self._backend, lambda c: read_schema_status(c, self._backend), write=False)

    async def liveness(self) -> LivenessReport:
        return LivenessReport()

    async def readiness(self) -> ReadinessReport:
        if not self._started or self._closed:
            return ReadinessReport(ready=False, state="not_started")
        try:
            status = await self.schema_status()
        except DatabaseUnavailableError:
            return ReadinessReport(ready=False, state="degraded_database")
        except EngineError:
            return ReadinessReport(ready=False, state="degraded_database")
        if status.compatible:
            return ReadinessReport(ready=True, state="ready")
        return ReadinessReport(ready=False, state="schema_incompatible")

    # ---- contexts --------------------------------------------------------------------------
    def trusted_scope_context(
        self, scope_id: str, permissions: Collection[ScopePermission | str], actor: str | None = None
    ) -> ScopeContext:
        """The *host* vouches that it authenticated and authorized the caller for this scope."""
        try:
            perms = frozenset(ScopePermission(p) for p in permissions)
        except ValueError:
            raise ValidationFailedError("Unknown permission.", field="permissions") from None
        return ScopeContext(v.scope_id(scope_id), perms, actor=actor, _token=self._token)

    def trusted_admin_context(
        self,
        permissions: Collection[AdminPermission | str],
        scope_grants: Mapping[str, Collection[ScopePermission | str]] | None = None,
        actor: str | None = None,
    ) -> AdminContext:
        try:
            perms = frozenset(AdminPermission(p) for p in permissions)
            grants = {
                v.scope_id(s): frozenset(ScopePermission(p) for p in ps) for s, ps in (scope_grants or {}).items()
            }
        except ValueError:
            raise ValidationFailedError("Unknown permission.", field="permissions") from None
        return AdminContext(perms, grants, actor=actor, _token=self._token)

    def scoped(self, ctx: ScopeContext, transaction: HostTransaction | None = None) -> ScopedOperations:
        self._check_token(ctx._token)
        return ScopedOperations(self, ctx, transaction)

    def admin(self, ctx: AdminContext, transaction: HostTransaction | None = None) -> AdminOperations:
        self._check_token(ctx._token)
        return AdminOperations(self, ctx, transaction)

    def _check_token(self, token: Any) -> None:
        if token is not self._token:
            raise PermissionDeniedError()

    async def authenticate_api_key(self, raw: str | SecretStr) -> AuthenticatedPrincipal:
        text = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
        return await self._run(lambda c: ak.authenticate(c, self._tables, text, self._token), write=False, tx=None)


class ScopedOperations(ProfileOperations, AnalysisOperations):
    """Operations on one scope. Every query is constrained by ``ctx.scope_id``."""

    def __init__(self, engine: ContentEngine, ctx: ScopeContext, tx: HostTransaction | None) -> None:
        self._e, self._ctx, self._tx = engine, ctx, tx

    def _need(self, perm: ScopePermission) -> None:
        if perm not in self._ctx.permissions:
            raise PermissionDeniedError()

    async def get_scope(self, scope_id: str) -> Scope:
        from .errors import ResourceNotAccessibleError

        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:  # membership is validated, the id grants nothing
            raise ResourceNotAccessibleError()
        self._need(ScopePermission.SCOPE_READ)

        async def fn(c: AsyncConnection) -> Scope:
            row = await sc.get_scope_row(c, self._e.tables, scope_id)
            if row is None:
                raise ResourceNotAccessibleError()
            from .operations.common import scope_from_row

            return scope_from_row(row)

        return await self._e._run(fn, write=False, tx=self._tx)

    async def update_scope(
        self,
        scope_id: str,
        *,
        expected_revision: int,
        display_name: Any = sc.UNSET,
        description: Any = sc.UNSET,
        metadata: Any = sc.UNSET,
    ) -> Scope:
        from .errors import ResourceNotAccessibleError

        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:
            raise ResourceNotAccessibleError()
        self._need(ScopePermission.SCOPE_WRITE)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValidationFailedError("Invalid expected_revision.", field="expected_revision")
        limits = self._e.config.limits
        changes: dict[str, Any] = {}
        if display_name is not sc.UNSET:
            changes["display_name"] = v.text(display_name, "display_name", limits.max_display_name)
        if description is not sc.UNSET:
            changes["description"] = v.text(description, "description", limits.max_description, required=False)
        if metadata is not sc.UNSET:
            changes["metadata"] = v.metadata(metadata, limits)
        if not changes:
            raise ValidationFailedError("No fields to update.")
        return await self._e._run(
            lambda c: sc.update_scope(
                c,
                self._e.tables,
                scope_id=scope_id,
                expected_revision=expected_revision,
                changes=changes,
                actor_key_id=self._ctx.key_id,
                actor_label=self._ctx.actor,
            ),
            write=True,
            tx=self._tx,
        )


class AdminOperations:
    """Global operations; require an ``AdminContext``."""

    def __init__(self, engine: ContentEngine, ctx: AdminContext, tx: HostTransaction | None) -> None:
        self._e, self._ctx, self._tx = engine, ctx, tx

    def _need(self, perm: AdminPermission) -> None:
        if perm not in self._ctx.permissions:
            raise PermissionDeniedError()

    @property
    def _t(self) -> Tables:
        return self._e.tables

    @property
    def _actor(self) -> dict[str, Any]:
        return dict(actor_key_id=self._ctx.key_id, actor_label=self._ctx.actor)

    # scopes
    async def create_scope(
        self,
        scope_id: str,
        display_name: str,
        *,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UpsertResult[Scope]:
        self._need(AdminPermission.ADMIN_SCOPES_WRITE)
        lim = self._e.config.limits
        sid = v.scope_id(scope_id)
        name = v.text(display_name, "display_name", lim.max_display_name)
        desc = v.text(description, "description", lim.max_description, required=False)
        meta = v.metadata(metadata if metadata is not None else {}, lim)
        assert name is not None
        return await self._e._run(
            lambda c: sc.create_scope(
                c, self._t, scope_id=sid, display_name=name, description=desc, metadata=meta, **self._actor
            ),
            write=True,
            tx=self._tx,
        )

    async def get_scope(self, scope_id: str) -> Scope:
        self._need(AdminPermission.ADMIN_SCOPES_READ)
        v.scope_id(scope_id)
        return await self._e._run(lambda c: sc.admin_get_scope(c, self._t, scope_id), write=False, tx=self._tx)

    async def list_scopes(
        self, *, status: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> Page[Scope]:
        self._need(AdminPermission.ADMIN_SCOPES_READ)
        lim = self._e.config.limits
        n = page_size(limit, lim.default_page_size, lim.max_page_size)
        return await self._e._run(
            lambda c: sc.list_scopes(c, self._t, status=status, limit=n, cursor=cursor), write=False, tx=self._tx
        )

    async def set_scope_status(self, scope_id: str, status: str) -> Scope:
        self._need(AdminPermission.ADMIN_SCOPES_WRITE)
        v.scope_id(scope_id)
        if status not in ("active", "disabled"):
            raise ValidationFailedError("Invalid status.", field="status")
        return await self._e._run(
            lambda c: sc.set_scope_status(c, self._t, scope_id=scope_id, status=status, **self._actor),
            write=True,
            tx=self._tx,
        )

    # clients
    async def create_client(self, name: str, display_name: str) -> UpsertResult[Client]:
        self._need(AdminPermission.ADMIN_CLIENTS_WRITE)
        cname = v.client_name(name)
        dname = v.text(display_name, "display_name", self._e.config.limits.max_display_name)
        assert dname is not None
        return await self._e._run(
            lambda c: cl.create_client(c, self._t, name=cname, display_name=dname, **self._actor),
            write=True,
            tx=self._tx,
        )

    async def get_client(self, client_id: str) -> Client:
        self._need(AdminPermission.ADMIN_CLIENTS_READ)
        return await self._e._run(lambda c: cl.get_client(c, self._t, client_id), write=False, tx=self._tx)

    async def list_clients(self, *, limit: int | None = None, cursor: str | None = None) -> Page[Client]:
        self._need(AdminPermission.ADMIN_CLIENTS_READ)
        lim = self._e.config.limits
        n = page_size(limit, lim.default_page_size, lim.max_page_size)
        return await self._e._run(
            lambda c: cl.list_clients(c, self._t, limit=n, cursor=cursor), write=False, tx=self._tx
        )

    async def set_client_status(self, client_id: str, status: str) -> Client:
        self._need(AdminPermission.ADMIN_CLIENTS_WRITE)
        return await self._e._run(
            lambda c: cl.set_client_status(c, self._t, client_id=client_id, status=status, **self._actor),
            write=True,
            tx=self._tx,
        )

    # API keys
    async def issue_api_key(
        self,
        client_id: str,
        *,
        request_id: str,
        grants: ApiKeyGrants | Mapping[str, Any],
        label: str,
        expires_at: datetime | None = None,
    ) -> IssuedApiKey:
        self._need(AdminPermission.ADMIN_CLIENTS_WRITE)
        lim = self._e.config.limits
        if isinstance(grants, ApiKeyGrants):
            g = ak.normalize_grants(grants.admin, grants.scopes, lim)
        else:
            g = ak.normalize_grants(grants.get("admin", ()), grants.get("scopes", {}), lim)
        for sid in g.scopes:
            v.scope_id(sid, "grants")
        rid = v.request_id(request_id)
        lbl = v.text(label, "label", lim.max_label)
        assert lbl is not None
        exp = v.aware_datetime(expires_at, "expires_at")

        async def fn(c: AsyncConnection) -> IssuedApiKey:
            await ak.check_delegation(
                c, self._t, issuer_admin=self._ctx.permissions, issuer_scopes=self._ctx.scope_grants, grants=g
            )
            return await ak.issue_key(
                c,
                self._t,
                lim,
                client_id=client_id,
                request_id=rid,
                label=lbl,
                expires_at=exp,
                grants=g,
                created_by_key_id=self._ctx.key_id,
                actor_label=self._ctx.actor,
            )

        return await self._e._run(fn, write=True, tx=self._tx)

    async def list_api_keys(self, client_id: str) -> list[ApiKeyInfo]:
        self._need(AdminPermission.ADMIN_CLIENTS_READ)
        return await self._e._run(lambda c: ak.list_keys(c, self._t, client_id), write=False, tx=self._tx)

    async def revoke_api_key(self, key_id: str) -> ApiKeyInfo:
        self._need(AdminPermission.ADMIN_CLIENTS_WRITE)
        return await self._e._run(
            lambda c: ak.revoke_key(c, self._t, key_id=key_id, **self._actor), write=True, tx=self._tx
        )

    async def diagnostics(self) -> Diagnostics:
        self._need(AdminPermission.ADMIN_DIAGNOSTICS_READ)
        status = (
            await self._e.schema_status()
            if self._tx is None
            else await self._e._run(lambda c: read_schema_status(c, self._e.backend), write=False, tx=self._tx)
        )
        ready = await self._e.readiness() if self._tx is None else None
        return Diagnostics(
            package_version=__version__,
            backend=self._e.backend.value,
            schema_level=status.schema_level,
            min_runtime_level=status.min_runtime_level,
            required_schema_level=SCHEMA_LEVEL,
            runtime_level=RUNTIME_LEVEL,
            migration_mode=self._e.config.migration_mode,
            state=ready.state if ready else ("ready" if status.compatible else "schema_incompatible"),
        )


__all__ = ["AdminOperations", "ContentEngine", "ScopedOperations"]
