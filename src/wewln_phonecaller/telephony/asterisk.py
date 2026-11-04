"""Asterisk over ARI: REST requests to place and end calls, a WebSocket for call events.

Asterisk runs in WSL2 on Windows (or on any Linux machine) and reaches the phone network only
through the SIP trunk configured in its pjsip.conf; wewln never talks SIP itself. See
docs/ASTERISK.md for the setup.

- Calls are created with `POST /ari/channels/{channelId}`: the channel ID is derived from the
  attempt's idempotency key, and Asterisk answers 409 while a channel with that ID exists.
- wewln sends the request for an attempt once. One that may have reached Asterisk without a
  clear answer (no answer, or an error while the channel's existence cannot be checked) is
  never repeated: the attempt is watched as a call that may exist.
- After the answer the channel enters the Stasis application. Without the voice engine wewln
  plays `answer_media` (if set) and hangs up. With it (WEWLN_VOICE_MODE=ai) the call service
  asks for the call's audio: wewln creates a mixing bridge and an `externalMedia` channel
  (AudioSocket over TCP, 8 kHz slin) that Asterisk connects to wewln's AudioSocket server with
  a one-time UUID, and puts both channels in the bridge. Both are removed when the call ends.
  If Asterisk drops the audio channel while the call is still up (a bug of Asterisk 20.14-20.16
  and 22.4-22.6, see MAX_MEDIA_RECONNECTS), wewln connects a new one to the same bridge.
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
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from ..voice.transport import AudioSocketConnection, AudioSocketServer, MediaStream
from . import PROVIDERS
from .base import (
    CallRequest,
    EventKind,
    EventSink,
    MediaError,
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
# The audio channel wewln adds to one of its calls (see open_media).
# ("-media-2" and so on after a reconnect).
_OUR_MEDIA = re.compile(r"^wewln-[0-9a-f]{32}-media(-[0-9]{1,2})?$")
MEDIA_SUFFIX = "-media"
BRIDGE_SUFFIX = "-bridge"
# Asterisk 20.14-20.16 and 22.4-22.6 (Ubuntu 26.04 has 22.5.2) may hang up the AudioSocket
# channel in the middle of a call: "Failed to read header from AudioSocket because: Resource
# temporarily unavailable" (fixed in 20.17, 22.7 and 23.1). The call stays up, so wewln
# connects a new audio channel to the call's bridge, at most this many times per call.
MAX_MEDIA_RECONNECTS = 5
# How long a lost audio connection waits for the call's own hangup before reconnecting.
MEDIA_LOSS_GRACE_S = 0.25
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
    # The AudioSocket server for call audio (voice mode): where wewln listens, and the
    # host:port Asterisk connects to (they differ when Asterisk runs in WSL2 with NAT).
    media_host: str = "127.0.0.1"
    media_port: int = 9092
    media_advertise: str = ""
    media_timeout: float = 8.0

    @property
    def media_target(self) -> str:
        return self.media_advertise or f"{self.media_host}:{self.media_port}"


@dataclass
class _Media:
    """The audio of one call: its externalMedia channel, the bridge and the connection."""

    channel_id: str
    bridge_id: str
    call_uuid: str
    connection: AudioSocketConnection | None = None
    reconnects: int = 0


class _CallAudio(MediaStream):
    """The audio of one call as the voice engine sees it: one stream for the whole call,
    even when Asterisk drops the audio channel and wewln connects a new one."""

    def __init__(self, provider: AsteriskAriProvider, call_id: str, media: _Media) -> None:
        self._provider = provider
        self._call_id = call_id
        self._media = media
        self._closed = False
        self._failed = False
        self._reconnect: asyncio.Task[bool] | None = None

    @property
    def connection(self) -> AudioSocketConnection | None:
        return self._media.connection

    @property
    def reconnects(self) -> int:
        return self._media.reconnects

    @property
    def closed(self) -> bool:
        if self._closed or self._failed:
            return True
        if self._reconnect is not None:
            return False
        connection = self._media.connection
        return connection is None or connection.closed

    async def read(self) -> bytes | None:
        while True:
            if self._reconnect is not None:
                # Shielded: the engine may cancel a read that waits too long; the reconnect
                # goes on and the next read waits for it.
                ok = await asyncio.shield(self._reconnect)
                self._reconnect = None
                self._failed = not ok
            connection = self._media.connection
            if connection is None or self._closed or self._failed:
                return None
            chunk = await connection.read()
            if chunk is not None:
                return chunk
            if self._closed or connection.closed_here:
                return None  # wewln ended it (the call ended)
            task = asyncio.create_task(
                self._provider._reconnect_media(self._call_id, self._media, connection)
            )
            self._provider._media_tasks.add(task)
            task.add_done_callback(self._provider._media_tasks.discard)
            self._reconnect = task

    async def write(self, pcm: bytes) -> None:
        connection = self._media.connection
        if connection is not None and not self._closed:
            await connection.write(pcm)  # dropped while a new connection is being made

    async def close(self) -> None:
        self._closed = True
        if self._reconnect is not None:
            self._reconnect.cancel()
        connection = self._media.connection
        if connection is not None:
            await connection.close()


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
    supports_media = True

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
        # Channels (calls and their audio channels) that entered the Stasis application.
        self._stasis: dict[str, asyncio.Event] = {}
        self._media: dict[str, _Media] = {}
        self._media_tasks: set[asyncio.Task[None]] = set()
        self.audiosocket: AudioSocketServer | None = None

    # ------------------------------------------------------------------ connection

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _new_session(self) -> aiohttp.ClientSession:
        # The connect limit is shorter than the total one, so that a connection that never
        # came about (nothing was sent) is told apart from a request left without an answer.
        connect = min(self.cfg.connect_timeout, self.cfg.request_timeout / 2)
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.request_timeout, connect=connect),
            raise_for_status=False,
        )

    async def start(self, sink: EventSink) -> None:
        if self.media_enabled and self.audiosocket is None:
            server = AudioSocketServer(self.cfg.media_host, self.cfg.media_port)
            try:
                await server.start()
            except OSError as exc:
                raise MediaError(
                    f"cannot listen for call audio on {self.cfg.media_host}:"
                    f"{self.cfg.media_port} ({exc.strerror or type(exc).__name__}; another "
                    "program may use the port: WEWLN_VOICE_AUDIOSOCKET_PORT)",
                    code="audiosocket_unavailable",
                ) from None
            self.audiosocket = server
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
        for call_id in list(self._media):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._drop_media(call_id), 5.0)
        if self._media_tasks:
            await asyncio.wait(set(self._media_tasks), timeout=5.0)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self.audiosocket is not None:
            await self.audiosocket.close()
            self.audiosocket = None
        self._connected.clear()
        self._sink = None
        self._stasis.clear()

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
        if _OUR_MEDIA.match(channel_id):
            # The audio channel of one of wewln's calls: it carries no call progress.
            if etype == "StasisStart":
                self._stasis.setdefault(channel_id, asyncio.Event()).set()
            elif etype == "ChannelDestroyed":
                self._stasis.pop(channel_id, None)
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
            self._stasis.pop(channel_id, None)
            if channel_id in self._media:
                self._spawn(self._drop_media(channel_id))
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
        if etype == "StasisStart":
            # Before the event is delivered: open_media() waits for it.
            self._stasis.setdefault(channel_id, asyncio.Event()).set()
        if self._sink is not None:
            await self._sink(provider_event)
        if etype == "StasisStart":
            await self._on_answer(channel_id)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._media_tasks.add(task)
        task.add_done_callback(self._media_tasks.discard)

    async def _on_answer(self, channel_id: str) -> None:
        """Voice mode: the call service connects the audio (open_media). Otherwise play the
        configured media, then hang up."""
        if self.media_enabled:
            return
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

    # ------------------------------------------------------------------ call audio

    async def open_media(self, call_id: str) -> MediaStream:
        server = self.audiosocket
        if not self.media_enabled or server is None:
            raise MediaError("call audio is not enabled (WEWLN_VOICE_MODE=ai)")
        if not _OUR_CALL.match(call_id):
            raise MediaError("not a call placed by wewln")
        if call_id in self._media:
            raise MediaError("the call's audio is already connected")
        stasis = self._stasis.setdefault(call_id, asyncio.Event())
        try:
            await asyncio.wait_for(stasis.wait(), self.cfg.media_timeout)
        except TimeoutError:
            raise MediaError("the answered call did not reach the ARI application") from None
        call_uuid, connected = server.expect()
        media = _Media(call_id + MEDIA_SUFFIX, call_id + BRIDGE_SUFFIX, call_uuid)
        self._media[call_id] = media
        started = time.perf_counter()
        try:
            await self._connect_media(call_id, media, connected)
        except BaseException as exc:
            server.forget(call_uuid)
            await asyncio.shield(self._drop_media(call_id))
            if isinstance(exc, ProviderError) and not isinstance(exc, MediaError):
                raise MediaError(f"the call audio could not be connected ({exc.message})") from None
            raise
        assert media.connection is not None  # noqa: S101 - set by _connect_media
        log.info(
            "call_media_connected",
            extra={"setup_ms": round((time.perf_counter() - started) * 1000)},
        )
        return _CallAudio(self, call_id, media)

    async def _connect_media(
        self, call_id: str, media: _Media, connected: asyncio.Future[AudioSocketConnection]
    ) -> None:
        status = await self._request(
            "POST",
            f"/ari/bridges/{quote(media.bridge_id, safe='')}",
            params={"type": "mixing", "name": "wewln-voice"},
        )
        if status != 200:
            raise MediaError(f"Asterisk could not create the audio bridge (HTTP {status})")
        await self._attach_media(media, connected, f"{call_id},{media.channel_id}")

    async def _attach_media(
        self, media: _Media, connected: asyncio.Future[AudioSocketConnection], channels: str
    ) -> None:
        """Create the externalMedia channel, wait for its connection and put `channels` (the
        call and the audio channel, or only the audio channel) in the bridge."""
        status = await self._request(
            "POST",
            "/ari/channels/externalMedia",
            params={
                "channelId": media.channel_id,
                "app": self.cfg.app,
                "external_host": self.cfg.media_target,
                "format": "slin",
                "encapsulation": "audiosocket",
                "transport": "tcp",
                "connection_type": "client",
                "direction": "both",
                "data": media.call_uuid,
            },
        )
        if status != 200:
            raise MediaError(
                f"Asterisk could not connect the call audio to {self.cfg.media_target} "
                f"(HTTP {status}): check WEWLN_VOICE_AUDIOSOCKET_ADVERTISE, the firewall and "
                "that chan_audiosocket is loaded (docs/ASTERISK.md)"
            )
        try:
            media.connection = await asyncio.wait_for(connected, self.cfg.media_timeout)
        except TimeoutError:
            raise MediaError(
                f"Asterisk did not connect to {self.cfg.media_target} for the call audio"
            ) from None
        in_stasis = self._stasis.setdefault(media.channel_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(in_stasis.wait(), self.cfg.media_timeout)
        status = await self._request(
            "POST",
            f"/ari/bridges/{quote(media.bridge_id, safe='')}/addChannel",
            params={"channel": channels},
        )
        if status not in (200, 204):
            raise MediaError(f"Asterisk could not bridge the call audio (HTTP {status})")

    async def _reconnect_media(
        self, call_id: str, media: _Media, lost: AudioSocketConnection
    ) -> bool:
        """Asterisk closed the call's audio connection. Unless the call is ending, a new audio
        channel joins the call's bridge (the person hears a short gap). True when connected."""
        await asyncio.sleep(MEDIA_LOSS_GRACE_S)  # the call's hangup usually arrives by now
        extra = {"call_id": call_id, "reconnects": media.reconnects}
        server = self.audiosocket
        if self._closing or server is None or self._media.get(call_id) is not media:
            return False
        if media.connection is not lost:
            return media.connection is not None and not media.connection.closed
        if media.reconnects >= MAX_MEDIA_RECONNECTS:
            log.error("call_media_lost", extra=extra)
            return False
        if await self.call_exists(call_id) is not True:
            return False  # the call has ended (or Asterisk cannot tell): no new audio
        log.warning("call_media_dropped_by_asterisk", extra=extra)
        server.release(lost)
        old_channel = media.channel_id
        media.reconnects += 1
        media.channel_id = f"{call_id}{MEDIA_SUFFIX}-{media.reconnects + 1}"
        media.call_uuid, connected = server.expect()
        media.connection = None
        self._stasis.pop(old_channel, None)
        with contextlib.suppress(ProviderError):
            await self._request("DELETE", f"/ari/channels/{quote(old_channel, safe='')}")
        started = time.perf_counter()
        try:
            await self._attach_media(media, connected, media.channel_id)
        except (ProviderError, OSError) as exc:
            server.forget(media.call_uuid)
            log.error("call_media_reconnect_failed", extra={**extra, "error": str(exc)})
            return False
        if self._media.get(call_id) is not media:
            # The call ended meanwhile; _drop_media did not see the new connection.
            if media.connection is not None:
                await media.connection.close()
                server.release(media.connection)
            with contextlib.suppress(ProviderError):
                await self._request("DELETE", f"/ari/channels/{quote(media.channel_id, safe='')}")
            return False
        log.warning(
            "call_media_reconnected",
            extra={
                **extra,
                "reconnects": media.reconnects,
                "setup_ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return True

    async def _drop_media(self, call_id: str) -> None:
        """Close the audio connection and remove the audio channel and the bridge."""
        media = self._media.pop(call_id, None)
        if media is None:
            return
        if media.connection is not None:
            await media.connection.close()
            if self.audiosocket is not None:
                self.audiosocket.release(media.connection)
        elif self.audiosocket is not None:
            self.audiosocket.forget(media.call_uuid, MediaError("the call ended"))
        for path in (
            f"/ari/channels/{quote(media.channel_id, safe='')}",
            f"/ari/bridges/{quote(media.bridge_id, safe='')}",
        ):
            with contextlib.suppress(ProviderError):
                await self._request("DELETE", path)
        self._stasis.pop(media.channel_id, None)

    async def check_media(self) -> dict[str, Any]:
        """Whether Asterisk can reach wewln's AudioSocket server, without any call: a test
        audio channel connects (nothing is dialed) and is removed at once."""
        server = self.audiosocket
        if server is None:
            return {"status": "error", "error": "call audio is not enabled"}
        channel_id = f"wewln-{uuid.uuid4().hex}{MEDIA_SUFFIX}"
        call_uuid, connected = server.expect()
        started = time.perf_counter()
        result: dict[str, Any] = {"target": self.cfg.media_target}
        try:
            status = await self._request(
                "POST",
                "/ari/channels/externalMedia",
                params={
                    "channelId": channel_id,
                    "app": self.cfg.app,
                    "external_host": self.cfg.media_target,
                    "format": "slin",
                    "encapsulation": "audiosocket",
                    "transport": "tcp",
                    "data": call_uuid,
                },
            )
            if status != 200:
                return {
                    **result,
                    "status": "error",
                    "error": f"Asterisk could not connect to {self.cfg.media_target} "
                    f"(externalMedia: HTTP {status})",
                }
            try:
                connection = await asyncio.wait_for(connected, self.cfg.media_timeout)
            except TimeoutError:
                return {**result, "status": "error", "error": "no AudioSocket connection came"}
            result["connect_ms"] = round((time.perf_counter() - started) * 1000)
            await connection.close()
            server.release(connection)
            return {**result, "status": "ok"}
        except ProviderError as exc:
            return {**result, "status": "error", "error": exc.message}
        finally:
            server.forget(call_uuid)
            with contextlib.suppress(ProviderError):
                await self._request("DELETE", f"/ari/channels/{quote(channel_id, safe='')}")

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
        except (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError) as exc:
            # The request was certainly not sent.
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
        if status is None or exists is None:
            # No answer, or an error while Asterisk cannot even say whether the channel exists
            # (a proxy's 502/504 may hide a call that was created): never sent again.
            what = "did not answer" if status is None else f"answered HTTP {status}"
            raise ProviderUncertainError(
                f"Asterisk ARI {what} to the call request; it is not repeated automatically"
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
        if status is None:
            return {**result, "status": "error", "error": "Asterisk ARI did not answer in time"}
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
        if self.media_enabled:
            m_status, module = await self._request_json(
                "GET", "/ari/asterisk/modules/chan_audiosocket.so"
            )
            state = (module or {}).get("status") if m_status == 200 else "not loaded"
            result["audiosocket"] = {
                "module": state,
                "listen": f"{self.cfg.media_host}:{self.cfg.media_port}",
                "advertise": self.cfg.media_target,
            }
            if m_status != 200:
                problems.append("chan_audiosocket.so is not loaded in Asterisk (modules.conf)")
        result["status"] = "error" if problems else "ok"
        if problems:
            result["error"] = "; ".join(problems)
        return result
