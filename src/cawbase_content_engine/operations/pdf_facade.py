"""A08 methods of ``ScopedOperations`` (authorization, host-transaction rejection, dispatch). Lazy imports only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from .. import validation as v
from ..contexts import ScopeContext
from ..errors import (
    DocumentsNotConfiguredError,
    IncompatibleTransactionError,
    PermissionDeniedError,
    ProtectionNotConfiguredError,
    ResourceNotAccessibleError,
)
from ..permissions import ScopePermission
from ..transactions import HostTransaction

if TYPE_CHECKING:
    from ..engine import ContentEngine
    from ..jobs.models import JobAccepted
    from ..jobs.spool import SpooledStream
    from ..pdf_protection.types import (
        ExistingContext,
        NewContext,
        PdfDerivativeInfo,
        PdfProtectionAccepted,
        PdfProtectionResult,
        PdfRestorationResult,
    )
    from ..protection.types import ProtectionPolicy, ProtectionSetup

SP = ScopePermission


class PdfOperations:
    _e: ContentEngine
    _ctx: ScopeContext
    _tx: HostTransaction | None

    def _gate_a08(self, scope_id: str, *perms: ScopePermission) -> ProtectionSetup:
        """Membership, permissions, host-transaction rejection, then configuration (docs/pdf-protection.md 11)."""
        v.scope_id(scope_id)
        if scope_id != self._ctx.scope_id:
            raise ResourceNotAccessibleError()
        if any(p not in self._ctx.permissions for p in perms):
            raise PermissionDeniedError()
        if self._tx is not None:
            raise IncompatibleTransactionError("PDF protection cannot run inside a host transaction.")
        if self._e.documents is None:
            raise DocumentsNotConfiguredError()
        setup = self._e.protection
        if setup is None:
            raise ProtectionNotConfiguredError()
        return setup

    async def submit_pdf_protection(
        self,
        scope_id: str,
        *,
        analysis_job_id: str,
        policy: ProtectionPolicy,
        context: ExistingContext | NewContext,
        idempotency_key: str,
    ) -> PdfProtectionAccepted:
        setup = self._gate_a08(scope_id, SP.PDF_PROTECT, SP.RESULTS_READ)
        from ..pdf_protection import ops

        return await ops.submit_protection(
            self._e,
            setup,
            scope_id,
            analysis_job_id=analysis_job_id,
            policy=policy,
            context=context,
            idempotency_key=idempotency_key,
        )

    async def get_pdf_protection_result(self, scope_id: str, job_id: str) -> PdfProtectionResult:
        self._gate_a08(scope_id, SP.PDF_PROTECTED_READ)
        from ..pdf_protection import ops

        return await ops.get_protection_result(self._e, scope_id, job_id)

    async def open_pdf_protected(self, scope_id: str, job_id: str) -> SpooledStream:
        self._gate_a08(scope_id, SP.PDF_PROTECTED_READ)
        from ..pdf_protection import ops

        return await ops.open_protected(self._e, scope_id, job_id)

    async def get_pdf_derivative(self, scope_id: str, derivative_id: str) -> PdfDerivativeInfo:
        self._gate_a08(scope_id, SP.PDF_PROTECTED_READ)
        from ..pdf_protection import ops

        return await ops.get_derivative(self._e, scope_id, derivative_id)

    async def submit_pdf_restoration(
        self,
        scope_id: str,
        *,
        derivative_id: str,
        file_id: str,
        context_id: str,
        kind: Literal["conversation", "job"],
        idempotency_key: str,
    ) -> JobAccepted:
        self._gate_a08(scope_id, SP.PDF_RESTORE)
        from ..pdf_protection import ops

        return await ops.submit_restoration(
            self._e,
            scope_id,
            derivative_id=derivative_id,
            file_id=file_id,
            context_id=context_id,
            kind=kind,
            idempotency_key=idempotency_key,
        )

    async def get_pdf_restoration_result(self, scope_id: str, job_id: str) -> PdfRestorationResult:
        self._gate_a08(scope_id, SP.PDF_RESTORED_READ)
        from ..pdf_protection import ops

        return await ops.get_restoration_result(self._e, scope_id, job_id)

    async def open_pdf_restored(self, scope_id: str, job_id: str) -> SpooledStream:
        self._gate_a08(scope_id, SP.PDF_RESTORED_READ)
        from ..pdf_protection import ops

        return await ops.open_restored(self._e, scope_id, job_id)


_ = Any
