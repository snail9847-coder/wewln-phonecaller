"""Asterisk over ARI: REST requests to place and end calls, a WebSocket for call events.

Asterisk runs in WSL2 on Windows (or on any Linux machine) and reaches the phone network only
through the SIP trunk configured in its pjsip.conf; wewln never talks SIP itself. See
docs/ASTERISK.md for the setup.

- Calls are created with `POST /ari/channels/{channelId}`: the channel ID is derived from the
  attempt's idempotency key, and Asterisk answers 409 for an ID that already exists.
- The request that may have reached Asterisk without an answer is never repeated blindly.
- After the answer the channel enters the Stasis application; until the voice engine arrives
  (Phase 5) wewln plays `answer_media` (if set) and hangs up.
- The password travels only in the Authorization header (never in a URL) and is never logged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from . import PROVIDERS
from .base import (
    CallRequest,
    EventKind,
    EventSink,
    PlaceCallResult,
    ProviderError,
    ProviderEvent,
    ProviderRejectedError,
    ProviderUnavailableError,
    ProviderUncertainError,
    TelephonyProvider,
)

log = logging.getLogger("wewln.telephony.asterisk")

NUMBER_FORMATS = ("e164", "digits", "00")
# Only a PJSIP trunk is an endpoint Asterisk can report on (Local/... is a dialplan context).
_TRUNK = re.compile(r"^PJSIP/\{number\}@(?P<resource>[A-Za-z0-9_.-]+)$")
# Channels wewln placed (see base.provider_call_id); a Local channel's ";2" half is not one.
_OUR_CALL = re.compile(r"^wewln-[0-9a-f]{32}$")
_DIAL_STATUS = {
    "RINGING": EventKind.RINGING,
    "PROGRESS": EventKind.RINGING,  # early media (183) instead of a ringing signal
    "ANSWER": EventKind.ANSWERED,
}
# Dial results that tell why an unanswered call ended when the hangup cause does not
# (Asterisk may destroy the channel with cause 0 or 16 after a busy or congestion signal).
_DIAL_CAUSE = {"BUSY": 17, "CONGESTION": 34}
_UNINFORMATIVE_CAUSES = (None, 0, 16)


@dataclass(frozen=True)
class AriConfig:
    url: str
    username: str
    password: str = field(repr=False)
    app: str = "wewln"
    endpoint_template: str = "PJSIP/{number}@wewln-trunk"
    number_format: str = "e164"
    caller_id: str | None = None
    answer_media: str | None = "sound:hello-world"
    request_timeout: float = 10.0
    connect_timeout: float = 10.0
    heartbeat: float = 30.0


def format_number(e164: str, number_format: str) -> str:
    """+994501234567 -> "+994501234567" (e164), "994501234567" (digits), "00994501234567" (00)."""
    digits = e164.removeprefix("+")
    if not digits.isdigit():
        raise ValueError("not an E.164 number")
    if number_format == "digits":
        return digits
    if number_format == "00":
        return "00" + digits
    return "+" + digits


def build_endpoint(template: str, number: str) -> str:
    return template.replace("{number}", number)


def trunk_of(template: str) -> tuple[str, str] | None:
    """("PJSIP", "wewln-trunk") for "PJSIP/{number}@wewln-trunk"; None for other templates."""
    match = _TRUNK.match(template)
    return ("PJSIP", match["resource"]) if match else None


class AsteriskAriProvider(TelephonyProvider):
    info = PROVIDERS["asterisk"]

    def __init__(
        self,
        config: AriConfig,
        *,
        reconnect_delays: tuple[float, ...] = (1, 2, 5, 10, 30),
    ) -> None:
        self.cfg = config
        self._base = config.url.rstrip("/")
        token = base64.b64encode(f"{config.username}:{config.password}".encode()).decode("ascii")
        # HTTP Basic auth, sent as a header on every request (never in a URL).
        self._headers = {"Authorization": f"Basic {token}"}
        self._delays = reconnect_delays
        self._session: aiohttp.ClientSession | None = None
        self._sink: EventSink | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._wake = asyncio.Event()
        self._closing = False
        self._fatal: ProviderError | None = None
        self._replaced = False
        self._last_error: str | None = None
        self._playing: set[str] = set()
        self._dial_causes: dict[str, int] = {}
        self.connections = 0

    # ------------------------------------------------------------------ connection

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _new_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.request_timeout),
            raise_for_status=False,
        )

    async def start(self, sink: EventSink) -> None:
        self._sink = sink
        self._closing = False
        self._session = self._new_session()
        self._task = asyncio.create_task(self._events_loop())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.connect_timeout
        while not self._connected.is_set():
            if self._fatal is not None:
                error = self._fatal
                await self.close()
                raise error
            remaining = deadline - loop.time()
            if remaining <= 0:
                reason = self._last_error or "no connection"
                await self.close()
                raise ProviderUnavailableError(f"cannot connect to Asterisk ARI ({reason})")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), min(remaining, 0.5))

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._connected.clear()
        self._sink = None

    def _ws_url(self) -> str:
        scheme = "wss" if self._base.startswith("https://") else "ws"
        return scheme + self._base[self._base.index(":") :] + "/ari/events"

    async def _events_loop(self) -> None:
        failures = 0
        while not self._closing and self._session is not None:
            try:
                async with self._session.ws_connect(
                    self._ws_url(),
                    params={"app": self.cfg.app, "subscribeAll": "false"},
                    headers=self._headers,
                    heartbeat=self.cfg.heartbeat or None,
                ) as ws:
                    failures = 0
                    self.connections += 1
                    self._connected.set()
                    self._wake.set()
                    log.info("ari_connected", extra={"app": self.cfg.app})
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await self._dispatch_text(message.data)
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (401, 403):
                    self._fatal = ProviderRejectedError(
                        "Asterisk ARI rejected the user name or password", code="ari_auth"
                    )
                    self._wake.set()
                    return
                self._last_error = f"ARI WebSocket handshake failed (HTTP {exc.status})"
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                self._last_error = f"{type(exc).__name__}"
            finally:
                if self._connected.is_set():
                    log.warning("ari_disconnected")
                self._connected.clear()
            self._wake.set()
            if self._replaced or self._closing:
                return
            delay = self._delays[min(failures, len(self._delays) - 1)] if self._delays else 1
            failures += 1
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ events

    async def _dispatch_text(self, data: str) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            return
        if isinstance(event, dict):
            try:
                await self._dispatch(event)
            except Exception:  # one bad event must not end the event stream
                log.exception("ari_event_failed", extra={"event_type": event.get("type")})

    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "ApplicationReplaced":
            self._replaced = True
            log.error("ari_application_replaced", extra={"app": self.cfg.app})
            return
        if etype == "PlaybackFinished":
            target = str((event.get("playback") or {}).get("target_uri") or "")
            channel_id = target.removeprefix("channel:")
            if channel_id in self._playing:
                self._playing.discard(channel_id)
                await self._hangup_quietly(channel_id)
            return
        # A Dial event names the dialed channel "peer"; all other channel events "channel".
        channel = event.get("peer" if etype == "Dial" else "channel")
        channel = channel if isinstance(channel, dict) else {}
        channel_id = channel.get("id")
        if not isinstance(channel_id, str):
            return
        if not _OUR_CALL.match(channel_id):
            if etype == "StasisStart":
                # Not a call wewln placed (a dialplan sent it here): it is not served.
                await self._hangup_quietly(channel_id)
            return
        cause = event.get("cause")
        cause = cause if isinstance(cause, int) else None
        kind: EventKind | None = None
        if etype == "ChannelStateChange":
            state = channel.get("state")
            if state in ("Ringing", "Ring"):
                kind = EventKind.RINGING
            elif state == "Up":
                kind = EventKind.ANSWERED
        elif etype == "StasisStart":
            kind = EventKind.ANSWERED
        elif etype == "Dial":
            status = str(event.get("dialstatus") or "")
            if status in _DIAL_CAUSE:
                self._dial_causes[channel_id] = _DIAL_CAUSE[status]
            kind = _DIAL_STATUS.get(status)
        elif etype == "ChannelDestroyed":
            kind = EventKind.HANGUP
            self._playing.discard(channel_id)
            hint = self._dial_causes.pop(channel_id, None)
            if hint is not None and cause in _UNINFORMATIVE_CAUSES:
                cause = hint
        if kind is None:
            return
        fingerprint = json.dumps(
            [
                etype,
                channel_id,
                event.get("timestamp"),
                channel.get("state"),
                event.get("cause"),
                event.get("dialstatus"),
            ],
            sort_keys=True,
        )
        tech_cause = event.get("tech_cause")
        provider_event = ProviderEvent(
            call_id=channel_id,
            kind=kind,
            event_key="ari:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:40],
            source="asterisk",
            cause=cause,
            cause_txt=str(event.get("cause_txt") or "")[:100] or None,
            tech_cause=tech_cause if isinstance(tech_cause, int) else None,
        )
        if self._sink is not None:
            await self._sink(provider_event)
        if etype == "StasisStart":
            await self._on_answer(channel_id)

    async def _on_answer(self, channel_id: str) -> None:
        """Phase 4 has no voice engine yet: play the configured media, then hang up."""
        if self.cfg.answer_media:
            status = await self._request(
                "POST",
                f"/ari/channels/{quote(channel_id, safe='')}/play",
                params={"media": self.cfg.answer_media},
            )
            if status in (200, 201):
                self._playing.add(channel_id)
                return
            log.warning("ari_playback_failed", extra={"status": status})
        await self._hangup_quietly(channel_id)

    async def _hangup_quietly(self, channel_id: str) -> None:
        with contextlib.suppress(ProviderError):
            await self.hangup(channel_id)

    # ------------------------------------------------------------------ requests

    async def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None
    ) -> int | None:
        """HTTP status of an ARI request; None when no answer came back."""
        status, _body = await self._request_json(method, path, params=params)
        return status

    async def _request_json(
        self, method: str, path: str, *, params: dict[str, str] | None = None
    ) -> tuple[int | None, Any]:
        session = self._session
        own = session is None
        if session is None:
            session = self._new_session()
        try:
            async with session.request(
                method, self._base + path, params=params, headers=self._headers
            ) as resp:
                body: Any = None
                if resp.content_type == "application/json":
                    with contextlib.suppress(ValueError, aiohttp.ContentTypeError):
                        body = await resp.json()
                return resp.status, body
        except aiohttp.ClientConnectorError as exc:
            raise ProviderUnavailableError(
                f"cannot connect to Asterisk ARI ({type(exc).__name__})"
            ) from None
        except (aiohttp.ClientError, OSError, TimeoutError):
            return None, None
        finally:
            if own:
                await session.close()

    def _endpoint(self, request: CallRequest) -> str:
        if request.local_endpoint:
            return request.local_endpoint
        number = format_number(request.to_e164, self.cfg.number_format)
        return build_endpoint(self.cfg.endpoint_template, number)

    async def place_call(self, request: CallRequest) -> PlaceCallResult:
        if self._fatal is not None:
            raise self._fatal
        if self._replaced:
            raise ProviderUnavailableError(
                "another program took over the ARI application", code="ari_app_replaced"
            )
        if not self._connected.is_set():
            raise ProviderUnavailableError("the ARI event connection is down")
        params = {
            "endpoint": self._endpoint(request),
            "app": self.cfg.app,
            "appArgs": "wewln",
            "timeout": str(int(request.ring_timeout_s)),
        }
        caller_id = request.caller_id or self.cfg.caller_id
        if caller_id:
            params["callerId"] = caller_id
        path = f"/ari/channels/{quote(request.call_id, safe='')}"
        status = await self._request("POST", path, params=params)
        if status == 200:
            return PlaceCallResult(request.call_id)
        if status == 409:
            return PlaceCallResult(request.call_id, already_existed=True)
        if status in (401, 403):
            raise ProviderRejectedError(
                "Asterisk ARI rejected the user name or password", code="ari_auth"
            )
        if status in (400, 404, 422):
            raise ProviderRejectedError(
                f"Asterisk refused the call request (HTTP {status}): check "
                "WEWLN_ASTERISK_ENDPOINT, the trunk in pjsip.conf and that ARI is enabled",
                code="ari_bad_request",
            )
        # No answer or a server error: the channel may exist anyway. Look before deciding.
        exists = await self.call_exists(request.call_id)
        if exists:
            return PlaceCallResult(request.call_id, already_existed=True)
        if status is None:
            raise ProviderUncertainError(
                "Asterisk ARI did not answer the call request; it is not repeated automatically"
            )
        raise ProviderError(f"Asterisk ARI error (HTTP {status})", code="ari_server_error")

    async def hangup(self, call_id: str) -> bool:
        status = await self._request(
            "DELETE", f"/ari/channels/{quote(call_id, safe='')}", params={"reason": "normal"}
        )
        if status in (200, 204):
            return True
        if status == 404:
            return False
        raise ProviderError(f"Asterisk ARI hangup failed (HTTP {status})", code="ari_hangup")

    async def call_exists(self, call_id: str) -> bool | None:
        try:
            status = await self._request("GET", f"/ari/channels/{quote(call_id, safe='')}")
        except ProviderUnavailableError:
            return None
        if status == 200:
            return True
        if status == 404:
            return False
        return None

    async def check(self) -> dict[str, Any]:
        """ARI reachable, credentials accepted, Asterisk version, the app and the trunk state."""
        result: dict[str, Any] = {
            "provider": "asterisk",
            "ari_url": self._base,
            "app": self.cfg.app,
            "endpoint_template": self.cfg.endpoint_template,
            "number_format": self.cfg.number_format,
        }
        try:
            status, info = await self._request_json(
                "GET", "/ari/asterisk/info", params={"only": "system"}
            )
        except ProviderUnavailableError as exc:
            return {**result, "status": "error", "error": exc.message}
        if status in (401, 403):
            return {**result, "status": "error", "error": "ARI rejected the user name or password"}
        if status != 200:
            return {**result, "status": "error", "error": f"ARI answered HTTP {status}"}
        result["asterisk_version"] = ((info or {}).get("system") or {}).get("version")
        app_status, _ = await self._request_json(
            "GET", f"/ari/applications/{quote(self.cfg.app, safe='')}"
        )
        result["app_connected"] = app_status == 200
        trunk = trunk_of(self.cfg.endpoint_template)
        if trunk is not None:
            tech, resource = trunk
            t_status, endpoint = await self._request_json(
                "GET", f"/ari/endpoints/{quote(tech, safe='')}/{quote(resource, safe='')}"
            )
            if t_status == 200:
                result["trunk"] = {"name": resource, "state": (endpoint or {}).get("state")}
            else:
                result["trunk"] = {"name": resource, "state": "missing"}
        problems = []
        if result.get("trunk", {}).get("state") == "missing":
            problems.append("the trunk endpoint does not exist in Asterisk (pjsip.conf)")
        result["status"] = "error" if problems else "ok"
        if problems:
            result["error"] = "; ".join(problems)
        return result
