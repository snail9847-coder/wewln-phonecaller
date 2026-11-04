"""Command line: `wewln <command>` (or `python -m wewln_phonecaller <command>`).

Messages are in English like the logs; docs/ (Russian) explains every command.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import getpass
import io
import logging
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .api import create_app
from .config import (
    ENV_PREFIX,
    MIN_WEBHOOK_SECRET_LENGTH,
    MOCK_SCENARIOS,
    Settings,
    check_notion_token_format,
    check_secret_format,
    find_unknown_variables,
    load_settings,
)
from .domain import CampaignStatus, reason_label
from .errors import ConfigError, RealCallsBlockedError, StorageError, WewlnError
from .logging_setup import setup_logging, shutdown_logging
from .phones import describe_number, dial_policy_problem, normalize_phone
from .redact import mask_phone_numbers
from .safety import evaluate_real_calls
from .secret_store import (
    ARI_PASSWORD_KEY,
    NOTION_TOKEN_KEY,
    WEBHOOK_SECRET_KEY,
    SecretStoreUnavailable,
    default_store,
)
from .storage import Database
from .storage.calls import CallStore
from .storage.repository import ContactInput, Repository
from .telephony import PROVIDERS
from .telephony.base import ProviderError
from .telephony.factory import create_call_store, create_provider, create_repository
from .telephony.lifecycle import STATE_LABELS, CallState
from .telephony.service import CallOutcome, CallService

log = logging.getLogger("wewln.cli")

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2

CSV_COLUMNS = {
    "name": {"name", "имя", "фио", "contact", "контакт"},
    "phone": {"phone", "телефон", "номер", "phone_number", "номер телефона"},
    "dnc": {"dnc", "do_not_call", "do not call", "не звонить", "не_звонить"},
}
TRUE_WORDS = {"1", "true", "yes", "y", "x", "+", "да", "д"}


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


def _quiet_logging(settings: Settings) -> None:
    """One-shot commands log to the log file only; their own output stays readable."""
    setup_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        log_file=settings.resolved_log_file,
        secrets=settings.secret_values(),
        console=False,
    )


def _repository(settings: Settings) -> Repository:
    db = Database(settings.resolved_db_path)
    db.migrate()
    return create_repository(settings, db)


def _with_repository(func: Callable[[argparse.Namespace, Settings, Repository], int]):
    def command(args: argparse.Namespace) -> int:
        settings = _load(args.env_file)
        if settings is None:
            return EXIT_CONFIG
        _quiet_logging(settings)
        return func(args, settings, _repository(settings))

    return command


def _table(rows: list[list[Any]], headers: list[str]) -> None:
    if not rows:
        print("(nothing)")
        return
    cells = [[str(h) for h in headers], *[["" if v is None else str(v) for v in r] for r in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    for index, row in enumerate(cells):
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip())
        if index == 0:
            print("  ".join("-" * w for w in widths))


def _phone(value: str | None, unmask: bool) -> str:
    if not value:
        return ""
    return value if unmask else mask_phone_numbers(value)


# ---------------------------------------------------------------------- basic commands


def cmd_check_config(args: argparse.Namespace) -> int:
    for name in find_unknown_variables(env_file=args.env_file or None):
        print(f"warning: unknown variable {name} is ignored (typo?)", file=sys.stderr)
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    decision = evaluate_real_calls(settings)
    problems = settings.provider_problems
    if problems:
        print("Configuration is invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_CONFIG
    print("Configuration is valid.")
    for key, value in settings.redacted_summary().items():
        print(f"  {key}: {value}")
    blocked = ", ".join(r.value for r in decision.reasons)
    suffix = f" (blocked by: {blocked})" if blocked else ""
    print(f"  real_calls_allowed: {decision.allowed}{suffix}")
    _print_call_path(settings)
    for problem in settings.voice_problems:
        print(f"warning: voice engine: {problem}", file=sys.stderr)
    return EXIT_OK


def _print_call_path(settings: Settings, *, softphone: bool = False) -> None:
    from .cli_voice import describe_call_path

    print("Call path:")
    for line in describe_call_path(settings, softphone=softphone):
        print(f"  {line}")


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
    from .notion.sync import NotionSync, SyncService

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
                "notion_enabled": settings.notion_enabled,
            },
        )
    except StorageError as exc:
        log.error("startup_failed", extra={"error": exc.message})
        shutdown_logging()
        return EXIT_RUNTIME

    sync_service = None
    if settings.notion_enabled:
        repo = create_repository(settings, db)
        sync_service = SyncService(
            NotionSync(settings, repo), interval=settings.notion_sync_interval
        )
    app = create_app(settings, db, sync_service=sync_service)

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
            "notion_sync_interval_s": settings.notion_sync_interval
            if settings.notion_enabled
            else None,
        },
    )
    notion = "on" if settings.notion_enabled else "off"
    print(
        f"wewln-phonecaller {__version__} on {url} (mode={settings.mode}, notion sync {notion})."
        " Ctrl+C stops it.",
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


# ---------------------------------------------------------------------- notion


def cmd_notion_token_set(args: argparse.Namespace) -> int:
    if args.stdin:
        token = sys.stdin.readline().strip()
    else:
        token = getpass.getpass("Paste the Notion token (input stays hidden), then Enter: ").strip()
    try:
        check_notion_token_format(token)
    except ValueError as exc:
        print(f"The token was not saved: it {exc}", file=sys.stderr)
        return EXIT_CONFIG
    store = default_store()
    try:
        store.set(NOTION_TOKEN_KEY, token)
    except SecretStoreUnavailable as exc:
        print(
            f"The token was not saved: {exc}. Set the WEWLN_NOTION_TOKEN environment variable "
            "instead (see docs/NOTION.md).",
            file=sys.stderr,
        )
        return EXIT_RUNTIME
    print(f"Notion token saved in the OS credential store ({store.name}).")
    if any(k.upper() == f"{ENV_PREFIX}NOTION_TOKEN" for k in os.environ):
        print("Note: WEWLN_NOTION_TOKEN is set in the environment and takes precedence.")
    print("Check the connection with: wewln notion check")
    return EXIT_OK


def cmd_notion_token_delete(args: argparse.Namespace) -> int:
    store = default_store()
    try:
        deleted = store.delete(NOTION_TOKEN_KEY)
    except SecretStoreUnavailable as exc:
        print(f"Credential store unavailable: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    print("Notion token deleted." if deleted else "No Notion token was stored.")
    return EXIT_OK


def cmd_notion_token_status(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    store = default_store()
    source = settings.notion_token_source
    print(f"Notion token: {f'set ({source})' if settings.notion_token else 'not set'}")
    print(f"Credential store: {store.name}")
    return EXIT_OK


def _notion_settings(args: argparse.Namespace) -> Settings | None:
    settings = _load(args.env_file)
    if settings is None:
        return None
    if settings.notion_token is None:
        print(
            "Notion token is not set: run `wewln notion token set` or set WEWLN_NOTION_TOKEN.",
            file=sys.stderr,
        )
        return None
    _quiet_logging(settings)
    return settings


def cmd_notion_check(args: argparse.Namespace) -> int:
    from .notion.client import NotionClient
    from .notion.setup import database_title, describe_connection, resolve_workspace

    settings = _notion_settings(args)
    if settings is None:
        return EXIT_CONFIG

    async def run() -> tuple[dict[str, Any], Any]:
        async with NotionClient.from_settings(settings) as client:
            who = await describe_connection(client)
            workspace = await resolve_workspace(
                client, settings.notion_databases, repair=args.repair
            )
        return who, workspace

    who, workspace = asyncio.run(run())
    print(f"Connected as «{who['name']}» ({who['type']}) to workspace «{who['workspace']}».")
    for db in workspace.databases.values():
        state = "ok" if db.report is not None and db.report.ok else "needs attention"
        if db.repaired:
            state += " (missing properties or options were added)"
        print(f"  {db.spec.env_var}: «{database_title(db)}» {state}")
    blocking = workspace.blocking_problems()
    for problem in workspace.problems():
        print(f"  - {problem}")
    if blocking:
        print(
            "The sync cannot run until these problems are fixed"
            + ("" if args.repair else " (try: wewln notion check --repair)")
            + ".",
            file=sys.stderr,
        )
        return EXIT_RUNTIME
    if workspace.problems():
        print("Missing select options are added by Notion automatically when wewln writes them.")
    print("Notion is ready for the sync.")
    return EXIT_OK


def _write_env(path: Path, lines: list[str]) -> None:
    values = dict(line.split("=", 1) for line in lines)
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    output: list[str] = []
    done: set[str] = set()
    for raw in existing:
        key = raw.split("=", 1)[0].strip()
        if "=" in raw and not raw.lstrip().startswith("#") and key in values:
            output.append(f"{key}={values[key]}")
            done.add(key)
        else:
            output.append(raw)
    output += [f"{key}={value}" for key, value in values.items() if key not in done]
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def cmd_notion_bootstrap(args: argparse.Namespace) -> int:
    from .notion.client import NotionClient
    from .notion.setup import bootstrap, database_title

    settings = _notion_settings(args)
    if settings is None:
        return EXIT_CONFIG

    async def run() -> Any:
        async with NotionClient.from_settings(settings) as client:
            return await bootstrap(client, args.parent)

    workspace = asyncio.run(run())
    for db in workspace.databases.values():
        state = "created" if db.created else "found"
        if db.repaired:
            state += ", missing properties or options added"
        print(f"  «{database_title(db)}»: {state}")
    lines = workspace.env_lines()
    if args.write_env:
        if not args.env_file:
            print("--write-env needs an .env file (do not use --env-file '').", file=sys.stderr)
            return EXIT_CONFIG
        _write_env(Path(args.env_file), lines)
        print(f"Saved the database IDs to {args.env_file}.")
    else:
        print("Add these lines to your .env file (or run again with --write-env):")
        for line in lines:
            print(f"  {line}")
    print("Next: wewln notion check, then wewln notion sync (or wewln serve).")
    return EXIT_OK


def cmd_notion_sync(args: argparse.Namespace) -> int:
    from .notion.sync import NotionSync

    settings = _notion_settings(args)
    if settings is None:
        return EXIT_CONFIG
    repo = _repository(settings)
    result = asyncio.run(NotionSync(settings, repo).run_once())
    print(f"Sync {result.status}.")
    if result.error_message:
        print(f"  error: {result.error_message}")
    for name, value in sorted(result.stats.items()):
        print(f"  {name}: {value}")
    for problem in result.problems:
        print(f"  - {problem}")
    return EXIT_OK if result.status in ("ok", "partial") else EXIT_RUNTIME


def cmd_notion_status(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    repo = _repository(settings)
    print(f"Notion sync: {'enabled' if settings.notion_enabled else 'disabled'}")
    runs = repo.recent_sync_runs(args.limit)
    _table(
        [
            [
                r["started_at"],
                r["status"],
                r["error_code"] or "",
                r["stats"].get("requests", ""),
                r["stats"].get("problems", 0),
            ]
            for r in runs
        ],
        ["started (UTC)", "status", "error", "requests", "problems"],
    )
    return EXIT_OK


def cmd_notion_mock_server(args: argparse.Namespace) -> int:
    from .notion.mock_server import MockNotionServer, seed_demo

    server = MockNotionServer(port=args.port)
    try:
        base_url = server.start()
    except (OSError, RuntimeError) as exc:
        print(f"Cannot start the mock server: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    try:
        demo = asyncio.run(seed_demo(server)) if args.demo else None
        parent = demo["parent"] if demo else server.mock.add_page("wewln (mock)")
        print(f"Mock Notion API on {base_url}: an imitation for trials, data is kept in memory.")
        print("In another terminal (PowerShell) point wewln at it:")
        print(f'  $env:WEWLN_NOTION_API_BASE_URL = "{base_url}"')
        print(f'  $env:WEWLN_NOTION_TOKEN = "{server.mock.token}"')
        if demo:
            for line in demo["env"]:
                key, value = line.split("=", 1)
                print(f'  $env:{key} = "{value}"')
            print("Then: wewln notion sync; wewln simulate; wewln notion sync; wewln jobs list")
        else:
            print(f"Then: wewln notion bootstrap --parent {parent}")
        print("Ctrl+C stops the mock server.", flush=True)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        server.stop()


# ---------------------------------------------------------------------- campaigns and contacts


@_with_repository
def cmd_campaign_create(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    campaign_id = repo.create_local_campaign(args.name, script=args.script or "")
    print(
        f"Campaign {campaign_id} created (draft). Add contacts: wewln contacts import <file.csv> "
        f"--campaign {campaign_id}"
    )
    return EXIT_OK


@_with_repository
def cmd_campaign_list(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    rows = []
    for c in repo.list_campaigns():
        jobs = ", ".join(f"{k}={v}" for k, v in sorted(c["jobs"].items()))
        rows.append(
            [c["id"], c["source"], c["status"], c["name"], c["members"], jobs, c["problem"]]
        )
    _table(rows, ["id", "source", "status", "name", "contacts", "jobs", "problem"])
    return EXIT_OK


def _campaign_status(status: CampaignStatus):
    @_with_repository
    def command(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
        report = repo.set_local_campaign_status(args.campaign_id, status)
        print(f"Campaign {args.campaign_id}: {status.value} ({report.created} job(s) planned).")
        return EXIT_OK

    return command


@_with_repository
def cmd_campaign_stop(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    report = repo.stop_campaign(args.campaign_id, reason=args.reason or "", source="local")
    campaign = repo.get_campaign(args.campaign_id)
    print(
        f"Campaign {args.campaign_id} is {campaign['status']} ({report.changed} job(s) cancelled)."
        " A stopped campaign never runs again."
    )
    if campaign["source"] == "notion":
        print("Notion gets the new status on the next sync.")
    return EXIT_OK


def read_contacts_csv(path: Path) -> list[ContactInput]:
    """CSV from Excel or Google Sheets: UTF-8 (with or without BOM) or Windows-1251; separated
    by commas, semicolons or tabs; header names in English or Russian."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    header = next(reader, None)
    if header is None:
        raise WewlnError("the file is empty", code="invalid_input", http_status=400)
    columns: dict[str, int] = {}
    for index, title in enumerate(header):
        name = title.strip().lower()
        for column, aliases in CSV_COLUMNS.items():
            if name in aliases and column not in columns:
                columns[column] = index
    if "phone" not in columns:
        raise WewlnError(
            "no phone column: the first row must name the columns, e.g. name;phone;dnc "
            "(or имя;телефон;не звонить)",
            code="invalid_input",
            http_status=400,
        )

    def cell(row: list[str], column: str) -> str:
        index = columns.get(column)
        return row[index].strip() if index is not None and index < len(row) else ""

    contacts = []
    for row in reader:
        if not any(value.strip() for value in row):
            continue
        contacts.append(
            ContactInput(
                line=reader.line_num,
                name=cell(row, "name"),
                phone=cell(row, "phone"),
                dnc=cell(row, "dnc").lower() in TRUE_WORDS,
            )
        )
    return contacts


@_with_repository
def cmd_contacts_import(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    try:
        rows = read_contacts_csv(Path(args.file))
    except OSError as exc:
        print(f"Cannot read {args.file}: {type(exc).__name__}", file=sys.stderr)
        return EXIT_RUNTIME
    report = repo.import_contacts(rows, campaign_id=args.campaign)
    print(
        f"Imported: {report.created} new, {report.updated} updated, "
        f"{report.added_to_campaign} added to the campaign, {report.marked_dnc} marked do-not-call,"
        f" {len(report.rejected)} rejected."
    )
    for line, error in report.rejected[:50]:
        print(f"  line {line}: {error}")
    return EXIT_OK


@_with_repository
def cmd_contacts_list(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    rows = [
        [
            c["id"],
            c["source"],
            c["name"],
            _phone(c["phone_e164"], args.unmask) or f"({c['phone_error']})",
            "yes" if c["do_not_call"] else "",
            c["campaigns"],
            "missing in Notion" if c["missing_in_notion"] else "",
        ]
        for c in repo.list_contacts(campaign_id=args.campaign)
    ]
    _table(rows, ["id", "source", "name", "phone", "do-not-call", "campaigns", "note"])
    return EXIT_OK


@_with_repository
def cmd_jobs_list(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    rows = [
        [
            j["id"],
            j["campaign_id"],
            j["contact_name"],
            _phone(j["phone"], args.unmask),
            j["status"],
            reason_label(j["reason"]),
            j["attempts"],
            "yes" if j["notion_page_id"] else "",
        ]
        for j in repo.list_jobs(campaign_id=args.campaign, status=args.status, limit=args.limit)
    ]
    _table(
        rows, ["id", "campaign", "contact", "phone", "status", "reason", "attempts", "in Notion"]
    )
    return EXIT_OK


@_with_repository
def cmd_dnc_add(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    added = repo.add_do_not_call(args.phone, reason=args.reason or "")
    masked = mask_phone_numbers(args.phone)
    print(f"{masked}: {'added to' if added else 'already on'} the do-not-call list.")
    if settings.notion_enabled:
        print("Notion contacts with this number get «Не звонить» on the next sync.")
    return EXIT_OK


@_with_repository
def cmd_dnc_remove(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    removed, remaining = repo.remove_do_not_call(args.phone, all_sources=args.all)
    masked = mask_phone_numbers(args.phone)
    print(f"{masked}: {removed} entr{'y' if removed == 1 else 'ies'} removed.")
    if remaining:
        print(
            "Still blocked by: " + ", ".join(remaining) + ". A «Не звонить» mark in Notion is "
            "removed in Notion; --all removes every local entry."
        )
    return EXIT_OK


@_with_repository
def cmd_dnc_list(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    rows = [
        [_phone(r["phone_e164"], args.unmask), r["source"], r["reason"], r["created_at"]]
        for r in repo.list_do_not_call()
    ]
    _table(rows, ["phone", "source", "reason", "added (UTC)"])
    return EXIT_OK


@_with_repository
def cmd_simulate(args: argparse.Namespace, settings: Settings, repo: Repository) -> int:
    report = repo.simulate(campaign_id=args.campaign, limit=args.limit)
    print(
        f"Simulated {report.completed} call(s); no real call was placed. "
        f"{report.skipped} job(s) skipped (not callable right now)."
    )
    if settings.notion_enabled:
        print("Results reach Notion on the next sync (wewln notion sync, or wewln serve).")
    return EXIT_OK


# ---------------------------------------------------------------------- telephony and calls

SECRET_NAMES = {"ari-password": ARI_PASSWORD_KEY, "webhook-secret": WEBHOOK_SECRET_KEY}


def cmd_telephony_secret_set(args: argparse.Namespace) -> int:
    key = SECRET_NAMES[args.name]
    if args.generate:
        if args.name != "webhook-secret":
            print("--generate is only for webhook-secret.", file=sys.stderr)
            return EXIT_CONFIG
        value = secrets.token_urlsafe(48)
    elif args.stdin:
        value = sys.stdin.readline().strip()
    else:
        value = getpass.getpass(f"Enter the {args.name} (input stays hidden), then Enter: ").strip()
    try:
        check_secret_format(value)
        if args.name == "webhook-secret" and len(value) < MIN_WEBHOOK_SECRET_LENGTH:
            raise ValueError(f"must be at least {MIN_WEBHOOK_SECRET_LENGTH} characters long")
    except ValueError as exc:
        print(f"The {args.name} was not saved: it {exc}", file=sys.stderr)
        return EXIT_CONFIG
    store = default_store()
    try:
        store.set(key, value)
    except SecretStoreUnavailable as exc:
        env = ENV_PREFIX + key.upper()
        print(f"Not saved: {exc}. Set the {env} environment variable instead.", file=sys.stderr)
        return EXIT_RUNTIME
    print(f"The {args.name} is saved in the OS credential store ({store.name}).")
    if args.generate and args.show:
        print("Copy it into the sender's configuration now; it is not shown again:")
        print(value)
    if any(k.upper() == ENV_PREFIX + key.upper() for k in os.environ):
        print(f"Note: {ENV_PREFIX + key.upper()} is set in the environment and takes precedence.")
    return EXIT_OK


def cmd_telephony_secret_delete(args: argparse.Namespace) -> int:
    store = default_store()
    try:
        deleted = store.delete(SECRET_NAMES[args.name])
    except SecretStoreUnavailable as exc:
        print(f"Credential store unavailable: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    print(f"The {args.name} is deleted." if deleted else f"No {args.name} was stored.")
    return EXIT_OK


def cmd_telephony_secret_status(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    print(f"ari-password: {settings.ari_password_source}")
    print(f"webhook-secret: {settings.webhook_secret_source}")
    return EXIT_OK


def cmd_telephony_check(args: argparse.Namespace) -> int:
    settings = _load(args.env_file)
    if settings is None:
        return EXIT_CONFIG
    _quiet_logging(settings)
    if settings.provider_problems:
        for problem in settings.provider_problems:
            print(f"Problem: {problem}", file=sys.stderr)
        return EXIT_CONFIG
    provider = create_provider(settings)
    result = asyncio.run(provider.check())
    for key, value in result.items():
        print(f"  {key}: {value}")
    decision = evaluate_real_calls(settings)
    blocked = ", ".join(r.value for r in decision.reasons)
    print(
        f"  real_calls_allowed: {decision.allowed}"
        + (f" (blocked by: {blocked})" if blocked else "")
    )
    if result.get("status") != "ok":
        print("Telephony check failed; see docs/ASTERISK.md (troubleshooting).", file=sys.stderr)
        return EXIT_RUNTIME
    print("Telephony check passed.")
    return EXIT_OK


def _calls_settings(args: argparse.Namespace, *, dialing: bool = False) -> Settings | None:
    """Settings for the calls commands. Only dialing needs a working provider configuration:
    the emergency stop, cancel and the lists always work."""
    settings = _load(args.env_file)
    if settings is None:
        return None
    problems = [*settings.provider_problems, *settings.voice_problems] if dialing else []
    if problems:
        for problem in problems:
            print(f"Problem: {problem}", file=sys.stderr)
        return None
    _quiet_logging(settings)
    return settings


def _store(settings: Settings) -> CallStore:
    db = Database(settings.resolved_db_path)
    db.migrate()
    return create_call_store(settings, db)


def _service(args: argparse.Namespace, settings: Settings, store: CallStore) -> CallService:
    scenario = getattr(args, "scenario", None)
    if scenario and settings.telephony_provider != "mock":
        raise WewlnError("--scenario works only with the mock provider", code="invalid_input")
    voice = None
    caller = None
    if settings.voice_enabled:
        from .voice.runtime import create_voice_runtime
        from .voice.simulate import simulated_person

        voice = create_voice_runtime(settings)
        if settings.telephony_provider == "mock":
            # A simulated person answers mock calls: the conversation really runs.
            caller = simulated_person(voice.tts, frame_s=voice.config.frame_s)
    provider = create_provider(
        settings,
        scenario=scenario,
        time_scale=0.0 if getattr(args, "fast", False) else 1.0,
        caller=caller,
    )
    return CallService(settings, store, provider, voice=voice)


@contextlib.asynccontextmanager
async def _dialer(
    args: argparse.Namespace, settings: Settings, store: CallStore
) -> AsyncIterator[CallService]:
    """The call service for one command; the voice engine (if on) is unloaded after it."""
    service = _service(args, settings, store)
    try:
        async with service:
            yield service
    finally:
        if service.voice is not None:
            await service.voice.close()


def _outcome_line(outcome: CallOutcome) -> str:
    cause = f", cause {outcome.cause}" if outcome.cause is not None else ""
    error = f", error {outcome.error_code}" if outcome.error_code else ""
    sim = " [simulated]" if outcome.simulated else ""
    subject = f"job {outcome.job_id}" if outcome.job_id else f"test call {outcome.attempt_id}"
    return (
        f"  {subject}: {outcome.state.value} ({STATE_LABELS[outcome.state]})"
        f" - {outcome.end_reason or ''}{cause}{error}{sim}"
    )


def _blocked(exc: RealCallsBlockedError) -> int:
    print(f"Blocked: {exc.message}", file=sys.stderr)
    blocked = exc.details.get("blocked_by") or []
    hints = {
        "simulation_mode": "WEWLN_MODE=production and WEWLN_PRODUCTION_CONFIRM are required",
        "production_not_confirmed": "set WEWLN_PRODUCTION_CONFIRM=I_ACCEPT_REAL_CALLS",
        "kill_switch_engaged": "the emergency stop is on (wewln calls resume)",
        "provider_simulation_only": "the mock provider never places real calls",
        "provider_not_configured": "configure the provider (docs/ASTERISK.md)",
        "call_regions_not_set": "set WEWLN_CALL_REGIONS, e.g. AZ or RU",
        "operator_not_armed": "a real call needs a permit: wewln calls permit --to <number>",
    }
    for reason in blocked:
        print(f"  - {reason}: {hints.get(reason, '')}", file=sys.stderr)
    return EXIT_RUNTIME


def _precheck_real(settings: Settings, store: CallStore, *, armed: bool) -> None:
    """The real-call gate before connecting to the provider (the service checks it again)."""
    decision = evaluate_real_calls(
        settings, operator_armed=armed, kill_switch_engaged=store.kill_switch() is not None
    )
    if not decision.allowed:
        raise RealCallsBlockedError(
            "Real calls are blocked: " + ", ".join(r.value for r in decision.reasons),
            details=decision.as_dict(),
        )


def cmd_calls_run(args: argparse.Namespace) -> int:
    settings = _calls_settings(args, dialing=True)
    if settings is None:
        return EXIT_CONFIG
    store = _store(settings)

    async def run() -> Any:
        async with _dialer(args, settings, store) as service:
            return await service.run_jobs(limit=args.limit, campaign_id=args.campaign)

    try:
        if PROVIDERS[settings.telephony_provider].places_real_calls:
            print(
                "Campaign calls over a real provider are not enabled in this version "
                "(Phase 6 adds arming, schedules and limits). Use the mock provider, or "
                "`wewln calls test` for a single test call.",
                file=sys.stderr,
            )
            _precheck_real(settings, store, armed=False)
        _print_call_path(settings)
        report = asyncio.run(run())
    except RealCallsBlockedError as exc:
        return _blocked(exc)
    except KeyboardInterrupt:
        print("Interrupted: calls in progress were ended; see `wewln calls list`.", file=sys.stderr)
        return 130
    simulated = all(o.simulated for o in report.outcomes)
    print(
        f"Calls placed: {report.placed}"
        + (" (mock provider: simulated, no real call)." if simulated else ".")
    )
    for outcome in report.outcomes:
        print(_outcome_line(outcome))
    if report.stopped_by:
        print(
            f"Stopped early: {report.stopped_by}" + (f" ({report.error})" if report.error else "")
        )
    if settings.notion_enabled:
        print("Results reach Notion on the next sync (wewln notion sync, or wewln serve).")
    return EXIT_OK if report.stopped_by in (None, "kill_switch") else EXIT_RUNTIME


def _number(settings: Settings, raw: str) -> str | None:
    normalized = normalize_phone(raw, settings.default_phone_region)
    if normalized.e164 is None:
        print(f"The number is rejected ({normalized.error}).", file=sys.stderr)
        return None
    return normalized.e164


def cmd_calls_test(args: argparse.Namespace) -> int:
    settings = _calls_settings(args, dialing=True)
    if settings is None:
        return EXIT_CONFIG
    to = None
    if not args.softphone:
        to = _number(settings, args.to)
        if to is None:
            return EXIT_CONFIG
    store = _store(settings)
    if to is not None and PROVIDERS[settings.telephony_provider].places_real_calls:
        try:
            _precheck_real(settings, store, armed=store.has_valid_permit(to))
        except RealCallsBlockedError as exc:
            return _blocked(exc)

    async def run() -> tuple[CallOutcome, Any]:
        async with _dialer(args, settings, store) as service:
            if args.softphone:
                print(
                    f"Test call to the local softphone {settings.asterisk_softphone_endpoint} "
                    "(no phone network)" + (" [simulated]" if service.simulated else "") + "..."
                )
            else:
                kind = "simulated" if service.simulated else "REAL"
                print(f"{kind} test call to {mask_phone_numbers(to or '')}...")
            outcome = await service.test_call(to_e164=to, softphone=args.softphone)
            return outcome, service.conversations.get(outcome.attempt_id)

    _print_call_path(settings, softphone=args.softphone)
    if settings.voice_enabled:
        print("Loading the voice engine (the models stay loaded during the call)...")
    try:
        outcome, conversation = asyncio.run(run())
    except RealCallsBlockedError as exc:
        return _blocked(exc)
    except KeyboardInterrupt:
        print("Interrupted: the call was ended; see `wewln calls list`.", file=sys.stderr)
        return 130
    print(_outcome_line(outcome))
    if conversation is not None:
        from .cli_voice import print_conversation

        print_conversation(conversation)
        print(f"Transcript later: wewln calls transcript {outcome.attempt_id}")
    return EXIT_OK


def cmd_calls_permit(args: argparse.Namespace) -> int:
    settings = _calls_settings(args)
    if settings is None:
        return EXIT_CONFIG
    to = _number(settings, args.to)
    if to is None:
        return EXIT_CONFIG
    problem = dial_policy_problem(to, settings.allowed_call_regions)
    if problem is not None:
        print(f"This number may not be called ({problem}).", file=sys.stderr)
        return EXIT_CONFIG
    info = describe_number(to)
    masked = mask_phone_numbers(to)
    print(
        f"Permit ONE real call to {masked} (country {info.region if info else '?'}, "
        f"type {info.kind if info else '?'}) within {args.minutes} minutes."
    )
    print("The call is real: it reaches a phone and may cost money at your SIP provider.")
    confirm = args.confirm
    if confirm is None:
        confirm = input("Type the last 4 digits of the number to confirm: ").strip()
    if confirm != to[-4:]:
        print("Not confirmed; no permit was created.", file=sys.stderr)
        return EXIT_RUNTIME
    store = _store(settings)
    permit = store.create_permit(to, minutes=args.minutes, created_by="cli")
    print(f"Permit {permit['id']} is valid until {permit['expires_at']} (UTC), for one call.")
    decision = evaluate_real_calls(
        settings, operator_armed=True, kill_switch_engaged=store.kill_switch() is not None
    )
    if decision.allowed:
        print("Everything else is ready: wewln calls test --to <the number>")
    else:
        print("Still blocked by: " + ", ".join(r.value for r in decision.reasons))
    return EXIT_OK


def cmd_calls_permits(args: argparse.Namespace) -> int:
    settings = _calls_settings(args)
    if settings is None:
        return EXIT_CONFIG
    store = _store(settings)
    if args.revoke:
        print(f"Revoked {store.revoke_permits()} permit(s).")
        return EXIT_OK
    rows = [
        [
            p["id"],
            _phone(p["phone_e164"], args.unmask),
            f"{p['used']}/{p['max_calls']}",
            p["expires_at"],
            "yes" if p["valid"] else "",
        ]
        for p in store.list_permits()
    ]
    _table(rows, ["id", "phone", "used", "expires (UTC)", "valid"])
    return EXIT_OK


def cmd_calls_list(args: argparse.Namespace) -> int:
    settings = _calls_settings(args)
    if settings is None:
        return EXIT_CONFIG
    store = _store(settings)
    rows = [
        [
            a["id"],
            a["kind"],
            a["campaign_name"] or "",
            a["phone_masked"] or a["target"],
            STATE_LABELS[CallState(a["state"])],
            a["end_reason"] or "",
            a["hangup_cause"] if a["hangup_cause"] is not None else "",
            "yes" if a["simulated"] else "",
            a["created_at"],
        ]
        for a in store.list_attempts(limit=args.limit, active_only=args.active)
    ]
    _table(
        rows,
        ["id", "kind", "campaign", "phone", "state", "reason", "cause", "simulated", "created"],
    )
    kill = store.kill_switch()
    if kill:
        print(f"Emergency stop is ON (since {kill['updated_at']}, by {kill['source']}).")
    return EXIT_OK


def cmd_calls_cancel(args: argparse.Namespace) -> int:
    settings = _calls_settings(args)
    if settings is None:
        return EXIT_CONFIG
    store = _store(settings)
    store.get_attempt(args.attempt_id)
    if store.request_cancel(args.attempt_id):
        print("Cancel requested: the running dialer ends the call within a few seconds.")
    else:
        print("The call is not in progress.")
    return EXIT_OK


def cmd_calls_stop_all(args: argparse.Namespace) -> int:
    settings = _calls_settings(args)
    if settings is None:
        return EXIT_CONFIG
    store = _store(settings)
    engaged = store.engage_kill_switch(source="cli", reason=args.reason or "")
    active = len(store.list_attempts(active_only=True))
    print(
        "Emergency stop " + ("engaged" if engaged else "was already engaged") + f": {active} "
        "call(s) in progress are ended by the dialer; nothing is dialed until `wewln calls resume`."
    )
    return EXIT_OK


def cmd_calls_resume(args: argparse.Namespace) -> int:
    settings = _calls_settings(args)
    if settings is None:
        return EXIT_CONFIG
    store = _store(settings)
    released = store.release_kill_switch(source="cli")
    print("Emergency stop released." if released else "The emergency stop was not engaged.")
    if settings.kill_switch:
        print("Note: WEWLN_KILL_SWITCH=true still blocks every call.")
    return EXIT_OK


# ---------------------------------------------------------------------- parser


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
    serve = sub.add_parser("serve", help="run the local HTTP API and the Notion sync")
    serve.add_argument("--host", help="override WEWLN_HTTP_HOST")
    serve.add_argument("--port", type=int, help="override WEWLN_HTTP_PORT")
    serve.set_defaults(func=cmd_serve)
    check = sub.add_parser("check-config", help="validate and print the settings without secrets")
    check.set_defaults(func=cmd_check_config)
    init_db = sub.add_parser("init-db", help="create or upgrade the SQLite database")
    init_db.set_defaults(func=cmd_init_db)

    notion = sub.add_parser("notion", help="Notion connection, databases and sync")
    notion_sub = notion.add_subparsers(dest="notion_command", required=True)
    token = notion_sub.add_parser("token", help="store the Notion token in the OS credential store")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_set = token_sub.add_parser("set", help="save the token (asked without echo)")
    token_set.add_argument("--stdin", action="store_true", help="read the token from stdin")
    token_set.set_defaults(func=cmd_notion_token_set)
    token_sub.add_parser("delete", help="remove the stored token").set_defaults(
        func=cmd_notion_token_delete
    )
    token_sub.add_parser("status", help="where the token comes from (never shows it)").set_defaults(
        func=cmd_notion_token_status
    )
    n_check = notion_sub.add_parser("check", help="check the token and the four databases")
    n_check.add_argument("--repair", action="store_true", help="add missing properties and options")
    n_check.set_defaults(func=cmd_notion_check)
    n_boot = notion_sub.add_parser("bootstrap", help="create the four databases under a page")
    n_boot.add_argument("--parent", required=True, help="link or ID of a page shared with wewln")
    n_boot.add_argument(
        "--write-env", action="store_true", help="save the database IDs to the .env file"
    )
    n_boot.set_defaults(func=cmd_notion_bootstrap)
    notion_sub.add_parser("sync", help="run one sync cycle now").set_defaults(func=cmd_notion_sync)
    n_status = notion_sub.add_parser("status", help="latest sync runs")
    n_status.add_argument("--limit", type=int, default=10)
    n_status.set_defaults(func=cmd_notion_status)
    n_mock = notion_sub.add_parser("mock-server", help="run a local imitation of the Notion API")
    n_mock.add_argument("--port", type=int, default=8799)
    n_mock.add_argument("--demo", action="store_true", help="create databases and demo data")
    n_mock.set_defaults(func=cmd_notion_mock_server)

    campaign = sub.add_parser("campaign", help="local campaigns (Notion campaigns: in Notion)")
    campaign_sub = campaign.add_subparsers(dest="campaign_command", required=True)
    c_create = campaign_sub.add_parser("create", help="create a local campaign (draft)")
    c_create.add_argument("name")
    c_create.add_argument("--script", help="conversation script text")
    c_create.set_defaults(func=cmd_campaign_create)
    campaign_sub.add_parser("list", help="all campaigns").set_defaults(func=cmd_campaign_list)
    for name, status in (("start", CampaignStatus.RUNNING), ("pause", CampaignStatus.PAUSED)):
        c_status = campaign_sub.add_parser(name, help=f"{name} a local campaign")
        c_status.add_argument("campaign_id", type=int)
        c_status.set_defaults(func=_campaign_status(status))
    c_stop = campaign_sub.add_parser("stop", help="stop any campaign for good (works offline)")
    c_stop.add_argument("campaign_id", type=int)
    c_stop.add_argument("--reason", help="why it is stopped")
    c_stop.set_defaults(func=cmd_campaign_stop)

    contacts = sub.add_parser("contacts", help="local contacts")
    contacts_sub = contacts.add_subparsers(dest="contacts_command", required=True)
    k_import = contacts_sub.add_parser("import", help="import a CSV file (name, phone, dnc)")
    k_import.add_argument("file")
    k_import.add_argument("--campaign", type=int, help="add the contacts to this local campaign")
    k_import.set_defaults(func=cmd_contacts_import)
    k_list = contacts_sub.add_parser("list", help="contacts (phone numbers masked)")
    k_list.add_argument("--campaign", type=int)
    k_list.add_argument("--unmask", action="store_true", help="show full phone numbers")
    k_list.set_defaults(func=cmd_contacts_list)

    jobs = sub.add_parser("jobs", help="call jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    j_list = jobs_sub.add_parser("list", help="jobs with status and reason")
    j_list.add_argument("--campaign", type=int)
    j_list.add_argument(
        "--status", choices=["queued", "blocked", "in_progress", "done", "failed", "cancelled"]
    )
    j_list.add_argument("--limit", type=int, default=500)
    j_list.add_argument("--unmask", action="store_true", help="show full phone numbers")
    j_list.set_defaults(func=cmd_jobs_list)

    dnc = sub.add_parser("dnc", help="do-not-call list")
    dnc_sub = dnc.add_subparsers(dest="dnc_command", required=True)
    d_add = dnc_sub.add_parser("add", help="never call this number")
    d_add.add_argument("phone")
    d_add.add_argument("--reason")
    d_add.set_defaults(func=cmd_dnc_add)
    d_remove = dnc_sub.add_parser("remove", help="remove a manual entry")
    d_remove.add_argument("phone")
    d_remove.add_argument("--all", action="store_true", help="remove every local entry")
    d_remove.set_defaults(func=cmd_dnc_remove)
    d_list = dnc_sub.add_parser("list", help="the list (phone numbers masked)")
    d_list.add_argument("--unmask", action="store_true", help="show full phone numbers")
    d_list.set_defaults(func=cmd_dnc_list)

    telephony = sub.add_parser("telephony", help="telephony provider: check and secrets")
    t_sub = telephony.add_subparsers(dest="telephony_command", required=True)
    t_sub.add_parser("check", help="check the provider (Asterisk: ARI, app, trunk)").set_defaults(
        func=cmd_telephony_check
    )
    t_secret = t_sub.add_parser("secret", help="ARI password / webhook secret in the OS store")
    ts_sub = t_secret.add_subparsers(dest="secret_command", required=True)
    ts_set = ts_sub.add_parser("set", help="save a secret (asked without echo)")
    ts_set.add_argument("name", choices=sorted(SECRET_NAMES))
    ts_set.add_argument("--stdin", action="store_true", help="read the value from stdin")
    ts_set.add_argument("--generate", action="store_true", help="webhook-secret: generate one")
    ts_set.add_argument("--show", action="store_true", help="with --generate: print it once")
    ts_set.set_defaults(func=cmd_telephony_secret_set)
    ts_delete = ts_sub.add_parser("delete", help="remove a stored secret")
    ts_delete.add_argument("name", choices=sorted(SECRET_NAMES))
    ts_delete.set_defaults(func=cmd_telephony_secret_delete)
    ts_sub.add_parser("status", help="where each secret comes from (never shows it)").set_defaults(
        func=cmd_telephony_secret_status
    )

    calls = sub.add_parser("calls", help="dial, watch, cancel and stop calls")
    calls_sub = calls.add_subparsers(dest="calls_command", required=True)
    l_run = calls_sub.add_parser("run", help="call queued jobs one at a time (mock: simulated)")
    l_run.add_argument("--campaign", type=int)
    l_run.add_argument("--limit", type=int, default=10)
    l_run.add_argument("--scenario", choices=MOCK_SCENARIOS, help="mock provider scenario")
    l_run.add_argument("--fast", action="store_true", help="mock provider: no waiting")
    l_run.set_defaults(func=cmd_calls_run)
    l_test = calls_sub.add_parser("test", help="one test call to a number or the softphone")
    target = l_test.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", help="phone number (+994..., +7...)")
    target.add_argument(
        "--softphone", action="store_true", help="call the local softphone (no phone network)"
    )
    l_test.add_argument("--scenario", choices=MOCK_SCENARIOS, help="mock provider scenario")
    l_test.add_argument("--fast", action="store_true", help="mock provider: no waiting")
    l_test.set_defaults(func=cmd_calls_test)
    l_permit = calls_sub.add_parser("permit", help="allow ONE real test call to a number")
    l_permit.add_argument("--to", required=True, help="phone number (+994..., +7...)")
    l_permit.add_argument("--minutes", type=int, default=15, help="validity, 1-60 (default 15)")
    l_permit.add_argument("--confirm", help="the last 4 digits (instead of the prompt)")
    l_permit.set_defaults(func=cmd_calls_permit)
    l_permits = calls_sub.add_parser("permits", help="real-call permits")
    l_permits.add_argument("--revoke", action="store_true", help="revoke every permit in force")
    l_permits.add_argument("--unmask", action="store_true", help="show full phone numbers")
    l_permits.set_defaults(func=cmd_calls_permits)
    l_list = calls_sub.add_parser("list", help="recent calls (phone numbers masked)")
    l_list.add_argument("--active", action="store_true", help="only calls in progress")
    l_list.add_argument("--limit", type=int, default=30)
    l_list.set_defaults(func=cmd_calls_list)
    l_cancel = calls_sub.add_parser("cancel", help="end one call in progress")
    l_cancel.add_argument("attempt_id", type=int)
    l_cancel.set_defaults(func=cmd_calls_cancel)
    l_stop = calls_sub.add_parser("stop-all", help="EMERGENCY STOP: end all calls, dial nothing")
    l_stop.add_argument("--reason")
    l_stop.set_defaults(func=cmd_calls_stop_all)
    calls_sub.add_parser("resume", help="release the emergency stop").set_defaults(
        func=cmd_calls_resume
    )

    simulate = sub.add_parser("simulate", help="complete queued jobs with simulated calls")
    simulate.add_argument("--campaign", type=int)
    simulate.add_argument("--limit", type=int, default=10)
    simulate.set_defaults(func=cmd_simulate)

    from .cli_voice import register

    register(sub, calls_sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except WewlnError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        return EXIT_RUNTIME
    except ProviderError as exc:
        print(f"Telephony error ({exc.code}): {exc.message}", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        if args.func is not cmd_serve:
            shutdown_logging()
