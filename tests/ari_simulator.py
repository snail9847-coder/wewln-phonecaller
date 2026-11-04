"""A small imitation of the Asterisk REST Interface (ARI) for tests: REST + WebSocket events.

It behaves like Asterisk where wewln depends on it: Basic auth, `POST /channels/{id}` with 409
for an existing ID, `DELETE` / `GET` channels, playback, and the event stream of a Stasis
application (ChannelStateChange, StasisStart, ChannelDestroyed, PlaybackFinished). What a call
does is a scenario chosen per dialed endpoint. No SIP, no audio.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import WSMsgType, web

# (delay in seconds, event) steps; "answer" waits for the provider's playback/hangup.
SCENARIOS: dict[str, tuple[tuple[float, str, dict[str, Any]], ...]] = {
    "answer": ((0.01, "ringing", {}), (0.02, "answer", {})),
    "busy": (
        (0.01, "ringing", {}),
        (0.02, "destroy", {"cause": 17, "cause_txt": "User busy", "tech_cause": 486}),
    ),
    "reject": (
        (0.01, "ringing", {}),
        (0.02, "destroy", {"cause": 21, "cause_txt": "Call Rejected", "tech_cause": 603}),
    ),
    "unreachable": (
        (0.01, "destroy", {"cause": 1, "cause_txt": "Unallocated (unassigned) number"}),
    ),
    "provider_refused": (
        (0.01, "destroy", {"cause": 21, "cause_txt": "Call Rejected", "tech_cause": 403}),
    ),
    # Asterisk may report a busy signal only in the Dial event and destroy with cause 16.
    "busy_signal": (
        (0.01, "ringing", {}),
        (0.01, "dial", {"dialstatus": "BUSY"}),
        (0.01, "destroy", {"cause": 16, "cause_txt": "Normal Clearing"}),
    ),
    # Early media (SIP 183) instead of a ringing signal, then nothing.
    "early_media": ((0.01, "dial", {"dialstatus": "PROGRESS"}),),
    "ringing": ((0.01, "ringing", {}),),
    "silent": (),
}


@dataclass
class Channel:
    id: str
    endpoint: str
    app: str
    state: str = "Down"
    task: asyncio.Task[None] | None = None


@dataclass
class AriSimulator:
    username: str = "wewln"
    password: str = "ari-test-password"
    version: str = "22.5.2"
    endpoints: dict[str, str] = field(default_factory=lambda: {"PJSIP/wewln-trunk": "online"})
    default_scenario: str = "answer"
    # Substring of the dialed endpoint -> scenario name.
    scenarios: dict[str, str] = field(default_factory=dict)
    playback_seconds: float = 0.02
    # "normal", "timeout" (no answer, no channel), "create_then_timeout", "error" (HTTP 500),
    # "slow_create" (the channel appears, and the answer comes, only after post_delay)
    post_behavior: str = "normal"
    post_delay: float = 2.0
    # "normal" or "error": GET /channels/{id} answers HTTP 500 (existence cannot be told)
    get_behavior: str = "normal"

    def __post_init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = []
        self.sockets: dict[str, web.WebSocketResponse] = {}
        self.ws_connects = 0
        self._seq = itertools.count(1)
        self._background: set[asyncio.Future[Channel]] = set()
        self.base_url = ""

    # ------------------------------------------------------------------ helpers

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        expected = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return header == f"Basic {expected}"

    def _timestamp(self) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
        return f"{now}{next(self._seq):04d}+0000"

    async def send(self, app: str, event: dict[str, Any]) -> None:
        ws = self.sockets.get(app)
        if ws is None or ws.closed:
            return
        event = {"application": app, "timestamp": self._timestamp(), **event}
        with contextlib.suppress(ConnectionError, RuntimeError):
            await ws.send_json(event)

    def _channel_json(self, channel: Channel) -> dict[str, Any]:
        return {"id": channel.id, "name": channel.endpoint, "state": channel.state}

    async def _destroy(self, channel: Channel, cause: int, cause_txt: str, tech: int | None):
        if self.channels.pop(channel.id, None) is None:
            return
        event: dict[str, Any] = {
            "type": "ChannelDestroyed",
            "cause": cause,
            "cause_txt": cause_txt,
            "channel": self._channel_json(channel),
        }
        if tech is not None:
            event["tech_cause"] = tech
        await self.send(channel.app, event)

    async def _dial(self, channel: Channel, status: str) -> None:
        """An originated call has no caller: the Dial event names only the "peer"."""
        await self.send(
            channel.app,
            {
                "type": "Dial",
                "peer": self._channel_json(channel),
                "dialstring": channel.endpoint.split("/", 1)[-1],
                "dialstatus": status,
            },
        )

    def scenario_for(self, endpoint: str) -> str:
        for needle, name in self.scenarios.items():
            if needle in endpoint:
                return name
        return self.default_scenario

    async def _run(self, channel: Channel, name: str) -> None:
        for delay, step, data in SCENARIOS[name]:
            await asyncio.sleep(delay)
            if channel.id not in self.channels:
                return
            if step == "ringing":
                channel.state = "Ringing"
                await self.send(
                    channel.app,
                    {"type": "ChannelStateChange", "channel": self._channel_json(channel)},
                )
                await self._dial(channel, "RINGING")
            elif step == "dial":
                await self._dial(channel, data["dialstatus"])
            elif step == "answer":
                channel.state = "Up"
                await self._dial(channel, "ANSWER")
                await self.send(
                    channel.app,
                    {"type": "ChannelStateChange", "channel": self._channel_json(channel)},
                )
                await self.send(
                    channel.app,
                    {
                        "type": "StasisStart",
                        "args": ["wewln"],
                        "channel": self._channel_json(channel),
                    },
                )
            elif step == "destroy":
                await self._destroy(
                    channel, data["cause"], data["cause_txt"], data.get("tech_cause")
                )

    async def foreign_stasis_start(self, app: str, channel_id: str = "1730000000.42") -> None:
        """A channel sent to the app by a dialplan (not placed by wewln)."""
        channel = Channel(id=channel_id, endpoint="PJSIP/foreign", app=app, state="Up")
        self.channels[channel.id] = channel
        await self.send(
            app, {"type": "StasisStart", "args": [], "channel": self._channel_json(channel)}
        )

    async def drop_websockets(self) -> None:
        for ws in list(self.sockets.values()):
            await ws.close()

    # ------------------------------------------------------------------ handlers

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        self.requests.append(
            (request.method, request.path, dict(request.query), dict(request.headers))
        )
        if not self._authorized(request):
            return web.json_response({"message": "Authentication required"}, status=401)
        return await handler(request)

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response({"system": {"version": self.version, "entity_id": "sim"}})

    async def application(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        ws = self.sockets.get(name)
        if ws is None or ws.closed:
            return web.json_response({"message": "Application not found"}, status=404)
        return web.json_response({"name": name, "channel_ids": list(self.channels)})

    async def endpoint(self, request: web.Request) -> web.Response:
        key = f"{request.match_info['tech']}/{request.match_info['resource']}"
        if key not in self.endpoints:
            return web.json_response({"message": "Endpoint not found"}, status=404)
        tech, resource = key.split("/", 1)
        return web.json_response(
            {"technology": tech, "resource": resource, "state": self.endpoints[key]}
        )

    async def create_channel(self, request: web.Request) -> web.Response:
        channel_id = request.match_info["id"]
        endpoint = request.query.get("endpoint", "")
        app = request.query.get("app", "")
        if channel_id in self.channels:
            return web.json_response(
                {"message": "Channel with given unique ID already exists"}, status=409
            )
        if not endpoint or "/" not in endpoint or not app:
            return web.json_response({"message": "Invalid parameters"}, status=400)
        if self.post_behavior == "error":
            return web.json_response({"message": "Internal error"}, status=500)
        if self.post_behavior == "timeout":
            await asyncio.sleep(self.post_delay)
            return web.json_response({"message": "late"}, status=500)
        if self.post_behavior == "slow_create":
            # Like Asterisk, finish a request that has arrived even if the client goes away
            # (the test server cancels handlers of closed connections).
            create = asyncio.ensure_future(self._create_later(channel_id, endpoint, app))
            self._background.add(create)
            create.add_done_callback(self._background.discard)
            return web.json_response(self._channel_json(await asyncio.shield(create)))
        channel = Channel(id=channel_id, endpoint=endpoint, app=app)
        self.channels[channel_id] = channel
        channel.task = asyncio.create_task(self._run(channel, self.scenario_for(endpoint)))
        if self.post_behavior == "create_then_timeout":
            await asyncio.sleep(self.post_delay)
        return web.json_response(self._channel_json(channel))

    async def _create_later(self, channel_id: str, endpoint: str, app: str) -> Channel:
        await asyncio.sleep(self.post_delay)
        channel = Channel(id=channel_id, endpoint=endpoint, app=app)
        self.channels[channel_id] = channel
        channel.task = asyncio.create_task(self._run(channel, self.scenario_for(endpoint)))
        return channel

    async def get_channel(self, request: web.Request) -> web.Response:
        if self.get_behavior == "error":
            return web.json_response({"message": "Internal error"}, status=500)
        channel = self.channels.get(request.match_info["id"])
        if channel is None:
            return web.json_response({"message": "Channel not found"}, status=404)
        return web.json_response(self._channel_json(channel))

    async def delete_channel(self, request: web.Request) -> web.Response:
        channel = self.channels.get(request.match_info["id"])
        if channel is None:
            return web.json_response({"message": "Channel not found"}, status=404)
        if channel.task is not None:
            channel.task.cancel()
        await self._destroy(channel, 16, "Normal Clearing", None)
        return web.Response(status=204)

    async def play(self, request: web.Request) -> web.Response:
        channel = self.channels.get(request.match_info["id"])
        if channel is None:
            return web.json_response({"message": "Channel not found"}, status=404)
        playback = {
            "id": f"pb-{next(self._seq)}",
            "media_uri": request.query.get("media", ""),
            "target_uri": f"channel:{channel.id}",
            "state": "playing",
        }

        async def finish() -> None:
            await asyncio.sleep(self.playback_seconds)
            if channel.id in self.channels:
                await self.send(
                    channel.app,
                    {"type": "PlaybackFinished", "playback": {**playback, "state": "done"}},
                )

        asyncio.get_running_loop().create_task(finish())
        return web.json_response(playback, status=201)

    async def events(self, request: web.Request) -> web.StreamResponse:
        app = request.query.get("app", "")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        old = self.sockets.get(app)
        if old is not None and not old.closed:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await old.send_json({"type": "ApplicationReplaced", "application": app})
                await old.close()
        self.sockets[app] = ws
        self.ws_connects += 1
        async for message in ws:
            if message.type == WSMsgType.ERROR:
                break
        if self.sockets.get(app) is ws:
            del self.sockets[app]
        return ws

    def app(self) -> web.Application:
        application = web.Application(middlewares=[self._auth])
        application.router.add_get("/ari/asterisk/info", self.info)
        application.router.add_get("/ari/applications/{name}", self.application)
        application.router.add_get("/ari/endpoints/{tech}/{resource}", self.endpoint)
        application.router.add_post("/ari/channels/{id}", self.create_channel)
        application.router.add_get("/ari/channels/{id}", self.get_channel)
        application.router.add_delete("/ari/channels/{id}", self.delete_channel)
        application.router.add_post("/ari/channels/{id}/play", self.play)
        application.router.add_get("/ari/events", self.events)
        return application

    async def close(self) -> None:
        for task in list(self._background):
            task.cancel()
        for channel in list(self.channels.values()):
            if channel.task is not None:
                channel.task.cancel()
        await self.drop_websockets()
