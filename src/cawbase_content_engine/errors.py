"""Stable error hierarchy (see docs/public-api.md, section "Errores")."""

from __future__ import annotations


class EngineError(Exception):
    """Base class. ``code`` is stable; ``message`` is generic and never contains secrets."""

    code: str = "internal_error"
    http_status: int | None = 500
    retryable: bool = False

    def __init__(self, message: str | None = None, *, retryable: bool | None = None) -> None:
        self.message = message or self.default_message
        if retryable is not None:
            self.retryable = retryable
        super().__init__(self.message)

    default_message = "Internal error."

    def __str__(self) -> str:
        return self.message


class AuthenticationError(EngineError):
    code = "authentication_failed"
    http_status = 401
    default_message = "Authentication failed."


class PermissionDeniedError(EngineError):
    code = "permission_denied"
    http_status = 403
    default_message = "Permission denied."


class ResourceNotAccessibleError(EngineError):
    code = "resource_not_accessible"
    http_status = 404
    default_message = "Resource not accessible."


class ResourceNotFoundError(EngineError):
    code = "resource_not_found"
    http_status = 404
    default_message = "Resource not found."


class ConflictError(EngineError):
    code = "conflict"
    http_status = 409
    default_message = "Conflict with the current state of the resource."


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"
    default_message = "The idempotency key was already used with different parameters."


class ScopeDisabledError(ConflictError):
    code = "scope_disabled"
    default_message = "The scope is disabled."


class RevisionConflictError(EngineError):
    code = "revision_conflict"
    http_status = 412
    default_message = "The expected revision does not match the current revision."


class ValidationFailedError(EngineError):
    code = "validation_failed"
    http_status = 422
    default_message = "Validation failed."

    def __init__(self, message: str | None = None, *, field: str | None = None, retryable: bool | None = None) -> None:
        super().__init__(message, retryable=retryable)
        self.field = field


class PreconditionRequiredError(ValidationFailedError):
    code = "precondition_required"
    http_status = 428
    default_message = "A precondition header is required."


class LimitExceededError(ValidationFailedError):
    code = "limit_exceeded"
    http_status = 422
    default_message = "A configured limit was exceeded."


class PayloadTooLargeError(LimitExceededError):
    http_status = 413
    default_message = "The request body is too large."


class InvalidConfigurationError(EngineError):
    code = "configuration_invalid"
    http_status = None
    default_message = "Invalid configuration."


class EngineStateError(EngineError):
    code = "engine_not_ready"
    http_status = 503
    default_message = "The engine is not started or already closed."


class DatabaseUnavailableError(EngineError):
    code = "database_unavailable"
    http_status = 503
    retryable = True
    default_message = "The database is unavailable."


class SchemaMissingError(EngineError):
    code = "schema_missing"
    http_status = 503
    default_message = "The engine schema is not installed."


class SchemaIncompatibleError(EngineError):
    code = "schema_incompatible"
    http_status = 503
    default_message = "The engine schema is not compatible with this runtime."


class MigrationError(EngineError):
    code = "migration_failed"
    http_status = None
    default_message = "Migration failed."


class MigrationLockTimeoutError(MigrationError):
    code = "migration_lock_timeout"
    retryable = True
    default_message = "Timed out waiting for the migration lock."


class MigrationPermissionError(MigrationError):
    code = "migration_permission_denied"
    default_message = "The migration credentials lack the required DDL permissions."


class TransactionStateError(EngineError):
    code = "transaction_state_invalid"
    http_status = None
    default_message = "The host transaction is not in a usable state."


class TransactionBrokenError(EngineError):
    code = "transaction_broken"
    http_status = None
    default_message = "The host transaction can no longer be assumed healthy: roll it back and discard the connection."


class IncompatibleTransactionError(EngineError):
    code = "transaction_incompatible"
    http_status = None
    default_message = "The transaction or connection is incompatible with this engine."


class BootstrapNotAllowedError(EngineError):
    code = "bootstrap_not_allowed"
    http_status = None
    default_message = "The installation was already initialized administratively."


class RecoveryNotAllowedError(EngineError):
    code = "recovery_not_allowed"
    http_status = None
    default_message = "The installation was never initialized; use the initial bootstrap."


class SecretDeliveryError(EngineError):
    """The credential was committed but its secret could not be delivered (never contains it)."""

    code = "secret_delivery_failed"
    http_status = None
    default_message = (
        "The credential was issued but its secret could not be delivered. "
        "It cannot be recovered: issue a new credential and revoke the orphaned one."
    )

    def __init__(self, key_id: str, message: str | None = None) -> None:
        super().__init__(message)
        self.key_id = key_id


class InternalError(EngineError):
    code = "internal_error"
    http_status = 500
    default_message = "Internal error."
