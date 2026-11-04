"""The call service: dials attempts through a provider and keeps every call accounted for.

Only one dialer runs at a time (a lease in SQLite shared by all processes). The service
- re-checks everything when it writes the attempt (CallStore.begin_*) and once more right before
  the dial request, and only then asks the provider to dial;
- applies provider events, each once, and ends calls that must end: emergency stop, a cancel,
  a stopped campaign, do-not-call, a removed contact, no answer in time, the maximum duration;
- after a crash first settles calls that may still exist, before it dials anything new;
- ends a call the provider reports alive although wewln already counts it as ended.

With the voice engine (WEWLN_VOICE_MODE=ai) an answered call gets a spoken conversation: the
service connects the call's audio, runs the conversation, stores the transcript, records a
request not to be called again (do-not-call list, real calls only) and ends the call when the
conversation is over.

In this version real calls are possible only as a single test call to one number with the
operator's permit (`wewln calls permit`), or to a local softphone (no phone network at all).
Campaign calls over a real provider stay blocked until campaigns can be armed (Phase 6).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..domain import JobReason, SuppressionSource
from ..errors import RealCallsBlockedError, WewlnError
from ..phones import describe_number, dial_policy_problem
from ..safety import evaluate_real_calls
from ..storage.calls import CallStore, DialTicket, KillSwitchEngagedError, parse_iso
from ..storage.repository import InvalidInputError
from ..voice.engine import SessionResult, Turn
from ..voice.timing import fine_timer
from .base import (
    CallRequest,
    EventKind,
    ProviderError,
    ProviderEvent,
    ProviderUncertainError,
    TelephonyProvider,
)
from .lifecycle import (
    CALLER_HANGUP,
    HANGUP_UNCONFIRMED,
    INTERRUPTED,
    KILL_SWITCH,
    MAX_DURATION,
    MEDIA_LOST,
    REAL_CALLS_BLOCKED,
    RING_TIMEOUT,
    STATE_LOST,
    TERMINAL_STATES,
    VOICE_ERROR,
    CallState,
)

if TYPE_CHECKING:
    from ..voice.runtime import VoiceRuntime

log = logging.getLogger("wewln.calls")

DIALER_LOCK = "dialer"
LOCK_TTL_S = 30.0
LOCK_RENEW_S = 10.0
# The provider ends an unanswered call itself after the ring timeout; this is the backstop.
RING_GRACE_S = 15.0
# How long to wait for the hangup event before looking again, and when to give up.
HANGUP_RECHECK_S = 5.0
HANGUP_GIVE_UP_S = 60.0
MAX_HANGUP_TRIES = 5
# Ctrl+C while a dial request is on its way: how long to wait for the provider's answer, so
# that a call it created is known (and ended) rather than left running unseen.
PLACE_SETTLE_S = 12.0
# How long start() waits for the calls it ends after a crash, before anything new is dialed.
RECOVER_WAIT_S = 5.0
# The conversation's audio ended (the person hung up): how long to wait for the provider's
# hangup event before wewln ends the call itself.
MEDIA_END_GRACE_S = 3.0
# How long a finished call waits for its conversation to store its result.
CONVERSATION_SETTLE_S = 10.0


class DialerBusyError(WewlnError):
    code = "dialer_busy"
    http_status = 409


@dataclass(frozen=True)
class CallOutcome:
    attempt_id: int
    kind: str
    state: CallState
    end_reason: str | None
    simulated: bool
    cause: int | None = None
    tech_cause: int | None = None
    error_code: str | None = None
    job_id: int | None = None
    voice_end: str | None = None
    opt_out: bool = False
    voice_turns: int | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CallOutcome:
        return cls(
            attempt_id=row["id"],
            kind=row["kind"],
            state=CallState(row["state"]),
            end_reason=row["end_reason"],
            simulated=bool(row["simulated"]),
            cause=row["hangup_cause"],
            tech_cause=row["tech_cause"],
            error_code=row["error_code"],
            job_id=row["job_id"],
            voice_end=row.get("voice_end"),
            opt_out=bool(row.get("opt_out")),
            voice_turns=row.get("voice_turns"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "kind": self.kind,
            "state": self.state.value,
            "end_reason": self.end_reason,
            "simulated": self.simulated,
            "cause": self.cause,
            "tech_cause": self.tech_cause,
            "error_code": self.error_code,
            "job_id": self.job_id,
            "voice_end": self.voice_end,
            "opt_out": self.opt_out,
            "voice_turns": self.voice_turns,
        }


@dataclass
class RunReport:
    outcomes: list[CallOutcome] = field(default_factory=list)
    # Why the run ended early: kill_switch, lock_lost or a provider error code.
    stopped_by: str | None = None
    error: str | None = None

    @property
    def placed(self) -> int:
        return len(self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "placed": self.placed,
            "stopped_by": self.stopped_by,
            "error": self.error,
            "calls": [o.as_dict() for o in self.outcomes],
        }


class CallService:
    def __init__(
        self,
        settings: Settings,
        store: CallStore,
        provider: TelephonyProvider,
        *,
        owner: str | None = None,
        sweep_interval: float = 0.5,
        ring_grace_s: float = RING_GRACE_S,
        place_settle_s: float = PLACE_SETTLE_S,
        campaign_armed: Callable[[], bool] | None = None,
        voice: VoiceRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self.owner = owner or f"dialer:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.sweep_interval = sweep_interval
        self.ring_grace_s = ring_grace_s
        self.place_settle_s = place_settle_s
        # The operator's arming of campaign calls arrives in Phase 6; nothing arms them now.
        self._campaign_armed = campaign_armed or (lambda: False)
        self._changed = asyncio.Event()
        self._sweeper: asyncio.Task[None] | None = None
        self._started = False
        self._lock_lost = False
        self._lock_renewed = 0.0
        # Attempts whose dial request is on its way: the provider may not know the call yet.
        self._placing: set[int] = set()
        # Hangups of calls that were reported alive after they ended in wewln.
        self._late_hangups: set[asyncio.Task[None]] = set()
        self._stuck_reported: set[int] = set()
        # The voice engine: a conversation per answered call (by provider call ID).
        self.voice = voice
        self._conversations: dict[str, asyncio.Task[None]] = {}
        # Numbers of live voice calls, for a request not to be called again (never logged).
        self._numbers: dict[str, str] = {}
        self.conversations: dict[int, SessionResult] = {}
        self._timer: contextlib.ExitStack | None = None

    @property
    def simulated(self) -> bool:
        return not self.provider.info.places_real_calls

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.voice is not None:
            # Before anything is dialed: the models are loaded and the call audio is set up.
            self.provider.enable_media()
            await self.voice.start()
        db = self.store.db
        if not await asyncio.to_thread(db.acquire_lock, DIALER_LOCK, self.owner, LOCK_TTL_S):
            holder = await asyncio.to_thread(db.lock_holder, DIALER_LOCK)
            raise DialerBusyError(f"another wewln dialer is running ({holder}); wait for it")
        self._lock_renewed = time.monotonic()
        try:
            await self.provider.start(self._on_event)
            await self.recover()
            await asyncio.to_thread(self.store.prune_events)
            if self.voice is not None:
                await asyncio.to_thread(
                    self.store.prune_turns, days=self.settings.voice_transcript_days
                )
        except BaseException:
            with contextlib.suppress(Exception):
                await self.provider.close()
            await asyncio.to_thread(db.release_lock, DIALER_LOCK, self.owner)
            raise
        self._started = True
        if self.voice is not None:
            self._timer = contextlib.ExitStack()
            self._timer.enter_context(fine_timer())
        self._sweeper = asyncio.create_task(self._sweep_loop())
        log.info(
            "dialer_started",
            extra={"provider": self.provider.info.name, "voice": self.voice is not None},
        )

    async def close(self) -> None:
        if self._conversations:
            # Shutdown: a conversation in progress ends its call.
            tasks = set(self._conversations.values())
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks, timeout=HANGUP_RECHECK_S)
        if self._timer is not None:
            self._timer.close()
            self._timer = None
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        if self._late_hangups:
            await asyncio.wait(set(self._late_hangups), timeout=HANGUP_RECHECK_S)
        with contextlib.suppress(Exception):
            await self.provider.close()
        if self._started:
            self._started = False
            await asyncio.to_thread(self.store.db.release_lock, DIALER_LOCK, self.owner)
            log.info("dialer_stopped")

    async def __aenter__(self) -> CallService:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _on_event(self, event: ProviderEvent) -> None:
        try:
            outcome = await asyncio.to_thread(self.store.apply_event, event)
        except Exception:
            log.exception("call_event_failed", extra={"source": event.source})
            return
        log.info(
            "call_event",
            extra={"kind": event.kind.value, "outcome": outcome, "source": event.source},
        )
        self._changed.set()
        if (
            outcome == "applied"
            and event.kind == EventKind.ANSWERED
            and self.voice is not None
            and event.call_id not in self._conversations
        ):
            task = asyncio.create_task(self._converse(event.call_id))
            self._conversations[event.call_id] = task
            task.add_done_callback(lambda _t, cid=event.call_id: self._conversations.pop(cid, None))
        if outcome == "after_end":
            # Progress on a call that ended in wewln without the provider reporting its end: it
            # must not live on. Decided when the event was applied (a repeated ringing signal
            # just before a hangup is not such a call). In its own task: the provider may be
            # delivering this event from the call it would end.
            task = asyncio.create_task(self._end_late_call(event.call_id))
            self._late_hangups.add(task)
            task.add_done_callback(self._late_hangups.discard)

    async def _end_late_call(self, call_id: str) -> None:
        """The provider reports progress on a call that ended in wewln (its hangup was never
        confirmed, or the provider created it after wewln gave up waiting): end it."""
        attempt = await asyncio.to_thread(self.store.find_attempt, call_id)
        if attempt is None:
            return
        log.warning("call_ended_late", extra={"attempt_id": attempt["id"]})
        try:
            await self.provider.hangup(call_id)
        except ProviderError as exc:
            log.error("call_hangup_failed", extra={"attempt_id": attempt["id"], "error": exc.code})

    def kill_switch_engaged(self) -> bool:
        return self.settings.kill_switch or self.store.kill_switch() is not None

    # ------------------------------------------------------------------ conversations

    async def _converse(self, call_id: str) -> None:
        """Talk with the person on an answered call, then end the call (unless they did)."""
        attempt = await asyncio.to_thread(self.store.find_attempt, call_id)
        if attempt is None or self.voice is None:
            return
        attempt_id = attempt["id"]
        extra = {"attempt_id": attempt_id}
        try:
            try:
                result = await self._talk(call_id, attempt_id)
            except asyncio.CancelledError:
                # Shutdown: the call must not live on without anyone talking.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._end_if_active(attempt_id, INTERRUPTED))
                raise
            except Exception:
                log.exception("voice_conversation_failed", extra=extra)
                result = SessionResult(
                    VOICE_ERROR, hangup=True, opt_out=False, turns=[], error="internal_error"
                )
            self.conversations[attempt_id] = result
            voice_end = result.reason
            if voice_end == CALLER_HANGUP:
                fresh = await asyncio.to_thread(self.store.get_attempt, attempt_id)
                # The audio ended because wewln ended the call (time limit, emergency stop).
                voice_end = fresh["end_request"] or voice_end
            log.info(
                "voice_conversation_ended",
                extra={
                    **extra,
                    "reason": voice_end,
                    "caller_turns": result.caller_turns,
                    "opt_out": result.opt_out,
                    "error": result.error,
                },
            )
            try:
                await asyncio.to_thread(
                    self.store.record_conversation,
                    attempt_id,
                    voice_end=voice_end,
                    opt_out=result.opt_out,
                    caller_turns=result.caller_turns,
                )
                if result.opt_out:
                    await self._record_opt_out(attempt_id, call_id)
            except Exception:
                log.exception("voice_result_not_saved", extra=extra)
            if result.hangup:
                await self._end_if_active(attempt_id, result.reason)
            else:
                await self._settle_after_media_end(attempt_id)
        finally:
            self._numbers.pop(call_id, None)
            self._changed.set()

    async def _talk(self, call_id: str, attempt_id: int) -> SessionResult:
        assert self.voice is not None  # noqa: S101 - checked by _converse
        script = await asyncio.to_thread(self.store.conversation_script, attempt_id)
        try:
            media = await self.provider.open_media(call_id)
        except ProviderError as exc:
            log.error(
                "voice_media_failed",
                extra={"attempt_id": attempt_id, "error": exc.code, "detail": exc.message},
            )
            return SessionResult(VOICE_ERROR, hangup=True, opt_out=False, turns=[], error=exc.code)
        log.info("voice_conversation_started", extra={"attempt_id": attempt_id})
        session = self.voice.session(
            media,
            script=script,
            on_turn=self._turn_sink(attempt_id),
            label=f"attempt:{attempt_id}",
        )
        return await session.run()

    def _turn_sink(self, attempt_id: int) -> Callable[[Turn], Any]:
        async def store_turn(turn: Turn) -> None:
            if self.settings.voice_store_transcripts:
                await asyncio.to_thread(self.store.add_turn, attempt_id, asdict(turn))

        return store_turn

    async def _record_opt_out(self, attempt_id: int, call_id: str) -> None:
        """The person asked not to be called again: their number goes on the do-not-call list.
        Not for simulated calls (nobody real asked) and not for a softphone (no number)."""
        attempt = await asyncio.to_thread(self.store.get_attempt, attempt_id)
        number = self._numbers.get(call_id)
        if attempt["simulated"] or not number:
            log.info("voice_opt_out_not_listed", extra={"attempt_id": attempt_id})
            return
        await asyncio.to_thread(
            self.store.repo.add_do_not_call,
            number,
            reason=f"asked not to be called again (call {attempt_id})",
            source=SuppressionSource.OPT_OUT,
        )
        log.warning("voice_opt_out_listed", extra={"attempt_id": attempt_id})

    async def _end_if_active(self, attempt_id: int, reason: str) -> None:
        fresh = await asyncio.to_thread(self.store.get_attempt, attempt_id)
        if CallState(fresh["state"]) not in TERMINAL_STATES:
            await self._end(fresh, reason)

    async def _settle_after_media_end(self, attempt_id: int) -> None:
        """The call's audio ended without wewln ending the call: normally the person hung up
        and the provider reports it. Otherwise the audio was lost: the call is ended."""
        deadline = time.monotonic() + MEDIA_END_GRACE_S
        while time.monotonic() < deadline:
            fresh = await asyncio.to_thread(self.store.get_attempt, attempt_id)
            if CallState(fresh["state"]) in TERMINAL_STATES or fresh["end_request"]:
                return
            await asyncio.sleep(0.1)
        await self._end_if_active(attempt_id, MEDIA_LOST)

    async def _conversation_done(self, call_id: str) -> None:
        task = self._conversations.get(call_id)
        if task is not None:
            await asyncio.wait({task}, timeout=CONVERSATION_SETTLE_S)

    # ------------------------------------------------------------------ recovery and sweeping

    async def recover(self) -> int:
        """Settle attempts left by a previous run, before anything new is dialed. A call that
        may still exist is ended (no conversation is resumed), also when the provider cannot
        tell; a call that is gone ends as "state lost". Neither is repeated."""
        attempts = await asyncio.to_thread(self.store.active_attempts)
        ending: set[int] = set()
        for attempt in attempts:
            if attempt["provider"] != self.provider.info.name:
                # Placed through another provider: it cannot be checked from here.
                log.warning("call_not_verifiable", extra={"attempt_id": attempt["id"]})
                exists: bool | None = False
            else:
                try:
                    exists = await self.provider.call_exists(attempt["provider_call_id"])
                except ProviderError:
                    exists = None
            if exists is not False:
                await self._end(attempt, STATE_LOST)
                ending.add(attempt["id"])
            else:
                answered = attempt["state"] == CallState.ANSWERED
                forced = (CallState.COMPLETED if answered else CallState.FAILED, STATE_LOST)
                await asyncio.to_thread(
                    self.store.finish_without_event, attempt["id"], forced=forced
                )
        if ending:
            await self._wait_ended(ending, RECOVER_WAIT_S)
        if attempts:
            log.warning("calls_recovered", extra={"count": len(attempts)})
        return len(attempts)

    async def _wait_ended(self, attempt_ids: set[int], wait_s: float) -> None:
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            active = await asyncio.to_thread(self.store.active_attempts)
            if not any(a["id"] in attempt_ids for a in active):
                return
            await asyncio.sleep(0.1)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("call_sweep_failed")
            await asyncio.sleep(self.sweep_interval)

    async def _renew_lock(self) -> None:
        if time.monotonic() - self._lock_renewed < LOCK_RENEW_S:
            return
        db = self.store.db
        if await asyncio.to_thread(db.acquire_lock, DIALER_LOCK, self.owner, LOCK_TTL_S):
            self._lock_renewed = time.monotonic()
        else:
            self._lock_lost = True
            log.error("dialer_lock_lost")

    async def sweep(self) -> None:
        """End calls that must end now, and settle hangups the provider never confirmed."""
        await self._renew_lock()
        now = self.store.now()
        kill = await asyncio.to_thread(self.kill_switch_engaged)
        for attempt in await asyncio.to_thread(self.store.active_attempts):
            if attempt["provider"] != self.provider.info.name:
                continue
            state = CallState(attempt["state"])
            if attempt["end_request"] is None:
                started = parse_iso(attempt["dial_started_at"] or attempt["created_at"])
                reason = None
                if kill:
                    reason = KILL_SWITCH
                elif attempt["cancel_requested"]:
                    reason = attempt["cancel_requested"]
                elif attempt["interrupt"]:
                    reason = attempt["interrupt"]
                elif state == CallState.ANSWERED:
                    answered = parse_iso(attempt["answered_at"])
                    if (now - answered).total_seconds() > self.settings.call_max_duration:
                        reason = MAX_DURATION
                elif (
                    now - started
                ).total_seconds() > self.settings.call_ring_timeout + self.ring_grace_s:
                    reason = RING_TIMEOUT
                if reason is not None:
                    await self._end(attempt, reason)
                continue
            if attempt["id"] in self._placing:
                continue  # _place hangs up as soon as the dial request returns
            since = (now - parse_iso(attempt["hangup_requested_at"])).total_seconds()
            if since < HANGUP_RECHECK_S:
                continue
            try:
                exists = await self.provider.call_exists(attempt["provider_call_id"])
            except ProviderError:
                exists = None
            if exists is False:
                await asyncio.to_thread(self._finish_gone, attempt)
            elif exists is True and attempt["hangup_tries"] < MAX_HANGUP_TRIES:
                await self._end(attempt, attempt["end_request"])
            elif exists is True:
                # Never forgotten while it may be live: the operator must end it in Asterisk.
                if attempt["id"] not in self._stuck_reported:
                    self._stuck_reported.add(attempt["id"])
                    log.error("call_hangup_not_possible", extra={"attempt_id": attempt["id"]})
            elif since > HANGUP_GIVE_UP_S:
                await asyncio.to_thread(
                    self.store.finish_without_event,
                    attempt["id"],
                    forced=(CallState.FAILED, HANGUP_UNCONFIRMED),
                )
            self._changed.set()

    def _finish_gone(self, attempt: dict[str, Any]) -> None:
        """The provider does not know the call (any more) and no hangup event came."""
        forced = None
        state = CallState(attempt["state"])
        uncertain = attempt["error_code"] == ProviderUncertainError.code
        if uncertain and state in (CallState.PENDING, CallState.DIALING):
            forced = (CallState.FAILED, ProviderUncertainError.code)
        self.store.finish_without_event(attempt["id"], forced=forced)

    async def _end(self, attempt: dict[str, Any], reason: str) -> None:
        """Ask the provider to end a call; the reason is stored first."""
        if not await asyncio.to_thread(self.store.request_hangup, attempt["id"], reason):
            return
        if attempt["id"] in self._placing:
            # The dial request is on its way and the provider may not know the call yet:
            # _place hangs up as soon as the request returns.
            log.info("call_hangup_deferred", extra={"attempt_id": attempt["id"], "reason": reason})
            return
        log.info("call_hangup", extra={"attempt_id": attempt["id"], "reason": reason})
        try:
            known = await self.provider.hangup(attempt["provider_call_id"])
        except ProviderError as exc:
            log.warning(
                "call_hangup_failed", extra={"attempt_id": attempt["id"], "error": exc.code}
            )
            return
        if not known:
            if attempt["id"] in self._placing:
                # Sent just before the dial request, which may still create the call: _place
                # ends it when the request returns.
                return
            fresh = await asyncio.to_thread(self.store.get_attempt, attempt["id"])
            if CallState(fresh["state"]) not in TERMINAL_STATES:
                await asyncio.to_thread(self._finish_gone, fresh)
        self._changed.set()

    async def hangup_all(self, reason: str, *, wait_s: float = 5.0) -> int:
        """End every live call of this provider (Ctrl+C, shutdown); waits briefly for them."""
        attempts = [
            a
            for a in await asyncio.to_thread(self.store.active_attempts)
            if a["provider"] == self.provider.info.name
        ]
        for attempt in attempts:
            await self._end(attempt, reason)
        await self._wait_ended({a["id"] for a in attempts}, wait_s)
        return len(attempts)

    # ------------------------------------------------------------------ dialing

    def _dial_blocker(self, ticket: DialTicket) -> str | None:
        """Why this attempt may not be dialed now, read again right before the dial request.

        A stop since the attempt was written (emergency stop, cancel, stopped campaign,
        do-not-call, removed contact) returns its reason. A real call that is no longer
        permitted (production mode, the permit for the number, the allowed countries, campaign
        arming) raises RealCallsBlockedError.
        """
        if self.settings.kill_switch:
            return KILL_SWITCH
        real = not self.simulated and ticket.target == "phone"
        reason = self.store.dial_blocker(
            ticket.attempt_id,
            phone_e164=ticket.to_e164 or None,
            real_test=real and ticket.kind == "test",
        )
        if reason is not None or not real:
            return reason
        # A test call's permit was checked just above; campaigns need the operator's arming.
        self._real_gate(operator_armed=ticket.kind == "test" or self._campaign_armed())
        problem = dial_policy_problem(ticket.to_e164, self.settings.allowed_call_regions)
        if problem is not None:
            raise RealCallsBlockedError(
                f"this number may not be called ({problem})", details={"blocked_by": [problem]}
            )
        return None

    def _stop_before_dial(self, attempt_id: int, reason: str) -> None:
        self.store.request_hangup(attempt_id, reason)
        self.store.finish_without_event(attempt_id)

    async def _place(self, ticket: DialTicket) -> None:
        request = CallRequest(
            call_id=ticket.call_id,
            to_e164=ticket.to_e164,
            ring_timeout_s=self.settings.call_ring_timeout,
            caller_id=self.settings.caller_id,
            local_endpoint=ticket.local_endpoint,
        )
        if self.voice is not None and ticket.target == "phone" and ticket.to_e164:
            self._numbers[ticket.call_id] = ticket.to_e164
        self._placing.add(ticket.attempt_id)
        try:
            try:
                blocker = await asyncio.to_thread(self._dial_blocker, ticket)
            except RealCallsBlockedError:
                await asyncio.to_thread(
                    self.store.abandon_attempt,
                    ticket.attempt_id,
                    error_code=REAL_CALLS_BLOCKED,
                    job_reason=None,
                )
                log.warning(
                    "call_not_placed",
                    extra={"attempt_id": ticket.attempt_id, "reason": REAL_CALLS_BLOCKED},
                )
                raise
            if blocker is not None:
                # A stop came since the attempt was written: nothing is dialed.
                await asyncio.to_thread(self._stop_before_dial, ticket.attempt_id, blocker)
                log.info(
                    "call_not_placed", extra={"attempt_id": ticket.attempt_id, "reason": blocker}
                )
                return
            error_code = await self._send(ticket, request)
            await asyncio.to_thread(
                self.store.mark_dialing, ticket.attempt_id, error_code=error_code
            )
        finally:
            self._placing.discard(ticket.attempt_id)
        if error_code is not None:
            log.warning("call_place_uncertain", extra={"attempt_id": ticket.attempt_id})
        else:
            log.info(
                "call_placed",
                extra={
                    "attempt_id": ticket.attempt_id,
                    "kind": ticket.kind,
                    "target": ticket.target,
                },
            )
        # An emergency stop or a cancel while the request was on its way was only recorded
        # (see _end): end the call now that it may exist.
        fresh = await asyncio.to_thread(self.store.get_attempt, ticket.attempt_id)
        if fresh["end_request"] is not None and CallState(fresh["state"]) not in TERMINAL_STATES:
            await self._end(fresh, fresh["end_request"])

    async def _send(self, ticket: DialTicket, request: CallRequest) -> str | None:
        """Send the dial request. Returns provider_uncertain when the call may exist although
        no answer came; raises ProviderError (after giving the attempt back) when it certainly
        does not exist."""
        dial = asyncio.ensure_future(self.provider.place_call(request))
        try:
            await asyncio.shield(dial)
        except ProviderUncertainError as exc:
            # The call may exist: it is watched like any other call and never repeated.
            return exc.code
        except ProviderError as exc:
            await asyncio.to_thread(
                self.store.abandon_attempt,
                ticket.attempt_id,
                error_code=exc.code,
                job_reason=JobReason.PROVIDER_ERROR,
            )
            log.error(
                "call_place_failed", extra={"attempt_id": ticket.attempt_id, "error": exc.code}
            )
            raise
        except BaseException:
            # Ctrl+C, shutdown or an unexpected error while the request was on its way. The
            # provider's answer is awaited (briefly): a call it created is then known and
            # _call ends it. Without an answer the call may exist: it is never repeated.
            try:
                if not dial.done():
                    log.warning("call_place_interrupted", extra={"attempt_id": ticket.attempt_id})
                    await asyncio.wait({dial}, timeout=self.place_settle_s)
            finally:
                try:
                    self._record_unfinished_dial(ticket.attempt_id, dial)
                except Exception:
                    log.exception("call_state_not_saved", extra={"attempt_id": ticket.attempt_id})
            raise
        return None

    def _record_unfinished_dial(self, attempt_id: int, dial: asyncio.Future[Any]) -> None:
        error: BaseException | None
        if not dial.done():
            dial.cancel()
            error = ProviderUncertainError("no answer to the interrupted dial request")
        elif dial.cancelled():
            error = ProviderUncertainError("the dial request was cancelled")
        else:
            error = dial.exception()
        if isinstance(error, ProviderError) and not error.call_may_exist:
            self.store.abandon_attempt(
                attempt_id, error_code=error.code, job_reason=JobReason.PROVIDER_ERROR
            )
        else:
            # Taken by the provider (the call exists) or unknown: watched, ended, not repeated.
            code = None if error is None else ProviderUncertainError.code
            self.store.mark_dialing(attempt_id, error_code=code)

    async def wait_for_end(self, attempt_id: int) -> CallOutcome:
        while True:
            row = await asyncio.to_thread(self.store.get_attempt, attempt_id)
            if CallState(row["state"]) in TERMINAL_STATES:
                return CallOutcome.from_row(row)
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), self.sweep_interval)

    async def _call(self, ticket: DialTicket) -> CallOutcome:
        try:
            await self._place(ticket)
            outcome = await self.wait_for_end(ticket.attempt_id)
            if ticket.call_id in self._conversations:
                # The conversation stores how it ended (and the transcript) just after.
                await self._conversation_done(ticket.call_id)
                outcome = await self.wait_for_end(ticket.attempt_id)
            return outcome
        except (ProviderError, RealCallsBlockedError):
            raise  # nothing was dialed (see _place)
        except BaseException:
            # Ctrl+C, shutdown or an unexpected error during a call: end it instead of
            # leaving it running.
            with contextlib.suppress(Exception):
                await asyncio.shield(self.hangup_all(INTERRUPTED))
            raise
        finally:
            if ticket.call_id not in self._conversations:
                self._numbers.pop(ticket.call_id, None)

    def _real_gate(self, *, operator_armed: bool) -> None:
        decision = evaluate_real_calls(
            self.settings,
            operator_armed=operator_armed,
            kill_switch_engaged=self.store.kill_switch() is not None,
        )
        if not decision.allowed:
            raise RealCallsBlockedError(
                "Real calls are blocked: " + ", ".join(r.value for r in decision.reasons),
                details=decision.as_dict(),
            )

    async def run_jobs(self, *, limit: int = 10, campaign_id: int | None = None) -> RunReport:
        """Call queued jobs one at a time (mock provider: simulated calls)."""
        if not self.simulated:
            await asyncio.to_thread(self._real_gate, operator_armed=self._campaign_armed())
        report = RunReport()
        while report.placed < limit:
            if self._lock_lost:
                report.stopped_by = "lock_lost"
                break
            if await asyncio.to_thread(self.kill_switch_engaged):
                report.stopped_by = "kill_switch"
                break
            try:
                ticket = await asyncio.to_thread(
                    self.store.begin_job_attempt,
                    provider=self.provider.info.name,
                    simulated=self.simulated,
                    campaign_id=campaign_id,
                )
            except KillSwitchEngagedError:
                report.stopped_by = "kill_switch"
                break
            if ticket is None:
                break
            try:
                outcome = await self._call(ticket)
            except ProviderError as exc:
                report.stopped_by = exc.code
                report.error = exc.message
                break
            report.outcomes.append(outcome)
        return report

    async def test_call(
        self, *, to_e164: str | None = None, softphone: bool = False, actor: str = "cli"
    ) -> CallOutcome:
        """One test call: to a local softphone (no phone network) or to one number. A real call
        to a number needs the whole real-call gate, including the operator's permit."""
        if await asyncio.to_thread(self.kill_switch_engaged):
            raise KillSwitchEngagedError("the emergency stop is engaged (wewln calls resume)")
        if softphone:
            ticket = await asyncio.to_thread(
                self.store.begin_test_attempt,
                provider=self.provider.info.name,
                simulated=self.simulated,
                local_endpoint=self.settings.asterisk_softphone_endpoint,
                actor=actor,
            )
        else:
            if to_e164 is None:
                raise InvalidInputError("give the number to call")
            problem = dial_policy_problem(to_e164, self.settings.allowed_call_regions)
            if problem is not None:
                raise InvalidInputError(f"this number may not be called ({problem})")
            info = describe_number(to_e164)
            if info is None:  # pragma: no cover - the policy check above rejects these
                raise InvalidInputError("this number may not be called (invalid)")
            to_e164 = info.e164  # the canonical form permits and do-not-call entries use
            if not self.simulated:
                armed = await asyncio.to_thread(self.store.has_valid_permit, to_e164)
                await asyncio.to_thread(self._real_gate, operator_armed=armed)
            ticket = await asyncio.to_thread(
                self.store.begin_test_attempt,
                provider=self.provider.info.name,
                simulated=self.simulated,
                to_e164=to_e164,
                require_permit=not self.simulated,
                actor=actor,
            )
        return await self._call(ticket)
