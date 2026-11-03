"""Command line: `wewln serve | check-config | init-db` (or `python -m wewln_phonecaller`)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections.abc import Sequence
from typing import Any

from aiohttp import web

from . import __version__
from .api import create_app
from .config import Settings, find_unknown_variables, load_settings
from .errors import ConfigError, StorageError
from .logging_setup import setup_logging, shutdown_logging
from .safety import evaluate_real_calls
from .storage import Database

log = logging.getLogger("wewln.cli")

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2


def _utf8_stdio() -> None:
    """Windows consoles and pipes may default to a legacy code page; Russian text needs UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _load(env_file: str | None, **overrides: Any) -> Settings | None:
    try:
        return load_settings(env_file=env_file or None, **overrides)
    except ConfigError as exc:
        print("Configuration is invalid:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return None


def cmd_check_config(args: argparse.Namespace) -> int:
    for name in find_unknown_variables(env_file=args.env_file or None):
        print(f"warning: unknown variable {name} is ignored (typo?)", file=sys.stderr)
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    decision = evaluate_real_calls(settings)
    print("Configuration is valid.")
    for key, value in settings.redacted_summary().items():
        print(f"  {key}: {value}")
    blocked = ", ".join(r.value for r in decision.reasons)
    suffix = f" (blocked by: {blocked})" if blocked else ""
    print(f"  real_calls_allowed: {decision.allowed}{suffix}")
    return EXIT_OK


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    db = Database(settings.resolved_db_path)
    try:
        version = db.migrate()
    except StorageError as exc:
        print(f"Database error: {exc.message}", file=sys.stderr)
        return EXIT_RUNTIME
    print(f"Database ready: {db.path} (schema version {version})")
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    overrides: dict[str, Any] = {}
    if args.host:
        overrides["http_host"] = args.host
    if args.port:
        overrides["http_port"] = args.port
    settings = _load(args.env_file, **overrides)
    if settings is None:
        return EXIT_CONFIG
    setup_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        log_file=settings.resolved_log_file,
        secrets=settings.secret_values(),
    )
    db = Database(settings.resolved_db_path)
    decision = evaluate_real_calls(settings)
    try:
        version = db.migrate()
        db.audit(
            "app.start",
            details={
                "version": __version__,
                "mode": settings.mode,
                "telephony_provider": settings.telephony_provider,
                "real_calls": decision.as_dict(),
            },
        )
    except StorageError as exc:
        log.error("startup_failed", extra={"error": exc.message})
        shutdown_logging()
        return EXIT_RUNTIME

    app = create_app(settings, db)

    async def _record_stop(_app: web.Application) -> None:
        await asyncio.to_thread(db.audit, "app.stop")

    app.on_cleanup.append(_record_stop)
    host = f"[{settings.http_host}]" if ":" in settings.http_host else settings.http_host
    url = f"http://{host}:{settings.http_port}"
    log.info(
        "server_starting",
        extra={
            "url": url,
            "mode": settings.mode,
            "schema_version": version,
            "real_calls": decision.as_dict(),
        },
    )
    print(
        f"wewln-phonecaller {__version__} on {url} (mode={settings.mode}). Ctrl+C stops it.",
        flush=True,
    )
    try:
        web.run_app(
            app, host=settings.http_host, port=settings.http_port, access_log=None, print=None
        )
    finally:
        log.info("server_stopped")
        shutdown_logging()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wewln",
        description="wewln-phonecaller: Notion-controlled AI voice calling (simulation-first).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="path to the .env file (default: .env in the current folder; '' disables it)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the local HTTP API (/health, /ready)")
    serve.add_argument("--host", help="override WEWLN_HTTP_HOST")
    serve.add_argument("--port", type=int, help="override WEWLN_HTTP_PORT")
    serve.set_defaults(func=cmd_serve)
    check = sub.add_parser("check-config", help="validate and print the settings without secrets")
    check.set_defaults(func=cmd_check_config)
    init_db = sub.add_parser("init-db", help="create or upgrade the SQLite database")
    init_db.set_defaults(func=cmd_init_db)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    return int(args.func(args))
