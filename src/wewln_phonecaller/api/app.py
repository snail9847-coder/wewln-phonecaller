"""Local HTTP API (aiohttp): probes, request IDs, JSON errors, token auth, campaign control.

The background Notion sync (when enabled) runs inside this application; Notion problems never
make the backend "not ready", because everything works offline and is synced later.

Calls are dialed by `wewln calls ...` (one dialer at a time); the API lists them, cancels one,
engages the emergency stop and accepts signed call-status webhooks. The running dialer picks
cancels and the emergency stop up from the database within a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from .. import __version__
from ..config import Settings
from ..errors import AuthError, WewlnError
from ..logging_setup import request_id_var
from ..notion.sync import SyncService
from ..redact import mask_phone_numbers
from ..safety import evaluate_real_calls
from ..storage import Database
from ..storage.calls import CallStore
from ..storage.repository import Repository
from ..telephony import PROVIDERS
from ..telephony.factory import create_repository
from ..telephony.service import DIALER_LOCK
from ..telephony.webhooks import MAX_BODY_BYTES, parse_webhook_event, verify_webhook

log = logging.getLogger("wewln.api")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
PUBLIC_PATHS = frozenset({"/health", "/ready"})
# Authenticated by its HMAC signature instead of the bearer token (see telephony/webhooks.py).
WEBHOOK_PATH = "/api/v1/telephony/events"
_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
}

SETTINGS_KEY = web.AppKey("settings", Settings)
DB_KEY = web.AppKey("db", Database)
REPO_KEY = web.AppKey("repo", Repository)
CALLS_KEY = web.AppKey("calls", CallStore)
SYNC_KEY = web.AppKey("notion_sync", SyncService)
MAX_REASON_LENGTH = 500


def _error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, Any] | None = None,
) -> web.Response:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        error["details"] = details
    headers = {REQUEST_ID_HEADER: request_id}
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="wewln"'
    return web.json_response({"error": error}, status=status, headers=headers)


@web.middleware
async def context_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Correlation ID for every request, structured access log, JSON errors without internals."""
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    status = 500
    try:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            status = exc.status
            if exc.status < 400:
                raise
            code = _HTTP_CODES.get(exc.status, "http_error")
            return _error_response(exc.status, code, exc.reason, request_id)
        except WewlnError as exc:
            status = exc.http_status
            return _error_response(exc.http_status, exc.code, exc.message, request_id, exc.details)
        except Exception:
            log.exception("unhandled_error", extra={"path": request.path})
            return _error_response(500, "internal_error", "Internal server error", request_id)
        status = response.status
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        level = logging.DEBUG if request.path in PUBLIC_PATHS else logging.INFO
        log.log(
            level,
            "http_request",
            extra={
                "method": request.method,
                "path": request.path,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        request_id_var.reset(token)


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Everything except the probes needs `Authorization: Bearer <WEWLN_API_TOKEN>`.

    Without a configured token the protected endpoints stay closed (fail closed).
    """
    if request.path in PUBLIC_PATHS or request.path == WEBHOOK_PATH:
        return await handler(request)
    expected = request.app[SETTINGS_KEY].api_token
    if expected is None:
        raise AuthError(
            "Protected endpoints are disabled until WEWLN_API_TOKEN is set",
            code="auth_not_configured",
        )
    scheme, _, supplied = request.headers.get("Authorization", "").partition(" ")
    valid = scheme.lower() == "bearer" and hmac.compare_digest(
        supplied.strip().encode(), expected.get_secret_value().encode()
    )
    if not valid:
        raise AuthError("Missing or invalid bearer token")
    return await handler(request)


def _check_data_dir(path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".ready-", dir=path)
        os.close(fd)
        os.remove(name)
    except OSError as exc:
        return {"status": "error", "error": type(exc).__name__}
    return {"status": "ok"}


async def health(request: web.Request) -> web.Response:
    """Liveness: the process runs and answers."""
    settings = request.app[SETTINGS_KEY]
    return web.json_response(
        {
            "status": "ok",
            "service": "wewln-phonecaller",
            "version": __version__,
            "mode": settings.mode,
        }
    )


async def ready(request: web.Request) -> web.Response:
    """Readiness: storage works. Also reports Notion, telephony and the real-call gate."""
    settings = request.app[SETTINGS_KEY]
    db = request.app[DB_KEY]
    database, data_dir = await asyncio.gather(
        asyncio.to_thread(db.check), asyncio.to_thread(_check_data_dir, settings.data_dir)
    )
    provider = PROVIDERS[settings.telephony_provider]
    kill_switch = None
    if database["status"] == "ok":
        with contextlib.suppress(WewlnError):
            kill_switch = await asyncio.to_thread(request.app[CALLS_KEY].kill_switch)
    telephony: dict[str, Any] = {
        "status": "ok",
        "provider": provider.name,
        "places_real_calls": provider.places_real_calls,
    }
    if settings.telephony_provider == "asterisk":
        problems = settings.provider_problems
        telephony["status"] = "error" if problems else "ok"
        telephony["configured"] = not problems
        telephony["ari_url"] = settings.ari_url
    if kill_switch is not None:
        telephony["emergency_stop"] = True
    if not settings.notion_enabled:
        notion: dict[str, Any] = {"status": "disabled"}
    elif SYNC_KEY in request.app:
        notion = request.app[SYNC_KEY].status()
    else:
        notion = {"status": "configured", "sync": "not_running"}
    checks = {
        "config": {"status": "ok"},
        "database": database,
        "data_dir": data_dir,
        "notion": notion,
        "telephony": telephony,
        "real_calls": evaluate_real_calls(
            settings, kill_switch_engaged=kill_switch is not None
        ).as_dict(),
    }
    is_ready = database["status"] == "ok" and data_dir["status"] == "ok"
    return web.json_response(
        {"status": "ready" if is_ready else "not_ready", "mode": settings.mode, "checks": checks},
        status=200 if is_ready else 503,
    )


async def info(request: web.Request) -> web.Response:
    """Protected: version, redacted settings and the real-call gate."""
    settings = request.app[SETTINGS_KEY]
    return web.json_response(
        {
            "service": "wewln-phonecaller",
            "version": __version__,
            "settings": settings.redacted_summary(),
            "real_calls": evaluate_real_calls(settings).as_dict(),
        }
    )


async def _json_body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WewlnError(
            "The request body is not valid JSON", code="invalid_json", http_status=400
        ) from None
    if not isinstance(body, dict):
        raise WewlnError(
            "The request body must be a JSON object", code="invalid_json", http_status=400
        )
    return body


def _text_field(body: dict[str, Any], name: str, *, required: bool = False) -> str:
    value = body.get(name, "")
    if not isinstance(value, str) or len(value) > MAX_REASON_LENGTH or (required and not value):
        raise WewlnError(
            f"'{name}' must be a string of at most {MAX_REASON_LENGTH} characters"
            + (" (required)" if required else ""),
            code="invalid_input",
            http_status=400,
        )
    return value


def _wake_sync(app: web.Application) -> None:
    if SYNC_KEY in app:
        app[SYNC_KEY].wake()


async def notion_status(request: web.Request) -> web.Response:
    """Protected: state of the background sync and the latest sync runs."""
    settings = request.app[SETTINGS_KEY]
    runs = await asyncio.to_thread(request.app[REPO_KEY].recent_sync_runs, 10)
    service = request.app[SYNC_KEY].status() if SYNC_KEY in request.app else None
    return web.json_response(
        {"enabled": settings.notion_enabled, "service": service, "recent_runs": runs}
    )


async def notion_sync_now(request: web.Request) -> web.Response:
    """Protected: run one sync cycle now and return its result."""
    if SYNC_KEY not in request.app:
        raise WewlnError(
            "Notion sync is not enabled (WEWLN_NOTION_ENABLED=true)",
            code="notion_disabled",
            http_status=409,
        )
    sync = request.app[SYNC_KEY].sync
    if sync.running:
        raise WewlnError("A Notion sync is already running", code="sync_running", http_status=409)
    result = await sync.run_once()
    return web.json_response(result.as_dict(), status=409 if result.status == "skipped" else 200)


async def campaigns_list(request: web.Request) -> web.Response:
    campaigns = await asyncio.to_thread(request.app[REPO_KEY].list_campaigns)
    return web.json_response({"campaigns": campaigns})


async def campaign_stop(request: web.Request) -> web.Response:
    """Protected: stop a campaign. Final, works offline, and the next sync tells Notion."""
    repo = request.app[REPO_KEY]
    campaign_id = int(request.match_info["campaign_id"])
    reason = _text_field(await _json_body(request), "reason")
    report = await asyncio.to_thread(repo.stop_campaign, campaign_id, reason=reason, source="local")
    campaign = await asyncio.to_thread(repo.get_campaign, campaign_id)
    _wake_sync(request.app)
    return web.json_response(
        {
            "campaign": {"id": campaign["id"], "status": campaign["status"]},
            "jobs_changed": report.changed,
        }
    )


async def dnc_add(request: web.Request) -> web.Response:
    """Protected: put a number on the do-not-call list (it is never called again)."""
    body = await _json_body(request)
    phone = _text_field(body, "phone", required=True)
    reason = _text_field(body, "reason")
    added = await asyncio.to_thread(request.app[REPO_KEY].add_do_not_call, phone, reason=reason)
    _wake_sync(request.app)
    return web.json_response({"added": added, "phone": mask_phone_numbers(phone)})


def _attempt_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "job_id": row["job_id"],
        "attempt": row["attempt"],
        "provider": row["provider"],
        "simulated": bool(row["simulated"]),
        "target": row["target"],
        "phone": row["phone_masked"],
        "state": row["state"],
        "end_reason": row["end_reason"],
        "hangup_cause": row["hangup_cause"],
        "error_code": row["error_code"],
        "campaign": row.get("campaign_name"),
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
        "ended_at": row["ended_at"],
    }


async def calls_list(request: web.Request) -> web.Response:
    """Protected: recent call attempts (numbers masked), the emergency stop and the dialer."""
    store = request.app[CALLS_KEY]
    active_only = request.query.get("active", "") in {"1", "true", "yes"}
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        raise WewlnError(
            "'limit' must be a number", code="invalid_input", http_status=400
        ) from None
    rows = await asyncio.to_thread(store.list_attempts, limit=limit, active_only=active_only)
    kill_switch = await asyncio.to_thread(store.kill_switch)
    dialer = await asyncio.to_thread(store.db.lock_holder, DIALER_LOCK)
    return web.json_response(
        {
            "calls": [_attempt_view(r) for r in rows],
            "emergency_stop": kill_switch,
            "dialer_running": dialer is not None,
        }
    )


async def call_cancel(request: web.Request) -> web.Response:
    """Protected: ask the running dialer to end one call."""
    store = request.app[CALLS_KEY]
    attempt_id = int(request.match_info["attempt_id"])
    await asyncio.to_thread(store.get_attempt, attempt_id)  # 404 for an unknown call
    requested = await asyncio.to_thread(store.request_cancel, attempt_id, actor="api")
    dialer = await asyncio.to_thread(store.db.lock_holder, DIALER_LOCK)
    return web.json_response({"cancel_requested": requested, "dialer_running": dialer is not None})


async def calls_stop_all(request: web.Request) -> web.Response:
    """Protected: the emergency stop. Every call is ended and nothing is dialed until
    `wewln calls resume` (deliberately not available over the API)."""
    store = request.app[CALLS_KEY]
    reason = _text_field(await _json_body(request), "reason")
    engaged = await asyncio.to_thread(store.engage_kill_switch, source="api", reason=reason)
    active = await asyncio.to_thread(store.list_attempts, active_only=True)
    dialer = await asyncio.to_thread(store.db.lock_holder, DIALER_LOCK)
    return web.json_response(
        {
            "emergency_stop": "engaged" if engaged else "already_engaged",
            "active_calls": len(active),
            "dialer_running": dialer is not None,
        }
    )


async def telephony_events(request: web.Request) -> web.Response:
    """Signed call-status events (HMAC, see telephony/webhooks.py). Each event applies once."""
    settings = request.app[SETTINGS_KEY]
    if settings.webhook_secret is None:
        raise web.HTTPNotFound()
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_BODY_BYTES, actual_size=request.content_length
        )
    body = await request.content.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY_BYTES, actual_size=len(body))
    verify_webhook(
        settings.webhook_secret.get_secret_value(),
        request.headers,
        body,
        now=time.time(),
        tolerance_s=settings.webhook_tolerance,
    )
    event = parse_webhook_event(body)
    outcome = await asyncio.to_thread(request.app[CALLS_KEY].apply_event, event)
    log.info("webhook_event", extra={"kind": event.kind.value, "outcome": outcome})
    return web.json_response({"result": outcome})


def create_app(
    settings: Settings, db: Database, *, sync_service: SyncService | None = None
) -> web.Application:
    app = web.Application(
        middlewares=[context_middleware, auth_middleware], client_max_size=1024 * 1024
    )
    app[SETTINGS_KEY] = settings
    app[DB_KEY] = db
    app[REPO_KEY] = create_repository(settings, db)
    app[CALLS_KEY] = CallStore(app[REPO_KEY], retry_delay_s=settings.call_retry_delay)
    if sync_service is not None:
        app[SYNC_KEY] = sync_service

        async def _start_sync(_app: web.Application) -> None:
            sync_service.start()

        async def _stop_sync(_app: web.Application) -> None:
            with contextlib.suppress(Exception):
                await sync_service.stop()

        app.on_startup.append(_start_sync)
        app.on_cleanup.append(_stop_sync)
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/api/v1/info", info)
    app.router.add_get("/api/v1/notion/status", notion_status)
    app.router.add_post("/api/v1/notion/sync", notion_sync_now)
    app.router.add_get("/api/v1/campaigns", campaigns_list)
    app.router.add_post(r"/api/v1/campaigns/{campaign_id:\d+}/stop", campaign_stop)
    app.router.add_post("/api/v1/dnc", dnc_add)
    app.router.add_get("/api/v1/calls", calls_list)
    app.router.add_post(r"/api/v1/calls/{attempt_id:\d+}/cancel", call_cancel)
    app.router.add_post("/api/v1/calls/stop-all", calls_stop_all)
    app.router.add_post(WEBHOOK_PATH, telephony_events)
    return app
