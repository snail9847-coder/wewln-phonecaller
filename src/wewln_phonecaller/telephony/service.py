"""The call service: dials attempts through a provider and keeps every call accounted for.

Only one dialer runs at a time (a lease in SQLite shared by all processes). The service
- re-checks everything at dial time (CallStore.begin_*) and only then asks the provider to dial;
- applies provider events, each once, and ends calls that must end: emergency stop, a cancel,
  a stopped campaign, do-not-call, a removed contact, no answer in time, the maximum duration;
- after a crash first settles calls that may still exist, before it dials anything new.

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
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..domain import JobReason
from ..errors import RealCallsBlockedError, WewlnError
from ..phones import dial_policy_problem
from ..safety import evaluate_real_calls
from ..storage.calls import CallStore, DialTicket, KillSwitchEngagedError, parse_iso
from ..storage.repository import InvalidInputError
from .base import (
    CallRequest,
    ProviderError,
    ProviderEvent,
    ProviderUncertainError,
    TelephonyProvider,
)
from .lifecycle import (
    HANGUP_UNCONFIRMED,
    INTERRUPTED,
    KILL_SWITCH,
    MAX_DURATION,
    RING_TIMEOUT,
    STATE_LOST,
    TERMINAL_STATES,
    CallState,
)

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
        campaign_armed: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self.owner = owner or f"dialer:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.sweep_interval = sweep_interval
        self.ring_grace_s = ring_grace_s
        # The operator's arming of campaign calls arrives in Phase 6; nothing arms them now.
        self._campaign_armed = campaign_armed or (lambda: False)
        self._changed = asyncio.Event()
        self._sweeper: asyncio.Task[None] | None = None
        self._started = False
        self._lock_lost = False
        self._lock_renewed = 0.0

    @property
    def simulated(self) -> bool:
        return not self.provider.info.places_real_calls

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        db = self.store.db
        if not await asyncio.to_thread(db.acquire_lock, DIALER_LOCK, self.owner, LOCK_TTL_S):
            holder = await asyncio.to_thread(db.lock_holder, DIALER_LOCK)
            raise DialerBusyError(f"another wewln dialer is running ({holder}); wait for it")
        self._lock_renewed = time.monotonic()
        try:
            await self.provider.start(self._on_event)
            await self.recover()
            await asyncio.to_thread(self.store.prune_events)
        except BaseException:
            with contextlib.suppress(Exception):
                await self.provider.close()
            await asyncio.to_thread(db.release_lock, DIALER_LOCK, self.owner)
            raise
        self._started = True
        self._sweeper = asyncio.create_task(self._sweep_loop())
        log.info("dialer_started", extra={"provider": self.provider.info.name})

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
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

    def kill_switch_engaged(self) -> bool:
        return self.settings.kill_switch or self.store.kill_switch() is not None

    # ------------------------------------------------------------------ recovery and sweeping

    async def recover(self) -> int:
        """Settle attempts left by a previous run. Live calls are ended (no conversation is
        resumed); calls that are gone end as "state lost" and are not repeated."""
        settled = 0
        for attempt in await asyncio.to_thread(self.store.active_attempts):
            exists: bool | None = None
            if attempt["provider"] == self.provider.info.name:
                try:
                    exists = await self.provider.call_exists(attempt["provider_call_id"])
                except ProviderError:
                    exists = None
            if exists:
                await self._end(attempt, STATE_LOST)
            else:
                answered = attempt["state"] == CallState.ANSWERED
                forced = (CallState.COMPLETED if answered else CallState.FAILED, STATE_LOST)
                await asyncio.to_thread(
                    self.store.finish_without_event, attempt["id"], forced=forced
                )
            settled += 1
        if settled:
            log.warning("calls_recovered", extra={"count": settled})
        return settled

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
        log.info("call_hangup", extra={"attempt_id": attempt["id"], "reason": reason})
        try:
            known = await self.provider.hangup(attempt["provider_call_id"])
        except ProviderError as exc:
            log.warning(
                "call_hangup_failed", extra={"attempt_id": attempt["id"], "error": exc.code}
            )
            return
        if not known:
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
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            active = await asyncio.to_thread(self.store.active_attempts)
            if not any(a["provider"] == self.provider.info.name for a in active):
                break
            await asyncio.sleep(0.1)
        return len(attempts)

    # ------------------------------------------------------------------ dialing

    async def _place(self, ticket: DialTicket) -> None:
        request = CallRequest(
            call_id=ticket.call_id,
            to_e164=ticket.to_e164,
            ring_timeout_s=self.settings.call_ring_timeout,
            caller_id=self.settings.caller_id,
            local_endpoint=ticket.local_endpoint,
        )
        try:
            await self.provider.place_call(request)
        except ProviderUncertainError as exc:
            # The call may exist: it is watched like any other call and never repeated.
            await asyncio.to_thread(self.store.mark_dialing, ticket.attempt_id, error_code=exc.code)
            log.warning("call_place_uncertain", extra={"attempt_id": ticket.attempt_id})
            return
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
        await asyncio.to_thread(self.store.mark_dialing, ticket.attempt_id)
        log.info(
            "call_placed",
            extra={"attempt_id": ticket.attempt_id, "kind": ticket.kind, "target": ticket.target},
        )

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
            return await self.wait_for_end(ticket.attempt_id)
        except asyncio.CancelledError:
            # Ctrl+C or shutdown during a call: end it instead of leaving it running.
            with contextlib.suppress(Exception):
                await asyncio.shield(self.hangup_all(INTERRUPTED))
            raise

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
