"""A06 methods of ``ScopedOperations`` (authorization, host-transaction rejection, dispatch). Lazy imports only."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any, Literal

from .. import validation as v
from ..contexts import ScopeContext
from ..errors import (
    IncompatibleTransactionError,
    PermissionDeniedError,
    ProtectionNotConfiguredError,
    ResourceNotAccessibleError,
    ValidationFailedError,
)
from ..models import Page
from ..permissions import ScopePermission
from ..transactions import HostTransaction
from .common import page_size

if TYPE_CHECKING:
    from ..engine import ContentEngine
    from ..protection.types import (
        AnalysisSource,
        ContextInfo,
        ProtectedText,
        ProtectionPolicy,
        ProtectionSetup,
        RestoredText,
    )

SP = ScopePermission


class ProtectionOperations:
    _e: ContentEngine
    _ctx: ScopeContext
    _tx: HostTransaction | None

    def _gate_a06(self, scope_id: str, *perms: ScopePermission) -> ProtectionSetup:
        """Membership, permissions, host-transaction rejection (the text must not be delivered before the
        mappings are committed, so the host's transaction is never used), then configuration."""
        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:
            raise ResourceNotAccessibleError()
        if any(p not in self._ctx.permissions for p in perms):
            raise PermissionDeniedError()
        if self._tx is not None:
            raise IncompatibleTransactionError("Text protection cannot run inside a host transaction.")
        setup = self._e.protection
        if setup is None:
            raise ProtectionNotConfiguredError()
        return setup

    @staticmethod
    def _analysis_perm(analysis: Any) -> ScopePermission:
        from ..protection.types import JobAnalysis

        return SP.RESULTS_READ if isinstance(analysis, JobAnalysis) else SP.ANALYSIS_EXECUTE

    # ---- contexts ----------------------------------------------------------------------------------
    async def create_context(
        self, scope_id: str, *, kind: Literal["conversation", "job"], ttl_s: int | None = None
    ) -> ContextInfo:
        setup = self._gate_a06(scope_id, SP.TEXT_PROTECT)
        from ..protection import ops
        from ..protection.service import check_kind

        knd = check_kind(kind)
        if ttl_s is None:
            ttl = setup.context_ttl_s
        else:
            if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or not (60 <= ttl_s <= setup.max_context_ttl_s):
                raise ValidationFailedError("Invalid ttl_s.", field="ttl_s")
            ttl = ttl_s
        return await ops.create_context(self._e, setup, scope_id, knd, ttl)

    async def get_context(self, scope_id: str, context_id: str) -> ContextInfo:
        self._gate_a06(scope_id, SP.CONTEXTS_READ)
        from ..protection import ops
        from ..protection.service import check_context_id

        return await ops.get_context(self._e, scope_id, check_context_id(context_id))

    async def list_contexts(
        self,
        scope_id: str,
        *,
        kind: str | None = None,
        state: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> Page[ContextInfo]:
        self._gate_a06(scope_id, SP.CONTEXTS_READ)
        from ..protection import ops

        if kind is not None:
            v.choice(kind, "kind", ("conversation", "job"))
        if state is not None:
            v.choice(state, "state", ("active", "expired", "deleted"))
        lim = self._e.config.limits
        items, nxt = await ops.list_contexts(
            self._e,
            scope_id,
            kind=kind,
            state=state,
            limit=page_size(limit, lim.default_page_size, lim.max_page_size),
            cursor=cursor,
        )
        return Page(items=items, next_cursor=nxt)

    async def delete_context(self, scope_id: str, context_id: str) -> ContextInfo:
        setup = self._gate_a06(scope_id, SP.CONTEXTS_DELETE)
        from ..protection import ops
        from ..protection.service import check_context_id

        return await ops.delete_context(self._e, setup, scope_id, check_context_id(context_id))

    # ---- text --------------------------------------------------------------------------------------
    async def protect_text(
        self,
        scope_id: str,
        *,
        context_id: str,
        kind: Literal["conversation", "job"],
        text: str,
        policy: ProtectionPolicy,
        analysis: AnalysisSource,
        request_id: str | None = None,
    ) -> ProtectedText:
        # the second permission depends on the source: authorize BEFORE anything else (docs 12.1)
        setup = self._gate_a06(scope_id, SP.TEXT_PROTECT, self._analysis_perm(analysis))
        from ..protection import service

        return await service.protect_text(
            self._e,
            setup,
            scope_id,
            context_id=context_id,
            kind=kind,
            text=text,
            policy=policy,
            analysis=analysis,
            request_id=request_id,
        )

    async def restore_text(
        self,
        scope_id: str,
        *,
        context_id: str,
        kind: Literal["conversation", "job"],
        text: str,
        analysis: AnalysisSource,
        delivered_material: Sequence[str] | None = None,
        delivered_markers: Collection[str] | None = None,
        on_new_findings: Literal["redact", "fail"] = "redact",
        request_id: str | None = None,
    ) -> RestoredText:
        setup = self._gate_a06(scope_id, SP.TEXT_RESTORE, self._analysis_perm(analysis))
        from ..protection import service

        return await service.restore_text(
            self._e,
            setup,
            scope_id,
            context_id=context_id,
            kind=kind,
            text=text,
            analysis=analysis,
            delivered_material=delivered_material,
            delivered_markers=delivered_markers,
            on_new_findings=on_new_findings,
            request_id=request_id,
        )
