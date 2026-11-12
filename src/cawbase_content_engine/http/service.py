"""Independent service: validate -> migrate -> verify with runtime credentials -> only then listen.

While preparation runs the process does not listen at all (no /livez, no /readyz). Any failure
logs a sanitized error and exits non-zero; there is no mode that stays up after a failed start.
Workers never migrate; each one only verifies compatibility before serving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from pydantic import SecretStr

from ..config import ENV_PREFIX, EngineConfig, read_secret
from ..engine import ContentEngine
from ..errors import (
    EngineError,
    InvalidConfigurationError,
    MigrationError,
    SchemaIncompatibleError,
    SchemaMissingError,
)
from .router import create_health_router, create_router

log = logging.getLogger("cawbase_content_engine.service")

EXIT_OTHER, EXIT_CONFIG, EXIT_MIGRATION, EXIT_COMPAT = 1, 2, 3, 4

#: Internal launcher->worker channel (not configuration): a private directory where a worker that
#: fails to initialize records the exit code the service must report. Uvicorn itself reports its own
#: code 3 ("startup failure") for every kind of worker failure, which would be indistinguishable from
#: a migration failure (also 3 in this service); and a multi-worker launcher would otherwise return 0
#: after the supervisor stops because a worker could not start.
_STATE_ENV = "_CCE_WORKER_STATE_DIR"
_FAILED_MARKER = "worker-startup-failed"


def _record_worker_failure(exc: BaseException) -> None:
    code = exit_code_for(exc)
    log.error(
        "worker_startup_failed",
        extra={
            "event": "worker_startup_failed",
            "error_code": getattr(exc, "code", None),
            "exit_code": code,
            "error_class": type(exc).__name__,
        },
    )
    directory = os.environ.get(_STATE_ENV)
    if directory:
        try:
            with open(os.path.join(directory, _FAILED_MARKER), "w") as fh:
                fh.write(str(code))
        except OSError:
            pass


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(ENV_PREFIX + name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise InvalidConfigurationError(f"{ENV_PREFIX}{name} must be a boolean.")


@dataclass(frozen=True)
class ServiceSettings:
    config: EngineConfig
    migrate_on_start: bool = True
    migration_url: SecretStr | None = field(default=None, repr=False)
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = 1
    enable_openapi: bool = False
    log_level: str = "INFO"
    log_format: str = "json"
    inference_adapter: str = "none"  # "none" | "mock" (development/testing only; real adapters arrive with A09)
    inference_mock_fixtures: str | None = None
    inference_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> ServiceSettings:
        env = os.environ if environ is None else environ
        migrate = _bool(env, "MIGRATE_ON_START", True)
        murl = read_secret(env, "MIGRATION_DATABASE_URL")
        try:
            values: dict[str, Any] = dict(
                host=env.get(ENV_PREFIX + "HOST", "127.0.0.1"),
                port=int(env.get(ENV_PREFIX + "PORT", "8080")),
                workers=int(env.get(ENV_PREFIX + "WORKERS", "1")),
            )
        except ValueError:
            raise InvalidConfigurationError("PORT and WORKERS must be integers.") from None
        values.update(
            enable_openapi=_bool(env, "ENABLE_OPENAPI", False),
            log_level=env.get(ENV_PREFIX + "LOG_LEVEL", "INFO").upper(),
            log_format=env.get(ENV_PREFIX + "LOG_FORMAT", "json"),
        )
        try:
            values.update(
                inference_adapter=env.get(ENV_PREFIX + "INFERENCE_ADAPTER", "none").lower(),
                inference_mock_fixtures=env.get(ENV_PREFIX + "INFERENCE_MOCK_FIXTURES"),
                inference_timeout_s=float(env.get(ENV_PREFIX + "INFERENCE_TIMEOUT_S", "30")),
            )
        except ValueError:
            raise InvalidConfigurationError("INFERENCE_TIMEOUT_S must be a number.") from None
        values.update({k: v for k, v in overrides.items() if v is not None})
        config = EngineConfig.from_env(env, migration_mode="on_start" if migrate else "external")
        s = cls(config=config, migrate_on_start=migrate, migration_url=SecretStr(murl) if murl else None, **values)
        s.validate()
        return s

    def validate(self) -> None:
        if not (1 <= self.port <= 65535) or not (1 <= self.workers <= 64):
            raise InvalidConfigurationError("PORT or WORKERS out of range.")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR") or self.log_format not in ("json", "text"):
            raise InvalidConfigurationError("Invalid log level or format.")
        if self.config.async_engine is not None:
            raise InvalidConfigurationError("The standalone service builds its own engine from a URL.")
        if self.inference_adapter not in ("none", "mock"):
            raise InvalidConfigurationError("INFERENCE_ADAPTER must be 'none' or 'mock'.")
        if (self.inference_adapter == "mock") != (self.inference_mock_fixtures is not None):
            raise InvalidConfigurationError("INFERENCE_MOCK_FIXTURES applies to, and is required by, the mock adapter.")
        if not (0 < self.inference_timeout_s <= 600):
            raise InvalidConfigurationError("INFERENCE_TIMEOUT_S is out of range.")
        if self.migration_url is not None and not self.migrate_on_start:
            raise InvalidConfigurationError("A migration URL has no effect with migrate_on_start=false.")


class _JsonFormatter(logging.Formatter):
    _EXTRA = ("request_id", "error_class", "error_code", "event", "exit_code", "status", "adapter_kind", "duration_ms")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {"level": record.levelname, "logger": record.name, "event": record.getMessage()}
        for key in self._EXTRA:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload)


def configure_logging(settings: ServiceSettings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormatter() if settings.log_format == "json" else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    # The service also routes uvicorn's own (error/lifecycle) logger through this handler: supervisor
    # messages ("Child process ... died") become visible, and the formatter prints only the message
    # (never ``exc_info``), so worker start-up tracebacks cannot leak into the service log.
    for name in ("cawbase_content_engine", "uvicorn", "uvicorn.error"):
        target = logging.getLogger(name)
        target.handlers[:] = [handler]
        target.setLevel(settings.log_level)
        target.propagate = False


def build_inference_setup(settings: ServiceSettings) -> Any:
    """Operator-chosen adapter (never a request parameter). ``None`` when inference is not configured."""
    if settings.inference_adapter != "mock":
        return None
    from ..inference import InferenceSetup, MockInferenceAdapter

    assert settings.inference_mock_fixtures is not None
    adapter = MockInferenceAdapter.from_file(settings.inference_mock_fixtures, acknowledge_simulated=True)
    return InferenceSetup(adapter, timeout_s=settings.inference_timeout_s)


async def prepare(settings: ServiceSettings) -> None:
    """Steps 1-3. Raises a typed EngineError; never starts listening."""
    from ..migrations import upgrade  # extra 'migrations'

    settings.validate()
    if build_inference_setup(settings) is not None:  # a bad fixtures file fails before listening
        log.warning("inference_mock_enabled", extra={"event": "inference_mock_enabled", "adapter_kind": "mock"})
    if settings.migrate_on_start:
        log.info("migration_started", extra={"event": "migration_started"})
        report = await upgrade(settings.config, migration_url=settings.migration_url)
        log.info("migration_finished", extra={"event": "migration_finished"})
        _ = report
    probe = ContentEngine(settings.config)  # runtime credentials only
    await probe.start()  # SchemaMissing / SchemaIncompatible / DatabaseUnavailable
    await probe.aclose()


def exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, InvalidConfigurationError):
        return EXIT_CONFIG
    if isinstance(exc, MigrationError):
        return EXIT_MIGRATION
    if isinstance(exc, (SchemaMissingError, SchemaIncompatibleError)):
        return EXIT_COMPAT
    if isinstance(exc, EngineError) and exc.code == "database_unavailable":
        return EXIT_COMPAT
    return EXIT_OTHER


def create_service_app(engine: ContentEngine, *, enable_openapi: bool = False) -> FastAPI:
    """Application around an engine. It never migrates; lifespan only verifies compatibility."""
    state = {"stopping": False}
    cache: dict[str, Any] = {"at": 0.0, "value": (False, "not_started")}

    async def readiness() -> tuple[bool, str]:
        if state["stopping"]:
            return False, "stopping"
        if time.monotonic() - cache["at"] > 2.0:
            report = await engine.readiness()
            cache.update(at=time.monotonic(), value=(report.ready, report.state))
        return cache["value"]  # type: ignore[no-any-return]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await engine.start()
        except BaseException as exc:
            _record_worker_failure(exc)
            raise
        try:
            yield
        finally:
            state["stopping"] = True
            await engine.aclose()

    app = FastAPI(
        lifespan=lifespan,
        docs_url="/docs" if enable_openapi else None,
        redoc_url=None,
        openapi_url="/openapi.json" if enable_openapi else None,
    )
    app.include_router(create_health_router(readiness))
    inner = create_router(engine)

    @app.middleware("http")
    async def gate(request: Any, call_next: Any) -> Any:
        if request.url.path.startswith("/v1/"):
            ready, state_name = await readiness()
            if not ready:
                from fastapi.responses import JSONResponse

                code = (
                    "database_unavailable"
                    if state_name == "degraded_database"
                    else ("schema_incompatible" if state_name == "schema_incompatible" else "engine_not_ready")
                )
                request_id = uuid.uuid4().hex
                return JSONResponse(
                    {"error": {"code": code, "message": "The service is not ready.", "request_id": request_id}},
                    status_code=503,
                    headers={"X-Request-ID": request_id},
                )
        return await call_next(request)

    app.include_router(inner)
    return app


def app_factory() -> FastAPI:
    """Uvicorn ``--factory`` entry point for workers: reads the environment; does NOT migrate."""
    settings = ServiceSettings.from_env()
    configure_logging(settings)
    return create_service_app(
        ContentEngine(settings.config, inference=build_inference_setup(settings)),
        enable_openapi=settings.enable_openapi,
    )


def serve(settings: ServiceSettings) -> int:
    """Prepare, then listen. Returns a process exit code only on failure."""
    import uvicorn

    configure_logging(settings)
    try:
        asyncio.run(prepare(settings))
    except EngineError as exc:
        code = exit_code_for(exc)
        log.error("startup_failed", extra={"error_code": exc.code, "exit_code": code})
        return code
    except Exception as exc:  # sanitized
        log.error("startup_failed", extra={"error_class": type(exc).__name__, "exit_code": EXIT_OTHER})
        return EXIT_OTHER
    # Best-effort only: closing the DDL connection/engine does not erase credentials from this
    # process's memory. Strict privilege separation requires external migrations.
    for name in ("MIGRATION_DATABASE_URL", "MIGRATION_DATABASE_URL_FILE"):
        os.environ.pop(ENV_PREFIX + name, None)
    os.environ[ENV_PREFIX + "MIGRATE_ON_START"] = "false"  # workers must never migrate
    state_dir = tempfile.mkdtemp(prefix="cce-service-")  # private (0700)
    os.environ[_STATE_ENV] = state_dir
    returncode = 0
    try:
        try:
            uvicorn.run(
                "cawbase_content_engine.http.service:app_factory",
                factory=True,
                host=settings.host,
                port=settings.port,
                workers=settings.workers,
                log_config=None,
                access_log=False,
            )
        except SystemExit as exc:  # single worker: uvicorn exits with its own code 3 on startup failure
            returncode = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else EXIT_OTHER)
        marker = os.path.join(state_dir, _FAILED_MARKER)
        if os.path.exists(marker):
            try:
                with open(marker) as fh:
                    returncode = int(fh.read().strip())
            except (OSError, ValueError):
                returncode = EXIT_OTHER
            log.error("service_stopped_after_worker_startup_failure", extra={"exit_code": returncode})
    finally:
        os.environ.pop(_STATE_ENV, None)
        shutil.rmtree(state_dir, ignore_errors=True)
    return returncode
