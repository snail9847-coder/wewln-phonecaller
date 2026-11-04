"""Call states, transitions, the classification of a call's end and the job decision."""

import pytest

from wewln_phonecaller.domain import JobReason, JobStatus
from wewln_phonecaller.telephony.lifecycle import (
    KILL_SWITCH,
    RING_TIMEOUT,
    STATE_LOST,
    CallState,
    can_transition,
    classify_end,
    decide_job,
    result_summary,
)

S = CallState


def test_states_only_move_forward():
    assert can_transition(S.PENDING, S.DIALING)
    assert can_transition(S.DIALING, S.ANSWERED)  # ringing may be skipped
    assert not can_transition(S.ANSWERED, S.RINGING)
    assert not can_transition(S.RINGING, S.RINGING)
    assert can_transition(S.RINGING, S.BUSY)
    for terminal in (S.COMPLETED, S.BUSY, S.FAILED, S.CANCELLED):
        assert not can_transition(terminal, S.COMPLETED)
        assert not can_transition(terminal, S.RINGING)


@pytest.mark.parametrize(
    ("state", "kwargs", "expected"),
    [
        (S.ANSWERED, {"cause": 16}, (S.COMPLETED, "hangup")),
        (S.ANSWERED, {"end_request": KILL_SWITCH}, (S.COMPLETED, KILL_SWITCH)),
        (S.RINGING, {"cause": 17}, (S.BUSY, "busy")),
        (S.RINGING, {"cause": 16, "tech_cause": 486}, (S.BUSY, "busy")),
        (S.RINGING, {"cause": 21, "tech_cause": 603}, (S.REJECTED, "rejected")),
        (S.DIALING, {"cause": 1}, (S.UNREACHABLE, "unreachable")),
        (S.DIALING, {"cause": 16, "tech_cause": 404}, (S.UNREACHABLE, "unreachable")),
        (S.RINGING, {"cause": 19}, (S.NO_ANSWER, "no_answer")),
        (S.RINGING, {"cause": 16}, (S.NO_ANSWER, "no_answer")),
        (S.DIALING, {"cause": 16, "seconds_since_dial": 1}, (S.FAILED, "ended_before_ringing")),
        (S.DIALING, {"cause": 16, "seconds_since_dial": 20}, (S.NO_ANSWER, "no_answer")),
        (S.DIALING, {"cause": 21, "tech_cause": 403}, (S.FAILED, "provider_refused")),
        (S.DIALING, {"cause": 34}, (S.FAILED, "cause_34")),
        (S.RINGING, {"end_request": RING_TIMEOUT}, (S.NO_ANSWER, RING_TIMEOUT)),
        (S.RINGING, {"end_request": STATE_LOST}, (S.FAILED, STATE_LOST)),
        (S.RINGING, {"end_request": KILL_SWITCH}, (S.CANCELLED, KILL_SWITCH)),
    ],
)
def test_the_end_of_a_call_is_classified(state, kwargs, expected):
    assert classify_end(state, **kwargs) == expected


def decide(state, reason=None, attempts=1, max_attempts=3, simulated=False):
    return decide_job(
        state, reason, attempts=attempts, max_attempts=max_attempts, simulated=simulated
    )


def test_what_happens_to_the_job():
    assert decide(S.COMPLETED).status == JobStatus.DONE
    assert decide(S.COMPLETED, simulated=True).reason == JobReason.SIMULATED
    busy = decide(S.BUSY)
    assert (busy.status, busy.reason, busy.retry) == (JobStatus.QUEUED, JobReason.RETRY_WAIT, True)
    assert decide(S.BUSY, attempts=3).reason == JobReason.ATTEMPTS_EXHAUSTED
    assert decide(S.REJECTED).reason == JobReason.CALL_REJECTED  # never retried
    assert decide(S.UNREACHABLE).reason == JobReason.NUMBER_UNREACHABLE
    assert decide(S.FAILED, STATE_LOST).reason == JobReason.STATE_LOST  # never repeated blindly
    assert decide(S.FAILED, "provider_refused").status == JobStatus.FAILED
    assert decide(S.FAILED, "cause_34").retry is True  # congestion: try again later
    stop = decide(S.CANCELLED, KILL_SWITCH)
    assert (stop.status, stop.reason) == (JobStatus.QUEUED, JobReason.EMERGENCY_STOP)
    assert decide(S.CANCELLED, "cancelled").status == JobStatus.CANCELLED
    assert decide(S.CANCELLED, "campaign_stopped").replan is True


def test_result_summary_is_russian_and_marks_simulations():
    text = result_summary(
        S.BUSY, "busy", duration_s=0, cause=17, cause_txt="User busy", simulated=True
    )
    assert text.startswith("Симуляция (mock-провайдер, реального звонка не было): Занято")
    assert "код 17 (User busy)" in text
    done = result_summary(
        S.COMPLETED, "hangup", duration_s=42.4, cause=16, cause_txt=None, simulated=False
    )
    assert done == "Завершён; разговор завершён; длительность 42 с"
