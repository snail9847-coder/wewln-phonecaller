"""Async client for the official Notion REST API (https://api.notion.com, version 2026-03-11).

- Every request carries `Notion-Version`; the token is sent only in the Authorization header and
  is never logged. Request and response bodies are never logged either (personal data).
- A client-side rate limiter spaces requests (Notion allows about 3 requests per second on
  average; data source queries have their own limit), and `Retry-After` pauses all requests.
- Retries follow Notion's guidance: 429/529 for every method (unless access was blocked),
  409 conflicts, and 5xx/network errors only for idempotent requests. A failed write that may
  have been saved raises NotionUncertainWriteError (with `committed_resource_id` when known) so
  the caller can look the object up instead of creating a duplicate.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from yarl import URL

from .. import __version__
from ..errors import WewlnError
from ..redact import redact

log = logging.getLogger("wewln.notion")

NOTION_VERSION = "2026-03-11"
DEFAULT_BASE_URL = "https://api.notion.com"
MAX_PAGE_SIZE = 100
_IDEMPOTENT_METHODS = frozenset({"GET", "DELETE"})


class NotionError(WewlnError):
    """A Notion API call failed. `notion_code` is Notion's stable error code when known."""

    code = "notion_error"
    http_status = 502
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        notion_code: str | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        committed_resource_id: str | None = None,
    ) -> None:
        self.status = status
        self.notion_code = notion_code
        self.request_id = request_id
        self.retry_after = retry_after
        self.committed_resource_id = committed_resource_id
        details: dict[str, Any] = {"status": status, "notion_code": notion_code}
        if request_id:
            details["notion_request_id"] = request_id
        super().__init__(message, details=details)


class NotionAuthError(NotionError):
    code = "notion_unauthorized"


class NotionAccessError(NotionError):
    code = "notion_no_access"


class NotionValidationError(NotionError):
    code = "notion_validation_error"


class NotionConflictError(NotionError):
    code = "notion_conflict"
    retryable = True


class NotionRateLimitedError(NotionError):
    code = "notion_rate_limited"
    retryable = True


class NotionUnavailableError(NotionError):
    code = "notion_unavailable"
    retryable = True


class NotionUncertainWriteError(NotionUnavailableError):
    """The write may or may not have been applied; never repeat it blindly."""

    code = "notion_write_uncertain"
    retryable = False


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 30.0
    # A longer Retry-After is not waited for inside one request; the caller backs off instead.
    max_retry_after: float = 60.0


class RateLimiter:
    """Spaces request starts at least 1/rate seconds apart; `pause` delays all of them."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                await self._sleep(delay)
            self._next = max(now, self._next) + self._interval

    def pause(self, seconds: float) -> None:
        self._next = max(self._next, self._clock() + seconds)


@dataclass
class QueryResult:
    pages: list[dict[str, Any]]
    # False when Notion's 10,000-results-per-query cap could not be worked around.
    complete: bool


def _retry_after(headers: Any, additional: dict[str, Any]) -> float | None:
    for value in (headers.get("Retry-After"), additional.get("retry_after")):
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return None


def _short(text: Any, limit: int = 300) -> str:
    text = redact(str(text or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_rps: float = 2.5,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._retry = retry or RetryPolicy()
        self._limiter = RateLimiter(max_rps, sleep=sleep)
        self._session = session
        self._own_session = session is None
        self.requests_sent = 0

    @classmethod
    def from_settings(cls, settings: Any, **kwargs: Any) -> NotionClient:
        if settings.notion_token is None:
            raise WewlnError(
                "Notion token is not configured: run `wewln notion token set` "
                "or set WEWLN_NOTION_TOKEN",
                code="notion_token_missing",
                http_status=400,
            )
        return cls(
            settings.notion_token.get_secret_value(),
            base_url=settings.notion_api_base_url,
            timeout=settings.notion_timeout,
            max_rps=settings.notion_max_rps,
            **kwargs,
        )

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None and self._own_session:
            await self._session.close()
        self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._own_session = True
        return self._session

    def _headers(self, with_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
            "User-Agent": f"wewln-phonecaller/{__version__}",
        }
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> dict[str, Any]:
        """One API call with rate limiting and retries. `path` starts with /v1/."""
        method = method.upper()
        if idempotent is None:
            idempotent = method in _IDEMPOTENT_METHODS
        url = URL(self._base_url + path, encoded=True)
        attempt = 0
        while True:
            attempt += 1
            await self._limiter.wait()
            started = time.perf_counter()
            try:
                status, headers, payload = await self._send(method, url, body, params)
            except aiohttp.ClientConnectorError as exc:
                # The connection was never established, so nothing was sent: always retryable.
                error: NotionError = NotionUnavailableError(
                    f"cannot connect to the Notion API ({type(exc).__name__})"
                )
            except (TimeoutError, aiohttp.ClientError) as exc:
                message = f"Notion API request failed ({type(exc).__name__})"
                error = (
                    NotionUnavailableError(message)
                    if idempotent
                    else NotionUncertainWriteError(message + "; the write may have been saved")
                )
            else:
                log.debug(
                    "notion_request",
                    extra={
                        "method": method,
                        "path": path,
                        "status": status,
                        "attempt": attempt,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
                if 200 <= status < 300:
                    return payload if isinstance(payload, dict) else {}
                error = self._error_from_response(status, headers, payload, idempotent)
            if not error.retryable or attempt >= self._retry.max_attempts:
                raise error
            if error.retry_after is not None:
                if error.retry_after > self._retry.max_retry_after:
                    raise error
                delay = error.retry_after + random.uniform(0, 0.5)  # noqa: S311 (jitter)
            else:
                delay = min(self._retry.max_delay, self._retry.base_delay * 2 ** (attempt - 1))
                delay *= random.uniform(0.5, 1.0)  # noqa: S311 (jitter)
            # The pause applies to every request of this client, as Notion asks for 429/529.
            self._limiter.pause(delay)
            log.warning(
                "notion_retry",
                extra={
                    "method": method,
                    "path": path,
                    "status": error.status,
                    "notion_code": error.notion_code,
                    "attempt": attempt,
                    "delay_s": round(delay, 2),
                },
            )

    async def _send(
        self,
        method: str,
        url: URL,
        body: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> tuple[int, Any, Any]:
        self.requests_sent += 1
        session = self._get_session()
        async with session.request(
            method,
            url,
            json=body,
            params=params,
            headers=self._headers(body is not None),
            timeout=self._timeout,
        ) as resp:
            try:
                payload = await resp.json(content_type=None)
            except ValueError:
                payload = None
            return resp.status, resp.headers, payload

    @staticmethod
    def _error_from_response(
        status: int, headers: Any, payload: Any, idempotent: bool
    ) -> NotionError:
        data = payload if isinstance(payload, dict) else {}
        notion_code = data.get("code") if isinstance(data.get("code"), str) else None
        additional = data.get("additional_data")
        additional = additional if isinstance(additional, dict) else {}
        request_id = data.get("request_id") if isinstance(data.get("request_id"), str) else None
        text = f"Notion API error {status}"
        if notion_code:
            text += f" {notion_code}"
        if data.get("message"):
            text += f": {_short(data['message'])}"
        kwargs: dict[str, Any] = {
            "status": status,
            "notion_code": notion_code,
            "request_id": request_id,
        }
        if status == 401:
            return NotionAuthError(text, **kwargs)
        if status in (403, 404):
            return NotionAccessError(text, **kwargs)
        if status == 409:
            return NotionConflictError(text, **kwargs)
        if status in (429, 529):
            error = NotionRateLimitedError(
                text, retry_after=_retry_after(headers, additional), **kwargs
            )
            if additional.get("rate_limit_reason") == "public_api_request_blocked":
                error.retryable = False
            return error
        if status in (500, 502, 503, 504):
            if idempotent:
                return NotionUnavailableError(text, **kwargs)
            committed = additional.get("committed_resource_id")
            return NotionUncertainWriteError(
                text + " (the write may have been saved)",
                committed_resource_id=committed if isinstance(committed, str) else None,
                **kwargs,
            )
        if status == 400:
            return NotionValidationError(text, **kwargs)
        return NotionError(text, **kwargs)

    # ------------------------------------------------------------------ endpoints

    async def paginate(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every response page of a paginated endpoint (cursors stay opaque)."""
        cursor: str | None = None
        while True:
            if method == "GET":
                page_params = {**(params or {}), "page_size": MAX_PAGE_SIZE}
                if cursor:
                    page_params["start_cursor"] = cursor
                response = await self.request("GET", path, params=page_params)
            else:
                page_body = {**(body or {}), "page_size": MAX_PAGE_SIZE}
                if cursor:
                    page_body["start_cursor"] = cursor
                response = await self.request(
                    method, path, body=page_body, params=params, idempotent=True
                )
            yield response
            cursor = response.get("next_cursor") if response.get("has_more") else None
            if not cursor:
                return

    async def get_self(self) -> dict[str, Any]:
        return await self.request("GET", "/v1/users/me")

    async def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/pages/{page_id}")

    async def retrieve_database(self, database_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/databases/{database_id}")

    async def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/v1/data_sources/{data_source_id}")

    async def create_database(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/v1/databases", body=body)

    async def update_data_source(self, data_source_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "PATCH", f"/v1/data_sources/{data_source_id}", body=body, idempotent=True
        )

    async def create_page(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/v1/pages", body=body)

    async def update_page(self, page_id: str, body: dict[str, Any]) -> dict[str, Any]:
        # Writing the same property values twice is harmless, so updates may be retried.
        return await self.request("PATCH", f"/v1/pages/{page_id}", body=body, idempotent=True)

    async def property_items(self, page_id: str, property_id: str) -> list[dict[str, Any]]:
        """All items of a paginated page property (relations with more than 25 references)."""
        items: list[dict[str, Any]] = []
        async for response in self.paginate("GET", f"/v1/pages/{page_id}/properties/{property_id}"):
            if response.get("object") == "list":
                items.extend(response.get("results") or [])
            else:
                items.append(response)
        return items

    async def block_children(self, block_id: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        async for response in self.paginate("GET", f"/v1/blocks/{block_id}/children"):
            blocks.extend(response.get("results") or [])
        return blocks

    async def query_all(
        self, data_source_id: str, *, filter: dict[str, Any] | None = None
    ) -> QueryResult:
        """Every page of a data source, beyond the 10,000-results-per-query cap.

        The query is split into created_time windows (ascending): when a window is cut off,
        the next one starts at the last created_time seen; rows on the boundary appear twice
        and are de-duplicated by ID (Notion's documented technique for large data sources).
        """
        rows: dict[str, dict[str, Any]] = {}
        window_start: str | None = None
        path = f"/v1/data_sources/{data_source_id}/query"
        while True:
            limit_reached = False
            last_created: str | None = None
            body: dict[str, Any] = {
                "sorts": [{"timestamp": "created_time", "direction": "ascending"}]
            }
            combined = _window_filter(filter, window_start)
            if combined is not None:
                body["filter"] = combined
            async for response in self.paginate("POST", path, body=body):
                for row in response.get("results") or []:
                    if row.get("object") != "page" or not row.get("id"):
                        continue
                    rows[row["id"]] = row
                    last_created = row.get("created_time") or last_created
                request_status = response.get("request_status") or {}
                if request_status.get("type") == "incomplete":
                    limit_reached = True
            if not limit_reached:
                return QueryResult(list(rows.values()), complete=True)
            if not last_created or last_created == window_start:
                log.warning(
                    "notion_query_incomplete",
                    extra={"data_source_id": data_source_id, "rows": len(rows)},
                )
                return QueryResult(list(rows.values()), complete=False)
            window_start = last_created


def _window_filter(
    filter: dict[str, Any] | None, window_start: str | None
) -> dict[str, Any] | None:
    if window_start is None:
        return filter
    window = {"timestamp": "created_time", "created_time": {"on_or_after": window_start}}
    if filter is None:
        return window
    if "or" in filter:
        raise ValueError("top-level 'or' filters cannot be windowed")
    if "and" in filter:
        return {"and": [*filter["and"], window]}
    return {"and": [filter, window]}
