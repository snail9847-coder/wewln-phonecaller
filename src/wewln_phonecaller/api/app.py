"""Local HTTP API (aiohttp): health and readiness probes, request IDs, JSON errors, token auth."""

from __future__ import annotations

import asyncio
import hmac
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
from ..safety import evaluate_real_calls
from ..storage import Database
from ..telephony import PROVIDERS

log = logging.getLogger("wewln.api")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
PUBLIC_PATHS = frozenset({"/health", "/ready"})
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
    if request.path in PUBLIC_PATHS:
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
    notion = (
        {"status": "configured", "connectivity": "not_checked"}
        if settings.notion_enabled
        else {"status": "disabled"}
    )
    checks = {
        "config": {"status": "ok"},
        "database": database,
        "data_dir": data_dir,
        "notion": notion,
        "telephony": {
            "status": "ok",
            "provider": provider.name,
            "places_real_calls": provider.places_real_calls,
        },
        "real_calls": evaluate_real_calls(settings).as_dict(),
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


def create_app(settings: Settings, db: Database) -> web.Application:
    app = web.Application(
        middlewares=[context_middleware, auth_middleware], client_max_size=1024 * 1024
    )
    app[SETTINGS_KEY] = settings
    app[DB_KEY] = db
    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    app.router.add_get("/api/v1/info", info)
    return app
