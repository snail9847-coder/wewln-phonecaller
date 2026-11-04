"""Call attempts, provider events, real-call permits and the emergency stop, in SQLite.

The call service (telephony/service.py) drives calls; everything it learns is stored here first:
- an attempt row exists before the provider is asked to dial, so after a crash every call that
  may exist is known, and the same attempt is never dialed twice (its provider call ID is
  derived from the attempt's idempotency key);
- every provider event is stored once by its key: a repeated or late event changes nothing;
- the job's status follows from how the attempt ended (lifecycle.decide_job), in the same
  transaction; at most one live attempt per job is enforced by a unique index.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..domain import FINAL_CAMPAIGN_STATUSES, JobReason, JobStatus
from ..errors import RealCallsBlockedError, WewlnError
from ..redact import mask_phone_numbers
from ..telephony.base import EventKind, ProviderEvent, provider_call_id
from ..telephony.lifecycle import (
    CAMPAIGN_STOPPED,
    CANCELLED_BY_OPERATOR,
    CONTACT_REMOVED,
    DO_NOT_CALL,
    JOB_CANCELLED,
    OUTCOME_BY_STATE,
    TERMINAL_STATES,
    CallState,
    can_transition,
    classify_end,
    decide_job,
    result_summary,
)
from .repository import (
    MAX_ATTEMPTS_LIMIT,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    Repository,
    result_key,
)

KILL_SWITCH_FLAG = "kill_switch"
EVENT_RETENTION_DAYS = 30
MAX_PERMIT_MINUTES = 60
MAX_PERMIT_CALLS = 3
_STATE_BY_EVENT = {
    EventKind.DIALING: CallState.DIALING,
    EventKind.RINGING: CallState.RINGING,
    EventKind.ANSWERED: CallState.ANSWERED,
}
# One fixed statement per state: the timestamp column is never formatted into SQL.
_PROGRESS_SQL = {
    CallState.DIALING: "UPDATE call_attempts SET state = 'dialing', "
    "dial_started_at = COALESCE(dial_started_at, ?), updated_at = ? WHERE id = ?",
    CallState.RINGING: "UPDATE call_attempts SET state = 'ringing', ringing_at = COALESCE("
    "ringing_at, ?), updated_at = ? WHERE id = ?",
    CallState.ANSWERED: "UPDATE call_attempts SET state = 'answered', answered_at = COALESCE("
    "answered_at, ?), updated_at = ? WHERE id = ?",
}


class KillSwitchEngagedError(WewlnError):
    code = "kill_switch_engaged"
    http_status = 409


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def attempt_key(job_idempotency_key: str, attempt: int) -> str:
    return job_idempotency_key.replace("wewln:job:", "wewln:call:", 1) + f":{attempt}"


def _clamp_attempts(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 1
    return max(1, min(MAX_ATTEMPTS_LIMIT, number))


@dataclass(frozen=True)
class DialTicket:
    """Everything needed to dial one attempt. The number is kept out of repr (and logs)."""

    attempt_id: int
    kind: str
    call_id: str
    to_e164: str = field(repr=False)
    simulated: bool
    target: str = "phone"
    job_id: int | None = None
    attempt: int = 1
    local_endpoint: str | None = None


class CallStore:
    def __init__(
        self,
        repo: Repository,
        *,
        retry_delay_s: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repo = repo
        self.db = repo.db
        self.retry_delay_s = retry_delay_s
        self._clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self._clock()

    # ------------------------------------------------------------------ emergency stop

    @staticmethod
    def _kill_switch(conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM runtime_flags WHERE name = ? AND value = 'on'", (KILL_SWITCH_FLAG,)
        ).fetchone()

    def kill_switch(self) -> dict[str, Any] | None:
        """The runtime emergency stop, if engaged: who set it, when and why."""
        with self.db.connect(must_exist=True) as conn:
            row = self._kill_switch(conn)
        return dict(row) if row else None

    def engage_kill_switch(self, *, source: str, reason: str = "") -> bool:
        now = iso(self.now())
        with self.db.transaction() as conn:
            if self._kill_switch(conn):
                return False
            conn.execute(
                "INSERT INTO runtime_flags (name, value, source, reason, updated_at) "
                "VALUES (?, 'on', ?, ?, ?) ON CONFLICT (name) DO UPDATE SET value = 'on', "
                "source = excluded.source, reason = excluded.reason, "
                "updated_at = excluded.updated_at",
                (KILL_SWITCH_FLAG, source[:50], reason.strip()[:200], now),
            )
            self.db.audit(
                "calls.kill_switch.engage",
                actor=source[:50],
                details={"reason": reason[:200]},
                conn=conn,
            )
        return True

    def release_kill_switch(self, *, source: str) -> bool:
        now = iso(self.now())
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE runtime_flags SET value = 'off', source = ?, reason = '', updated_at = ? "
                "WHERE name = ? AND value = 'on'",
                (source[:50], now, KILL_SWITCH_FLAG),
            ).rowcount
            if changed:
                self.db.audit("calls.kill_switch.release", actor=source[:50], conn=conn)
        return bool(changed)

    # ------------------------------------------------------------------ starting attempts

    def begin_job_attempt(
        self, *, provider: str, simulated: bool, campaign_id: int | None = None
    ) -> DialTicket | None:
        """Pick the next callable job and create its attempt, all in one transaction.

        Every condition is checked again here (campaign running, contact still in it, number
        valid, allowed and not on the do-not-call list, Notion reconciled, retry time reached,
        attempts left). Returns None when nothing may be dialed now.
        """
        now = self.now()
        now_s = iso(now)
        with self.db.transaction() as conn:
            if self._kill_switch(conn):
                raise KillSwitchEngagedError("the emergency stop is engaged (wewln calls resume)")
            self.repo._plan(conn, campaign_ids=[campaign_id] if campaign_id is not None else None)
            suppressed = {r[0] for r in conn.execute("SELECT DISTINCT phone_e164 FROM suppression")}
            sql = (
                "SELECT * FROM call_jobs WHERE status = 'queued' AND "
                "(next_attempt_at IS NULL OR next_attempt_at <= ?)"
            )
            params: list[Any] = [now_s]
            if campaign_id is not None:
                sql += " AND campaign_id = ?"
                params.append(campaign_id)
            sql += " ORDER BY COALESCE(next_attempt_at, created_at), id"
            for job in conn.execute(sql, params).fetchall():
                if not self.repo.callable_now(conn, job, suppressed, now=now_s):
                    continue
                campaign = conn.execute(
                    "SELECT max_attempts FROM campaigns WHERE id = ?", (job["campaign_id"],)
                ).fetchone()
                if job["attempts"] >= _clamp_attempts(campaign["max_attempts"]):
                    conn.execute(
                        "UPDATE call_jobs SET status = 'failed', reason = ?, "
                        "next_attempt_at = NULL, notion_dirty = 1, updated_at = ? WHERE id = ?",
                        (JobReason.ATTEMPTS_EXHAUSTED.value, now_s, job["id"]),
                    )
                    continue
                contact = conn.execute(
                    "SELECT phone_e164 FROM contacts WHERE id = ?", (job["contact_id"],)
                ).fetchone()
                previous = conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) FROM call_attempts WHERE job_id = ?",
                    (job["id"],),
                ).fetchone()[0]
                number = max(int(job["attempts"]), int(previous)) + 1
                key = attempt_key(job["idempotency_key"], number)
                call_id = provider_call_id(key)
                attempt_id = self._insert_attempt(
                    conn,
                    kind="job",
                    job_id=job["id"],
                    attempt=number,
                    key=key,
                    provider=provider,
                    call_id=call_id,
                    simulated=simulated,
                    target="phone",
                    phone=contact["phone_e164"],
                    now=now_s,
                )
                # The attempt counts from here on: if the request never reaches the provider,
                # abandon_attempt() gives it back.
                conn.execute(
                    "UPDATE call_jobs SET status = 'in_progress', reason = NULL, "
                    "attempts = attempts + 1, next_attempt_at = NULL, notion_dirty = 1, "
                    "updated_at = ? WHERE id = ?",
                    (now_s, job["id"]),
                )
                self.db.audit(
                    "call.attempt.start",
                    subject=f"job:{job['id']}",
                    details={
                        "attempt": number,
                        "attempt_id": attempt_id,
                        "provider": provider,
                        "simulated": simulated,
                    },
                    conn=conn,
                )
                return DialTicket(
                    attempt_id=attempt_id,
                    kind="job",
                    call_id=call_id,
                    to_e164=contact["phone_e164"],
                    simulated=simulated,
                    job_id=job["id"],
                    attempt=number,
                )
        return None

    def begin_test_attempt(
        self,
        *,
        provider: str,
        simulated: bool,
        to_e164: str | None = None,
        local_endpoint: str | None = None,
        require_permit: bool = False,
        actor: str = "cli",
    ) -> DialTicket:
        """A single test call to one number (or to a local softphone, no phone network).

        A real test call to a number consumes one use of a valid permit (`create_permit`) in
        this transaction; without one it is refused. Do-not-call always wins.
        """
        if (to_e164 is None) == (local_endpoint is None):
            raise InvalidInputError("give either a phone number or a softphone endpoint")
        now_s = iso(self.now())
        with self.db.transaction() as conn:
            if self._kill_switch(conn):
                raise KillSwitchEngagedError("the emergency stop is engaged (wewln calls resume)")
            permit_id = None
            if to_e164 is not None:
                if self._number_suppressed(conn, to_e164):
                    raise InvalidInputError("this number is on the do-not-call list")
                if require_permit:
                    permit = conn.execute(
                        "SELECT id FROM real_call_permits WHERE phone_e164 = ? AND "
                        "revoked_at IS NULL AND used < max_calls AND expires_at > ? "
                        "ORDER BY id LIMIT 1",
                        (to_e164, now_s),
                    ).fetchone()
                    if permit is None:
                        raise RealCallsBlockedError(
                            "no valid real-call permit for this number "
                            "(wewln calls permit --to <number>)",
                            details={"blocked_by": ["operator_not_armed"]},
                        )
                    permit_id = permit["id"]
                    conn.execute(
                        "UPDATE real_call_permits SET used = used + 1 WHERE id = ?", (permit_id,)
                    )
            key = f"wewln:test:{uuid.uuid4().hex}"
            call_id = provider_call_id(key)
            target = "softphone" if local_endpoint else "phone"
            attempt_id = self._insert_attempt(
                conn,
                kind="test",
                job_id=None,
                attempt=1,
                key=key,
                provider=provider,
                call_id=call_id,
                simulated=simulated,
                target=target,
                phone=to_e164 or "",
                now=now_s,
            )
            self.db.audit(
                "call.test.start",
                actor=actor,
                subject=to_e164 or local_endpoint,
                details={
                    "attempt_id": attempt_id,
                    "provider": provider,
                    "simulated": simulated,
                    "target": target,
                    "permit_id": permit_id,
                },
                conn=conn,
            )
        return DialTicket(
            attempt_id=attempt_id,
            kind="test",
            call_id=call_id,
            to_e164=to_e164 or "",
            simulated=simulated,
            target=target,
            local_endpoint=local_endpoint,
        )

    @staticmethod
    def _number_suppressed(conn: sqlite3.Connection, e164: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM suppression WHERE phone_e164 = ? UNION ALL "
                "SELECT 1 FROM contacts WHERE phone_e164 = ? AND notion_dnc = 1 LIMIT 1",
                (e164, e164),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _insert_attempt(
        conn: sqlite3.Connection,
        *,
        kind: str,
        job_id: int | None,
        attempt: int,
        key: str,
        provider: str,
        call_id: str,
        simulated: bool,
        target: str,
        phone: str,
        now: str,
    ) -> int:
        try:
            cursor = conn.execute(
                "INSERT INTO call_attempts (kind, job_id, attempt, idempotency_key, provider, "
                "provider_call_id, simulated, target, phone_masked, state, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    kind,
                    job_id,
                    attempt,
                    key,
                    provider,
                    call_id,
                    int(simulated),
                    target,
                    mask_phone_numbers(phone) if phone else "",
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("this call attempt exists already") from exc
        return int(cursor.lastrowid or 0)

    def mark_dialing(self, attempt_id: int, *, error_code: str | None = None) -> None:
        """The provider took the request (or may have: `error_code` provider_uncertain)."""
        now_s = iso(self.now())
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE call_attempts SET state = CASE WHEN state = 'pending' THEN 'dialing' "
                "ELSE state END, dial_started_at = COALESCE(dial_started_at, ?), "
                "error_code = COALESCE(?, error_code), updated_at = ? WHERE id = ?",
                (now_s, error_code, now_s, attempt_id),
            )

    def abandon_attempt(self, attempt_id: int, *, error_code: str, job_reason: JobReason | None):
        """The request certainly never created a call: the attempt does not count."""
        now_s = iso(self.now())
        with self.db.transaction() as conn:
            attempt = self._attempt(conn, attempt_id)
            if CallState(attempt["state"]) in TERMINAL_STATES:
                return
            conn.execute(
                "UPDATE call_attempts SET state = 'failed', end_reason = ?, error_code = ?, "
                "ended_at = ?, updated_at = ? WHERE id = ?",
                (error_code, error_code, now_s, now_s, attempt_id),
            )
            if attempt["kind"] == "job":
                conn.execute(
                    "UPDATE call_jobs SET attempts = MAX(attempts - 1, 0), "
                    "reason = CASE WHEN status = 'in_progress' THEN ? ELSE reason END, "
                    "status = CASE WHEN status = 'in_progress' THEN 'queued' ELSE status END, "
                    "notion_dirty = 1, updated_at = ? WHERE id = ?",
                    (job_reason.value if job_reason else None, now_s, attempt["job_id"]),
                )
                campaign_id = conn.execute(
                    "SELECT campaign_id FROM call_jobs WHERE id = ?", (attempt["job_id"],)
                ).fetchone()[0]
                self.repo._plan(conn, campaign_ids=[campaign_id])
            self.db.audit(
                "call.attempt.not_sent",
                subject=f"attempt:{attempt_id}",
                details={"error": error_code},
                conn=conn,
            )

    # ------------------------------------------------------------------ events and endings

    def apply_event(self, event: ProviderEvent) -> str:
        """Store and apply one provider event. Returns applied, ignored, duplicate or
        unknown_call. A repeated event (same key) is never applied twice."""
        now = self.now()
        now_s = iso(now)
        with self.db.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM call_events WHERE event_key = ?", (event.event_key,)
            ).fetchone():
                return "duplicate"
            attempt = conn.execute(
                "SELECT * FROM call_attempts WHERE provider_call_id = ?", (event.call_id,)
            ).fetchone()
            if attempt is None:
                outcome = "unknown_call"
            elif CallState(attempt["state"]) in TERMINAL_STATES:
                outcome = "ignored"
            elif event.kind == EventKind.HANGUP:
                self._finish(
                    conn,
                    attempt,
                    now,
                    cause=event.cause,
                    cause_txt=event.cause_txt,
                    tech_cause=event.tech_cause,
                )
                outcome = "applied"
            else:
                target = _STATE_BY_EVENT[event.kind]
                if can_transition(CallState(attempt["state"]), target):
                    conn.execute(_PROGRESS_SQL[target], (now_s, now_s, attempt["id"]))
                    conn.execute(
                        "UPDATE call_attempts SET dial_started_at = COALESCE(dial_started_at, ?) "
                        "WHERE id = ?",
                        (now_s, attempt["id"]),
                    )
                    outcome = "applied"
                else:
                    outcome = "ignored"
            conn.execute(
                "INSERT INTO call_events (event_key, source, provider_call_id, attempt_id, kind, "
                "outcome, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_key[:200],
                    event.source[:30],
                    event.call_id[:100],
                    attempt["id"] if attempt is not None else None,
                    event.kind.value,
                    outcome,
                    now_s,
                ),
            )
        return outcome

    def finish_without_event(
        self, attempt_id: int, *, forced: tuple[CallState, str] | None = None
    ) -> CallState:
        """The provider no longer knows the call, but its hangup event never came: end it here,
        classified by why wewln asked to end it (or `forced`)."""
        now = self.now()
        with self.db.transaction() as conn:
            attempt = self._attempt(conn, attempt_id)
            state = CallState(attempt["state"])
            if state in TERMINAL_STATES:
                return state
            return self._finish(conn, attempt, now, forced=forced)

    def _finish(
        self,
        conn: sqlite3.Connection,
        attempt: sqlite3.Row,
        now: datetime,
        *,
        cause: int | None = None,
        cause_txt: str | None = None,
        tech_cause: int | None = None,
        forced: tuple[CallState, str] | None = None,
    ) -> CallState:
        state = CallState(attempt["state"])
        started = parse_iso(attempt["dial_started_at"] or attempt["created_at"])
        if forced is not None:
            final, end_reason = forced
        else:
            final, end_reason = classify_end(
                state,
                cause=cause,
                tech_cause=tech_cause,
                end_request=attempt["end_request"],
                seconds_since_dial=(now - started).total_seconds(),
            )
        duration = 0.0
        if final == CallState.COMPLETED and attempt["answered_at"]:
            duration = max(0.0, (now - parse_iso(attempt["answered_at"])).total_seconds())
        now_s = iso(now)
        conn.execute(
            "UPDATE call_attempts SET state = ?, end_reason = ?, hangup_cause = ?, "
            "hangup_cause_txt = ?, tech_cause = ?, ended_at = ?, updated_at = ? WHERE id = ?",
            (
                final.value,
                end_reason,
                cause,
                (cause_txt or "")[:100] or None,
                tech_cause,
                now_s,
                now_s,
                attempt["id"],
            ),
        )
        simulated = bool(attempt["simulated"])
        details = {
            "attempt_id": attempt["id"],
            "state": final.value,
            "end_reason": end_reason,
            "cause": cause,
            "tech_cause": tech_cause,
            "duration_s": round(duration, 1),
        }
        if attempt["kind"] != "job":
            self.db.audit(
                "call.test.end", subject=f"attempt:{attempt['id']}", details=details, conn=conn
            )
            return final
        job = conn.execute("SELECT * FROM call_jobs WHERE id = ?", (attempt["job_id"],)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO call_results (job_id, attempt, idempotency_key, outcome, "
            "simulated, started_at, duration_s, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job["id"],
                attempt["attempt"],
                result_key(job["idempotency_key"], attempt["attempt"]),
                OUTCOME_BY_STATE[final].value,
                int(simulated),
                iso(started),
                round(duration, 1),
                result_summary(
                    final,
                    end_reason,
                    duration_s=duration,
                    cause=cause,
                    cause_txt=cause_txt,
                    simulated=simulated,
                ),
                now_s,
            ),
        )
        if job["status"] == JobStatus.IN_PROGRESS:
            campaign = conn.execute(
                "SELECT max_attempts FROM campaigns WHERE id = ?", (job["campaign_id"],)
            ).fetchone()
            decision = decide_job(
                final,
                end_reason,
                attempts=int(job["attempts"]),
                max_attempts=_clamp_attempts(campaign["max_attempts"]),
                simulated=simulated,
            )
            next_at = iso(now + timedelta(seconds=self.retry_delay_s)) if decision.retry else None
            conn.execute(
                "UPDATE call_jobs SET status = ?, reason = ?, next_attempt_at = ?, "
                "notion_dirty = 1, updated_at = ? WHERE id = ?",
                (
                    decision.status.value,
                    decision.reason.value if decision.reason else None,
                    next_at,
                    now_s,
                    job["id"],
                ),
            )
            details["job_status"] = decision.status.value
            self.repo._plan(conn, campaign_ids=[job["campaign_id"]])
        self.db.audit("call.attempt.end", subject=f"job:{job['id']}", details=details, conn=conn)
        return final

    # ------------------------------------------------------------------ hangups and cancels

    def request_hangup(self, attempt_id: int, reason: str) -> bool:
        """Remember why wewln ends the call before asking the provider (first reason wins)."""
        now_s = iso(self.now())
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE call_attempts SET end_request = COALESCE(end_request, ?), "
                "hangup_requested_at = ?, hangup_tries = hangup_tries + 1, updated_at = ? "
                "WHERE id = ? AND state IN ('pending', 'dialing', 'ringing', 'answered')",
                (reason, now_s, now_s, attempt_id),
            ).rowcount
        return bool(changed)

    def request_cancel(self, attempt_id: int, *, actor: str = "cli") -> bool:
        """Ask the running dialer to end this call (it hangs up within a second or two)."""
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE call_attempts SET cancel_requested = COALESCE(cancel_requested, ?), "
                "updated_at = ? WHERE id = ? AND "
                "state IN ('pending', 'dialing', 'ringing', 'answered')",
                (CANCELLED_BY_OPERATOR, iso(self.now()), attempt_id),
            ).rowcount
            if changed:
                self.db.audit(
                    "call.cancel", actor=actor, subject=f"attempt:{attempt_id}", conn=conn
                )
        return bool(changed)

    def active_attempts(self) -> list[dict[str, Any]]:
        """Live attempts with `interrupt`: why the call must end now, if anything changed
        (job cancelled, campaign stopped, contact removed or put on the do-not-call list).
        A paused campaign lets a call in progress finish."""
        with self.db.connect(must_exist=True) as conn:
            rows = conn.execute(
                "SELECT * FROM call_attempts WHERE "
                "state IN ('pending', 'dialing', 'ringing', 'answered') ORDER BY id"
            ).fetchall()
            suppressed = {r[0] for r in conn.execute("SELECT DISTINCT phone_e164 FROM suppression")}
            result = []
            for row in rows:
                item = dict(row)
                item["interrupt"] = (
                    self._interrupt(conn, row, suppressed) if row["kind"] == "job" else None
                )
                result.append(item)
        return result

    @staticmethod
    def _interrupt(
        conn: sqlite3.Connection, attempt: sqlite3.Row, suppressed: set[str]
    ) -> str | None:
        job = conn.execute("SELECT * FROM call_jobs WHERE id = ?", (attempt["job_id"],)).fetchone()
        if job is None or job["status"] != JobStatus.IN_PROGRESS:
            return JOB_CANCELLED
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (job["campaign_id"],)
        ).fetchone()
        if (
            campaign["status"] in FINAL_CAMPAIGN_STATUSES
            or campaign["stopped_at"]
            or campaign["missing_in_notion"]
        ):
            return CAMPAIGN_STOPPED
        contact = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (job["contact_id"],)
        ).fetchone()
        member = conn.execute(
            "SELECT 1 FROM campaign_contacts WHERE campaign_id = ? AND contact_id = ?",
            (job["campaign_id"], job["contact_id"]),
        ).fetchone()
        if member is None or contact is None or contact["missing_in_notion"]:
            return CONTACT_REMOVED
        if contact["notion_dnc"] or (contact["phone_e164"] in suppressed):
            return DO_NOT_CALL
        return None

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _attempt(conn: sqlite3.Connection, attempt_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM call_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"call {attempt_id} not found")
        return row

    def get_attempt(self, attempt_id: int) -> dict[str, Any]:
        with self.db.connect(must_exist=True) as conn:
            return dict(self._attempt(conn, attempt_id))

    def list_attempts(self, *, limit: int = 50, active_only: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT a.id, a.kind, a.job_id, a.attempt, a.provider, a.simulated, a.target, "
            "a.phone_masked, a.state, a.end_reason, a.end_request, a.hangup_cause, "
            "a.tech_cause, a.error_code, a.created_at, a.answered_at, a.ended_at, "
            "c.name AS campaign_name FROM call_attempts a "
            "LEFT JOIN call_jobs j ON j.id = a.job_id LEFT JOIN campaigns c ON c.id = j.campaign_id"
        )
        if active_only:
            sql += " WHERE a.state IN ('pending', 'dialing', 'ringing', 'answered')"
        sql += " ORDER BY a.id DESC LIMIT ?"
        with self.db.connect(must_exist=True) as conn:
            return [dict(r) for r in conn.execute(sql, (max(1, min(limit, 500)),))]

    def prune_events(self, *, days: int = EVENT_RETENTION_DAYS) -> int:
        cutoff = iso(self.now() - timedelta(days=days))
        with self.db.transaction() as conn:
            return conn.execute("DELETE FROM call_events WHERE received_at < ?", (cutoff,)).rowcount

    # ------------------------------------------------------------------ real-call permits

    def create_permit(
        self, phone_e164: str, *, minutes: int, max_calls: int = 1, created_by: str = "cli"
    ) -> dict[str, Any]:
        if not 1 <= minutes <= MAX_PERMIT_MINUTES:
            raise InvalidInputError(f"a permit lasts 1-{MAX_PERMIT_MINUTES} minutes")
        if not 1 <= max_calls <= MAX_PERMIT_CALLS:
            raise InvalidInputError(f"a permit allows 1-{MAX_PERMIT_CALLS} calls")
        now = self.now()
        expires = iso(now + timedelta(minutes=minutes))
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO real_call_permits (phone_e164, max_calls, used, expires_at, "
                "created_by, created_at) VALUES (?, ?, 0, ?, ?, ?)",
                (phone_e164, max_calls, expires, created_by[:50], iso(now)),
            )
            permit_id = int(cursor.lastrowid or 0)
            self.db.audit(
                "calls.permit.create",
                actor=created_by[:50],
                subject=phone_e164,
                details={"permit_id": permit_id, "minutes": minutes, "max_calls": max_calls},
                conn=conn,
            )
        return {"id": permit_id, "expires_at": expires, "max_calls": max_calls}

    def has_valid_permit(self, phone_e164: str) -> bool:
        with self.db.connect(must_exist=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM real_call_permits WHERE phone_e164 = ? AND revoked_at IS NULL "
                "AND used < max_calls AND expires_at > ? LIMIT 1",
                (phone_e164, iso(self.now())),
            ).fetchone()
        return row is not None

    def revoke_permits(self, *, actor: str = "cli") -> int:
        now_s = iso(self.now())
        with self.db.transaction() as conn:
            revoked = conn.execute(
                "UPDATE real_call_permits SET revoked_at = ? WHERE revoked_at IS NULL "
                "AND used < max_calls AND expires_at > ?",
                (now_s, now_s),
            ).rowcount
            if revoked:
                self.db.audit(
                    "calls.permit.revoke", actor=actor, details={"revoked": revoked}, conn=conn
                )
        return revoked

    def list_permits(self) -> list[dict[str, Any]]:
        with self.db.connect(must_exist=True) as conn:
            rows = conn.execute(
                "SELECT id, phone_e164, max_calls, used, expires_at, created_by, created_at, "
                "revoked_at FROM real_call_permits ORDER BY id DESC LIMIT 50"
            ).fetchall()
        now_s = iso(self.now())
        result = []
        for row in rows:
            item = dict(row)
            item["valid"] = (
                row["revoked_at"] is None
                and row["used"] < row["max_calls"]
                and row["expires_at"] > now_s
            )
            result.append(item)
        return result
