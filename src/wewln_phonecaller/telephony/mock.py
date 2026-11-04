"""Simulated provider: realistic call states with deterministic scenarios, no network at all.

Scenarios (times in seconds from the dial; `time_scale=0` makes them instant for tests):
- answer: ringing at 0.3, answered at 1.0, the person hangs up at 3.0;
- busy, reject, unreachable, fail: the network ends the call with the matching cause;
- no_answer: rings until the ring timeout, then the network gives up;
- silent: no event at all (the call service's own timeouts must end it);
- answer_hold: answered and never hung up (the maximum call duration must end it);
- mixed: chosen by the last digit of the number (0-4 answer, 5-6 no_answer, 7 busy, 8 reject,
  9 fail), so a list of contacts produces a believable mix.

With call audio enabled (voice mode) an answered call lasts until wewln hangs up or the
simulated person (`caller`, e.g. voice.simulate.SimulatedCaller) hangs up; the scenario's own
hangup is skipped. Without a `caller` the person stays silent on the line.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..voice.transport import MediaStream, memory_pair
from . import PROVIDERS
from .base import (
    CallRequest,
    EventKind,
    EventSink,
    MediaError,
    PlaceCallResult,
    ProviderEvent,
    ProviderUnavailableError,
    TelephonyProvider,
)

RING_TIMEOUT_STEP = -1.0  # placeholder delay: "at the request's ring timeout"
# The simulated person on an answered call: gets the phone's end of the audio and returns when
# they hang up.
MockCaller = Callable[[CallRequest, MediaStream], Awaitable[None]]


async def silent_caller(request: CallRequest, phone: MediaStream) -> None:
    """Picks up and says nothing until the call ends."""
    while await phone.read() is not None:
        pass


@dataclass(frozen=True)
class MockStep:
    at: float
    kind: EventKind
    cause: int | None = None
    cause_txt: str | None = None
    tech_cause: int | None = None


SCENARIOS: dict[str, tuple[MockStep, ...]] = {
    "answer": (
        MockStep(0.3, EventKind.RINGING),
        MockStep(1.0, EventKind.ANSWERED),
        MockStep(3.0, EventKind.HANGUP, 16, "Normal Clearing"),
    ),
    "busy": (
        MockStep(0.3, EventKind.RINGING),
        MockStep(1.0, EventKind.HANGUP, 17, "User busy", 486),
    ),
    "no_answer": (
        MockStep(0.3, EventKind.RINGING),
        MockStep(RING_TIMEOUT_STEP, EventKind.HANGUP, 19, "No answer"),
    ),
    "reject": (
        MockStep(0.3, EventKind.RINGING),
        MockStep(1.5, EventKind.HANGUP, 21, "Call Rejected", 603),
    ),
    "unreachable": (MockStep(0.3, EventKind.HANGUP, 1, "Unallocated (unassigned) number", 404),),
    "fail": (MockStep(0.3, EventKind.HANGUP, 34, "Circuit/channel congestion", 503),),
    "silent": (),
    "answer_hold": (MockStep(0.3, EventKind.RINGING), MockStep(1.0, EventKind.ANSWERED)),
}
MIXED_BY_LAST_DIGIT = {
    **dict.fromkeys("01234", "answer"),
    **dict.fromkeys("56", "no_answer"),
    "7": "busy",
    "8": "reject",
    "9": "fail",
}
SCENARIO_NAMES = (*SCENARIOS, "mixed")


def scenario_for(name: str, number: str) -> str:
    if name == "mixed":
        return MIXED_BY_LAST_DIGIT.get(number[-1:], "answer")
    if name not in SCENARIOS:
        raise ValueError(f"unknown mock scenario {name!r}")
    return name


class MockTelephonyProvider(TelephonyProvider):
    info = PROVIDERS["mock"]
    supports_media = True

    def __init__(
        self,
        scenario: str | Callable[[CallRequest], str] = "answer",
        *,
        time_scale: float = 1.0,
        duplicate_events: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        caller: MockCaller | None = None,
    ) -> None:
        self._scenario = scenario
        self._caller = caller
        self._phones: dict[str, tuple[MediaStream, asyncio.Task[None]]] = {}
        self._requests: dict[str, CallRequest] = {}
        self._scale = max(0.0, time_scale)
        self._duplicates = duplicate_events
        self._sleep = sleep
        self._sink: EventSink | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._answered: set[str] = set()
        self._seq = itertools.count(1)
        self.placed: list[CallRequest] = []
        self.hangups: list[str] = []

    async def start(self, sink: EventSink) -> None:
        self._sink = sink

    async def close(self) -> None:
        for call_id in list(self._phones):
            await self._drop_phone(call_id)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._sink = None

    def scenario_name(self, request: CallRequest) -> str:
        name = self._scenario(request) if callable(self._scenario) else self._scenario
        return scenario_for(name, request.local_endpoint or request.to_e164)

    async def place_call(self, request: CallRequest) -> PlaceCallResult:
        if self._sink is None:
            raise ProviderUnavailableError("the mock provider is not started")
        if request.call_id in self._tasks or any(r.call_id == request.call_id for r in self.placed):
            return PlaceCallResult(request.call_id, already_existed=True)
        self.placed.append(request)
        self._requests[request.call_id] = request
        steps = SCENARIOS[self.scenario_name(request)]
        self._tasks[request.call_id] = asyncio.create_task(self._run(request, steps))
        return PlaceCallResult(request.call_id)

    async def _run(self, request: CallRequest, steps: tuple[MockStep, ...]) -> None:
        elapsed = 0.0
        try:
            for step in steps:
                at = request.ring_timeout_s if step.at == RING_TIMEOUT_STEP else step.at
                await self._sleep(max(0.0, at - elapsed) * self._scale)
                elapsed = at
                if step.kind == EventKind.ANSWERED:
                    self._answered.add(request.call_id)
                await self._emit(request.call_id, step)
                if step.kind == EventKind.HANGUP:
                    return
                if step.kind == EventKind.ANSWERED and self.media_enabled:
                    break  # the conversation decides when the call ends
            # No hangup in the scenario (silent, answer_hold): the call lasts until hung up.
            await asyncio.Event().wait()
        finally:
            if self._tasks.get(request.call_id) is asyncio.current_task():
                del self._tasks[request.call_id]

    async def _emit(self, call_id: str, step: MockStep) -> None:
        if self._sink is None:
            return
        event = ProviderEvent(
            call_id=call_id,
            kind=step.kind,
            event_key=f"mock:{call_id}:{next(self._seq)}",
            source="mock",
            cause=step.cause,
            cause_txt=step.cause_txt,
            tech_cause=step.tech_cause,
        )
        await self._sink(event)
        if self._duplicates:
            await self._sink(event)

    async def hangup(self, call_id: str) -> bool:
        task = self._tasks.pop(call_id, None)
        if task is None:
            return False
        self.hangups.append(call_id)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self._drop_phone(call_id)
        # As a PBX does: the hangup we asked for comes back as an event.
        await self._emit(call_id, MockStep(0, EventKind.HANGUP, 16, "Normal Clearing"))
        return True

    async def open_media(self, call_id: str) -> MediaStream:
        if not self.media_enabled:
            raise MediaError("call audio is not enabled")
        if call_id not in self._tasks or call_id not in self._answered:
            raise MediaError("the call is not answered (or already over)")
        if call_id in self._phones:
            raise MediaError("the call's audio is already connected")
        line, phone = memory_pair()
        request = self._requests[call_id]
        task = asyncio.create_task(self._person(call_id, request, line, phone))
        self._phones[call_id] = (line, task)
        return line

    async def _person(
        self, call_id: str, request: CallRequest, line: MediaStream, phone: MediaStream
    ) -> None:
        try:
            await (self._caller or silent_caller)(request, phone)
        finally:
            # wewln closed the audio (the call itself ends when wewln hangs up), or not.
            by_wewln = bool(getattr(line, "closed_here", False))
            await phone.close()
        if by_wewln:
            return
        # The person hung up: the call ends, reported like any other hangup.
        call = self._tasks.pop(call_id, None)
        if call is None:
            return
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call
        self._phones.pop(call_id, None)
        await self._emit(call_id, MockStep(0, EventKind.HANGUP, 16, "Normal Clearing"))

    async def _drop_phone(self, call_id: str) -> None:
        entry = self._phones.pop(call_id, None)
        if entry is None:
            return
        line, person = entry
        await line.close()
        if person is not asyncio.current_task():
            person.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await person

    async def call_exists(self, call_id: str) -> bool | None:
        return call_id in self._tasks

    async def check(self) -> dict[str, Any]:
        return {"status": "ok", "provider": "mock", "places_real_calls": False}
