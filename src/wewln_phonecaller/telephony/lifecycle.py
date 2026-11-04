"""Call lifecycle: states, allowed transitions and how a call's end is classified.

    pending -> dialing -> ringing -> answered -> completed
       \\          \\          \\
        +----------+----------+--> busy | no_answer | rejected | unreachable | failed | cancelled

- States only move forward. A late or repeated event (ringing after answer, a second hangup) is
  ignored, so events may arrive twice or out of order without harm.
- Every call that was answered ends as `completed`, whoever hung up and why.
- The end of an unanswered call is classified by the hangup cause (Q.850, as reported by
  Asterisk) and, when present, the SIP response code (`tech_cause`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..domain import JobReason, JobStatus, Outcome


class CallState(StrEnum):
    PENDING = "pending"
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATES = (CallState.PENDING, CallState.DIALING, CallState.RINGING, CallState.ANSWERED)
TERMINAL_STATES = frozenset(set(CallState) - set(ACTIVE_STATES))
_PROGRESS = {state: index for index, state in enumerate(ACTIVE_STATES)}

STATE_LABELS: dict[CallState, str] = {
    CallState.PENDING: "Подготовка",
    CallState.DIALING: "Набор номера",
    CallState.RINGING: "Идёт вызов",
    CallState.ANSWERED: "Разговор",
    CallState.COMPLETED: "Завершён",
    CallState.BUSY: "Занято",
    CallState.NO_ANSWER: "Нет ответа",
    CallState.REJECTED: "Отклонён",
    CallState.UNREACHABLE: "Номер недоступен",
    CallState.FAILED: "Ошибка",
    CallState.CANCELLED: "Отменён",
}

OUTCOME_BY_STATE: dict[CallState, Outcome] = {
    CallState.COMPLETED: Outcome.ANSWERED,
    CallState.BUSY: Outcome.BUSY,
    CallState.NO_ANSWER: Outcome.NO_ANSWER,
    CallState.REJECTED: Outcome.REJECTED,
    CallState.UNREACHABLE: Outcome.UNREACHABLE,
    CallState.FAILED: Outcome.FAILED,
    CallState.CANCELLED: Outcome.CANCELLED,
}

# Why wewln itself ended (or asked to end) a call.
RING_TIMEOUT = "ring_timeout"
MAX_DURATION = "max_duration"
KILL_SWITCH = "kill_switch"
CAMPAIGN_STOPPED = "campaign_stopped"
DO_NOT_CALL = "dnc"
CONTACT_REMOVED = "contact_removed"
JOB_CANCELLED = "job_cancelled"
CANCELLED_BY_OPERATOR = "cancelled"
STATE_LOST = "state_lost"
HANGUP_UNCONFIRMED = "hangup_unconfirmed"
INTERRUPTED = "interrupted"
# The check right before the dial request found the real call no longer permitted.
REAL_CALLS_BLOCKED = "real_calls_blocked"

# Failures after which repeating the attempt cannot help.
NON_RETRYABLE_FAILURES = frozenset({"provider_rejected", "provider_refused", "provider_uncertain"})

_BUSY_CAUSES = {17}
_NO_ANSWER_CAUSES = {18, 19, 20}
_REJECTED_CAUSES = {21}
_UNREACHABLE_CAUSES = {1, 2, 3, 22, 27, 28}
_NORMAL_CAUSES = {0, 16, 31}
_SIP_BUSY = {486, 600}
_SIP_NO_ANSWER = {408, 480}
_SIP_DECLINE = {603}
_SIP_UNREACHABLE = {404, 410, 484, 485, 604}
# The provider refused us (credentials, balance, destination not allowed), not the person called.
_SIP_PROVIDER_REFUSED = {401, 402, 403, 407}


def can_transition(current: CallState, target: CallState) -> bool:
    if current in TERMINAL_STATES:
        return False
    if target in TERMINAL_STATES:
        return True
    return _PROGRESS[target] > _PROGRESS[current]


def classify_end(
    state: CallState,
    *,
    cause: int | None = None,
    tech_cause: int | None = None,
    end_request: str | None = None,
    seconds_since_dial: float | None = None,
) -> tuple[CallState, str]:
    """Terminal state and end reason for a call in `state` that just ended.

    `end_request` is why wewln asked to hang up (None: the other side or the network ended it).
    """
    if state == CallState.ANSWERED:
        return CallState.COMPLETED, end_request or "hangup"
    if end_request == RING_TIMEOUT:
        return CallState.NO_ANSWER, RING_TIMEOUT
    if end_request in (STATE_LOST, HANGUP_UNCONFIRMED):
        return CallState.FAILED, end_request
    if end_request:
        return CallState.CANCELLED, end_request
    if tech_cause in _SIP_PROVIDER_REFUSED:
        return CallState.FAILED, "provider_refused"
    if tech_cause in _SIP_BUSY or cause in _BUSY_CAUSES:
        return CallState.BUSY, "busy"
    if tech_cause in _SIP_DECLINE or cause in _REJECTED_CAUSES:
        return CallState.REJECTED, "rejected"
    if tech_cause in _SIP_UNREACHABLE or cause in _UNREACHABLE_CAUSES:
        return CallState.UNREACHABLE, "unreachable"
    if tech_cause in _SIP_NO_ANSWER or cause in _NO_ANSWER_CAUSES:
        return CallState.NO_ANSWER, "no_answer"
    if cause in _NORMAL_CAUSES or cause is None:
        # Ended without an answer after ringing (or after a while): nobody picked up. An
        # immediate end without any progress is a failure.
        if state == CallState.RINGING or (seconds_since_dial or 0) >= 5:
            return CallState.NO_ANSWER, "no_answer"
        return CallState.FAILED, "ended_before_ringing"
    return CallState.FAILED, f"cause_{cause}"


@dataclass(frozen=True)
class JobDecision:
    status: JobStatus
    reason: JobReason | None
    retry: bool = False
    # Re-run the planner: campaign stop, do-not-call and membership decide the final status.
    replan: bool = False


def decide_job(
    state: CallState,
    end_reason: str | None,
    *,
    attempts: int,
    max_attempts: int,
    simulated: bool,
) -> JobDecision:
    """What happens to the job after one of its attempts ended in `state`."""
    can_retry = attempts < max_attempts
    if state == CallState.COMPLETED:
        return JobDecision(JobStatus.DONE, JobReason.SIMULATED if simulated else None)
    if state == CallState.CANCELLED:
        if end_reason == CANCELLED_BY_OPERATOR:
            return JobDecision(JobStatus.CANCELLED, JobReason.CANCELLED_LOCALLY)
        if end_reason == KILL_SWITCH:
            if can_retry:
                return JobDecision(JobStatus.QUEUED, JobReason.EMERGENCY_STOP)
            return JobDecision(JobStatus.FAILED, JobReason.ATTEMPTS_EXHAUSTED)
        return JobDecision(JobStatus.QUEUED, None, replan=True)
    if state == CallState.REJECTED:
        return JobDecision(JobStatus.FAILED, JobReason.CALL_REJECTED)
    if state == CallState.UNREACHABLE:
        return JobDecision(JobStatus.FAILED, JobReason.NUMBER_UNREACHABLE)
    if state == CallState.FAILED and end_reason in (STATE_LOST, HANGUP_UNCONFIRMED):
        return JobDecision(JobStatus.FAILED, JobReason.STATE_LOST)
    if state == CallState.FAILED and end_reason in NON_RETRYABLE_FAILURES:
        return JobDecision(JobStatus.FAILED, JobReason.PROVIDER_ERROR)
    if can_retry:
        return JobDecision(JobStatus.QUEUED, JobReason.RETRY_WAIT, retry=True)
    return JobDecision(JobStatus.FAILED, JobReason.ATTEMPTS_EXHAUSTED)


_END_TEXT = {
    "hangup": "разговор завершён",
    MAX_DURATION: "wewln завершил разговор по лимиту длительности",
    RING_TIMEOUT: "никто не ответил за отведённое время",
    KILL_SWITCH: "аварийная остановка",
    CAMPAIGN_STOPPED: "кампания остановлена",
    DO_NOT_CALL: "номер попал в список «Не звонить»",
    CONTACT_REMOVED: "контакт убран из кампании",
    JOB_CANCELLED: "задание отменено",
    CANCELLED_BY_OPERATOR: "отменено вручную",
    INTERRUPTED: "прервано: программа остановлена (Ctrl+C или выключение)",
    STATE_LOST: "состояние звонка неизвестно (сбой или перезапуск): проверьте вручную",
    HANGUP_UNCONFIRMED: "провайдер не подтвердил завершение звонка: проверьте Asterisk",
    "provider_refused": "провайдер отказал (учётные данные, баланс или запрет направления)",
    "provider_rejected": "провайдер отклонил запрос: проверьте настройки",
    "provider_unavailable": "провайдер недоступен",
    "provider_uncertain": "нет ответа от провайдера: звонок мог начаться, повтор не делался",
    REAL_CALLS_BLOCKED: "реальный звонок больше не разрешён: номер не набирался",
    "ended_before_ringing": "звонок прервался до вызова",
}


def result_summary(
    state: CallState,
    end_reason: str | None,
    *,
    duration_s: float,
    cause: int | None,
    cause_txt: str | None,
    simulated: bool,
) -> str:
    """The «Итог» text of a result (Russian, no personal data)."""
    parts = [STATE_LABELS[state]]
    detail = _END_TEXT.get(end_reason or "")
    if detail:
        parts.append(detail)
    if state == CallState.COMPLETED:
        parts.append(f"длительность {round(duration_s)} с")
    if cause is not None and state not in (CallState.COMPLETED, CallState.CANCELLED):
        parts.append(f"код {cause}" + (f" ({cause_txt})" if cause_txt else ""))
    text = "; ".join(parts)
    if simulated:
        text = "Симуляция (mock-провайдер, реального звонка не было): " + text
    return text[:1900]
