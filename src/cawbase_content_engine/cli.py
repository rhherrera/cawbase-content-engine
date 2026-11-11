"""Command line. The database URL is read only from the environment (or ``*_FILE``), never argv."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from ._version import __version__
from .errors import EngineError

EXIT_OTHER, EXIT_CONFIG, EXIT_MIGRATION, EXIT_COMPAT = 1, 2, 3, 4


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cawbase-content-engine")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print the package version")
    sub.add_parser("check", help="inspect the schema without migrating; non-zero if not compatible")
    m = sub.add_parser("migrate", help="apply pending migrations (one transaction, one lock)")
    m.add_argument("--lock-timeout", type=float, default=None, help="seconds to wait for the migration lock")
    b = sub.add_parser("bootstrap-admin", help="create the first administrator key (once per installation)")
    b.add_argument("--secret-file", help="write the secret to a new 0600 file instead of stdout")
    r = sub.add_parser("recover-admin", help="issue a new administrator key in a new client")
    r.add_argument("--confirm", action="store_true", help="required: acknowledge this is a deliberate operator act")
    r.add_argument("--secret-file", help="write the secret to a new 0600 file instead of stdout")
    s = sub.add_parser("serve", help="validate, migrate, verify, then serve HTTP")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--workers", type=int)
    return p


def _exit_code(exc: BaseException) -> int:
    from .errors import (
        InvalidConfigurationError,
        MigrationError,
        SchemaIncompatibleError,
        SchemaMissingError,
    )

    if isinstance(exc, InvalidConfigurationError):
        return EXIT_CONFIG
    if isinstance(exc, MigrationError):
        return EXIT_MIGRATION
    if isinstance(exc, (SchemaMissingError, SchemaIncompatibleError)):
        return EXIT_COMPAT
    return EXIT_OTHER


async def _with_engine(args: argparse.Namespace) -> int:
    from .bootstrap import SecretFileSink, create_initial_admin_key, recover_admin_access
    from .config import EngineConfig
    from .engine import ContentEngine

    sink = SecretFileSink(args.secret_file) if getattr(args, "secret_file", None) else None
    async with ContentEngine(EngineConfig.from_env()) as engine:
        if args.command == "bootstrap-admin":
            issued = await create_initial_admin_key(engine, sink=sink)
        else:
            issued = await recover_admin_access(engine, confirm=args.confirm, sink=sink)
    assert issued.secret is not None
    print(f"key_id: {issued.info.key_id}", file=sys.stderr)
    if sink is None:
        print(issued.secret.get_secret_value())  # the only time it is ever shown
    else:
        print(f"secret written to {args.secret_file}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "version":
            print(__version__)
            return 0
        if args.command == "serve":
            from .http.service import ServiceSettings, serve

            settings = ServiceSettings.from_env(host=args.host, port=args.port, workers=args.workers)
            return serve(settings)
        from .config import EngineConfig

        if args.command == "check":
            from .schema import check_schema

            status = asyncio.run(check_schema(EngineConfig.from_env()))
            print(json.dumps(status.model_dump(mode="json")))
            return 0 if status.compatible else EXIT_COMPAT
        if args.command == "migrate":
            from .migrations import upgrade

            report = asyncio.run(upgrade(EngineConfig.from_env(), lock_timeout=args.lock_timeout))
            print(
                json.dumps(
                    {
                        "applied": list(report.applied),
                        "already_current": report.already_current,
                        "to_level": report.to_level,
                    }
                )
            )
            return 0
        return asyncio.run(_with_engine(args))
    except ModuleNotFoundError as exc:
        print(f"error: optional dependency missing ({exc.name}); install the matching extra.", file=sys.stderr)
        return EXIT_CONFIG
    except EngineError as exc:
        print(f"error: {exc.code} - {exc.message}", file=sys.stderr)
        return _exit_code(exc)
    except Exception as exc:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return EXIT_OTHER


if __name__ == "__main__":
    raise SystemExit(main())
