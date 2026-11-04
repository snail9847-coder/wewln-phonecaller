"""The call service with the mock provider: lifecycle, retries, cancels, timeouts, recovery."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE
from wewln_phonecaller.domain import CampaignStatus
from wewln_phonecaller.errors import RealCallsBlockedError
from wewln_phonecaller.storage import MIGRATIONS, Database
from wewln_phonecaller.storage.calls import CallStore, KillSwitchEngagedError, iso
from wewln_phonecaller.storage.repository import ContactInput, InvalidInputError
from wewln_phonecaller.telephony import ProviderInfo
from wewln_phonecaller.telephony.base import (
    CallRequest,
    EventKind,
    ProviderEvent,
    ProviderUnavailableError,
    ProviderUncertainError,
)
from wewln_phonecaller.telephony.factory import create_repository
from wewln_phonecaller.telephony.lifecycle import CallState
from wewln_phonecaller.telephony.mock import MockTelephonyProvider
from wewln_phonecaller.telephony.service import CallService, DialerBusyError

PHONE = "+994501234560"
PHONE_2 = "+994501234561"
REAL = {
    "mode": "production",
    "production_confirm": PRODUCTION_CONFIRM_PHRASE,
    "telephony_provider": "asterisk",
    "ari_password": "ari-test-password",
    "call_regions": "AZ",
}


class FakeClock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class RealMock(MockTelephonyProvider):
    """The mock wearing a real provider's badge: exercises the real-call gate, no network."""

    info = ProviderInfo("asterisk", True, "test double")


class FailingMock(MockTelephonyProvider):
    def __init__(self, error, **kwargs):
        super().__init__(**kwargs)
        self.error = error

    async def place_call(self, request):
        raise self.error


class UnsureMock(MockTelephonyProvider):
    """Cannot tell whether a call exists (as when Asterisk answers that question with 500)."""

    async def call_exists(self, call_id):
        return None


class SlowDialMock(MockTelephonyProvider):
    """The dial request takes a while (as when Asterisk is slow to create the channel)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent = asyncio.Event()
        self.release = asyncio.Event()

    async def place_call(self, request):
        self.sent.set()
        await self.release.wait()
        return await super().place_call(request)


UNCERTAIN = ("dialing", "provider_uncertain")


@pytest.fixture
def world(make_settings):
    def build(*phones, scenario="answer", max_attempts=1, provider=None, start=True, **overrides):
        settings = make_settings(**overrides)
        db = Database(settings.resolved_db_path)
        db.migrate()
        clock = FakeClock()
        store = CallStore(
            create_repository(settings, db), retry_delay_s=settings.call_retry_delay, clock=clock
        )
        cid = None
        if phones:
            cid = store.repo.create_local_campaign("Тест")
            rows = [
                ContactInput(line=i + 2, name=f"Контакт {i}", phone=p) for i, p in enumerate(phones)
            ]
            store.repo.import_contacts(rows, campaign_id=cid)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE campaigns SET max_attempts = ? WHERE id = ?", (max_attempts, cid)
                )
            if start:
                store.repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
        provider = provider or MockTelephonyProvider(scenario, time_scale=0)
        service = CallService(settings, store, provider, sweep_interval=0.01)
        return SimpleNamespace(
            settings=settings,
            db=db,
            store=store,
            repo=store.repo,
            clock=clock,
            cid=cid,
            provider=provider,
            service=service,
        )

    return build


def jobs(w):
    return [(j["status"], j["reason"], j["attempts"]) for j in w.repo.list_jobs()]


def rows(w, sql, params=()):
    with w.db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


async def until(predicate, limit_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not await asyncio.to_thread(predicate):
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def active(w):
    return w.store.list_attempts(active_only=True)


def dialing(w):
    """One call was placed (the provider took the request) and is still going."""
    return [a["state"] for a in active(w)] == ["dialing"]


async def test_mixed_calls_end_in_the_right_states(world):
    phones = ("+994501234560", "+994501234565", "+994501234567", "+994501234568", "+994501234569")
    w = world(*phones, scenario="mixed")
    async with w.service:
        report = await w.service.run_jobs(limit=10)
    assert [o.state for o in report.outcomes] == [
        CallState.COMPLETED,
        CallState.NO_ANSWER,
        CallState.BUSY,
        CallState.REJECTED,
        CallState.FAILED,
    ]
    assert all(o.simulated for o in report.outcomes)
    assert jobs(w) == [
        ("done", "simulated", 1),
        ("failed", "attempts_exhausted", 1),
        ("failed", "attempts_exhausted", 1),
        ("failed", "call_rejected", 1),
        ("failed", "attempts_exhausted", 1),
    ]
    results = rows(w, "SELECT outcome, simulated, summary FROM call_results ORDER BY id")
    assert [r["outcome"] for r in results] == [
        "answered",
        "no_answer",
        "busy",
        "rejected",
        "failed",
    ]
    assert all(r["simulated"] == 1 and r["summary"].startswith("Симуляция") for r in results)
    # Each attempt has its own provider call ID derived from its idempotency key.
    assert len({r.call_id for r in w.provider.placed}) == 5


async def test_busy_is_retried_after_the_delay_and_attempts_are_limited(world):
    w = world(PHONE, scenario="busy", max_attempts=2)
    async with w.service:
        first = await w.service.run_jobs()
        assert [o.state for o in first.outcomes] == [CallState.BUSY]
        (job,) = w.repo.list_jobs()
        assert (job["status"], job["reason"], job["attempts"]) == ("queued", "retry_wait", 1)
        assert job["next_attempt_at"] > iso(w.clock())
        assert (await w.service.run_jobs()).placed == 0  # the retry time is not reached yet
        w.clock.advance(w.settings.call_retry_delay + 1)
        second = await w.service.run_jobs()
        assert [o.state for o in second.outcomes] == [CallState.BUSY]
        assert (await w.service.run_jobs()).placed == 0
    assert jobs(w) == [("failed", "attempts_exhausted", 2)]
    assert [r["attempt"] for r in rows(w, "SELECT attempt FROM call_results")] == [1, 2]
    assert len({r.call_id for r in w.provider.placed}) == 2


async def test_rejected_and_unreachable_numbers_are_not_retried(world):
    w = world(
        PHONE,
        PHONE_2,
        max_attempts=3,
        provider=MockTelephonyProvider(
            lambda request: "reject" if request.to_e164 == PHONE else "unreachable", time_scale=0
        ),
    )
    async with w.service:
        report = await w.service.run_jobs()
    assert [o.state for o in report.outcomes] == [CallState.REJECTED, CallState.UNREACHABLE]
    assert jobs(w) == [("failed", "call_rejected", 1), ("failed", "number_unreachable", 1)]


async def test_repeated_events_are_applied_once(world):
    w = world(PHONE, provider=MockTelephonyProvider("answer", time_scale=0, duplicate_events=True))
    async with w.service:
        report = await w.service.run_jobs()
    assert report.outcomes[0].state == CallState.COMPLETED
    events = rows(w, "SELECT outcome FROM call_events")
    assert [e["outcome"] for e in events] == ["applied", "applied", "applied"]
    assert len(rows(w, "SELECT id FROM call_results")) == 1
    attempt = w.store.get_attempt(report.outcomes[0].attempt_id)
    late = ProviderEvent(attempt["provider_call_id"], EventKind.RINGING, "late-event-1", "test")
    assert w.store.apply_event(late) == "ignored"
    assert w.store.apply_event(late) == "duplicate"
    long_key = ProviderEvent(attempt["provider_call_id"], EventKind.RINGING, "k" * 250, "test")
    assert w.store.apply_event(long_key) == "ignored"
    assert w.store.apply_event(long_key) == "duplicate"  # keys are stored cut to 200 characters
    unknown = ProviderEvent("wewln-" + "0" * 32, EventKind.HANGUP, "unknown-event-1", "test")
    assert w.store.apply_event(unknown) == "unknown_call"


def test_states_only_move_forward(world):
    w = world()
    ticket = w.store.begin_test_attempt(provider="mock", simulated=True, to_e164=PHONE)

    def event(kind, key, **kw):
        return ProviderEvent(ticket.call_id, kind, key, "test", **kw)

    assert w.store.apply_event(event(EventKind.ANSWERED, "e-answer")) == "applied"
    assert w.store.apply_event(event(EventKind.RINGING, "e-ringing")) == "ignored"
    assert w.store.apply_event(event(EventKind.HANGUP, "e-hangup-1", cause=16)) == "applied"
    assert w.store.apply_event(event(EventKind.HANGUP, "e-hangup-2", cause=17)) == "ignored"
    attempt = w.store.get_attempt(ticket.attempt_id)
    assert (attempt["state"], attempt["hangup_cause"]) == ("completed", 16)
    assert rows(w, "SELECT id FROM call_results") == []  # test calls have no job results


def test_progress_after_an_end_the_provider_never_reported_is_flagged(world):
    w = world()
    ticket = w.store.begin_test_attempt(provider="mock", simulated=True, to_e164=PHONE)

    def event(kind, key):
        return ProviderEvent(ticket.call_id, kind, key, "test")

    assert w.store.apply_event(event(EventKind.RINGING, "r-1")) == "applied"
    # A second ringing signal changes nothing; the call is not taken for one alive after its end.
    assert w.store.apply_event(event(EventKind.RINGING, "r-2")) == "ignored"
    w.store.finish_without_event(ticket.attempt_id, forced=(CallState.FAILED, "hangup_unconfirmed"))
    assert w.store.apply_event(event(EventKind.ANSWERED, "a-1")) == "after_end"
    assert w.store.apply_event(event(EventKind.HANGUP, "h-1")) == "ignored"
    # Once the provider has reported the end, later progress is stale.
    assert w.store.apply_event(event(EventKind.ANSWERED, "a-2")) == "ignored"


async def test_silent_call_ends_at_the_ring_timeout(world):
    w = world(PHONE, scenario="silent")
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await until(lambda: dialing(w))
        w.clock.advance(w.settings.call_ring_timeout + 20)
        report = await asyncio.wait_for(task, 5)
    (outcome,) = report.outcomes
    assert (outcome.state, outcome.end_reason) == (CallState.NO_ANSWER, "ring_timeout")
    assert w.provider.hangups == [w.provider.placed[0].call_id]
    assert jobs(w) == [("failed", "attempts_exhausted", 1)]


async def test_long_call_is_ended_at_the_maximum_duration(world):
    w = world(PHONE, scenario="answer_hold")
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await until(lambda: [a["state"] for a in active(w)] == ["answered"])
        w.clock.advance(w.settings.call_max_duration + 1)
        report = await asyncio.wait_for(task, 5)
    (outcome,) = report.outcomes
    assert (outcome.state, outcome.end_reason) == (CallState.COMPLETED, "max_duration")
    (result,) = rows(w, "SELECT outcome, duration_s FROM call_results")
    assert result["outcome"] == "answered"
    assert result["duration_s"] == pytest.approx(w.settings.call_max_duration + 1, abs=1)


@pytest.mark.parametrize(
    ("action", "end_reason", "job"),
    [
        ("cancel", "cancelled", ("cancelled", "cancelled_locally", 1)),
        ("stop", "campaign_stopped", ("cancelled", "campaign_stopped", 1)),
        ("dnc", "dnc", ("blocked", "dnc", 1)),
        ("remove", "contact_removed", ("cancelled", "contact_removed", 1)),
    ],
)
async def test_calls_are_ended_when_they_must_not_go_on(world, action, end_reason, job):
    w = world(PHONE, scenario="silent", max_attempts=3)
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await until(lambda: dialing(w))
        attempt_id = active(w)[0]["id"]
        if action == "cancel":
            assert w.store.request_cancel(attempt_id) is True
        elif action == "stop":
            w.repo.stop_campaign(w.cid, reason="test")
        elif action == "dnc":
            w.repo.add_do_not_call(PHONE, reason="asked not to call")
        else:
            with w.db.connect() as conn:
                conn.execute("DELETE FROM campaign_contacts WHERE campaign_id = ?", (w.cid,))
        report = await asyncio.wait_for(task, 5)
    assert (report.outcomes[0].state, report.outcomes[0].end_reason) == (
        CallState.CANCELLED,
        end_reason,
    )
    assert jobs(w) == [job]
    assert len(w.provider.placed) == 1


async def test_paused_campaign_lets_the_call_in_progress_finish(world):
    w = world(PHONE, scenario="answer_hold")
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await until(lambda: [a["state"] for a in active(w)] == ["answered"])
        w.repo.set_local_campaign_status(w.cid, CampaignStatus.PAUSED)
        await asyncio.sleep(0.1)  # several sweeps
        assert [a["state"] for a in active(w)] == ["answered"]
        w.clock.advance(w.settings.call_max_duration + 1)
        report = await asyncio.wait_for(task, 5)
    assert report.outcomes[0].state == CallState.COMPLETED
    assert jobs(w) == [("done", "simulated", 1)]


async def test_emergency_stop_ends_calls_and_blocks_dialing(world):
    w = world(PHONE, PHONE_2, scenario="silent", max_attempts=2)
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs(limit=10))
        await until(lambda: dialing(w))
        assert w.store.engage_kill_switch(source="test", reason="drill") is True
        report = await asyncio.wait_for(task, 5)
    assert report.stopped_by == "kill_switch"
    assert (report.outcomes[0].state, report.outcomes[0].end_reason) == (
        CallState.CANCELLED,
        "kill_switch",
    )
    assert jobs(w) == [("queued", "emergency_stop", 1), ("queued", None, 0)]
    with pytest.raises(KillSwitchEngagedError):
        w.store.begin_job_attempt(provider="mock", simulated=True)
    with pytest.raises(KillSwitchEngagedError):
        w.store.begin_test_attempt(provider="mock", simulated=True, to_e164=PHONE)
    assert w.store.release_kill_switch(source="test") is True
    assert w.store.begin_job_attempt(provider="mock", simulated=True) is not None


async def test_emergency_stop_during_the_dial_request_still_ends_the_call(world):
    provider = SlowDialMock(scenario="silent", time_scale=0)
    w = world(PHONE, max_attempts=2, provider=provider)
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await asyncio.wait_for(provider.sent.wait(), 5)
        w.store.engage_kill_switch(source="test", reason="drill")
        # The hangup is asked for while the provider does not know the call yet.
        await until(lambda: [a["end_request"] for a in active(w)] == ["kill_switch"])
        provider.release.set()
        report = await asyncio.wait_for(task, 5)
        (call,) = provider.placed
        assert await provider.call_exists(call.call_id) is False
    (outcome,) = report.outcomes
    assert (outcome.state, outcome.end_reason) == (CallState.CANCELLED, "kill_switch")
    assert provider.hangups == [call.call_id]
    assert report.stopped_by == "kill_switch"


async def test_a_stop_just_before_the_dial_request_dials_nothing(world, monkeypatch):
    w = world(PHONE, max_attempts=2)
    begin = w.store.begin_job_attempt

    def begin_then_stop(**kwargs):
        ticket = begin(**kwargs)
        if ticket is not None:
            # The sweeper saw an emergency stop between the attempt and the dial request.
            w.store.engage_kill_switch(source="test", reason="drill")
            w.store.request_hangup(ticket.attempt_id, "kill_switch")
        return ticket

    monkeypatch.setattr(w.store, "begin_job_attempt", begin_then_stop)
    async with w.service:
        report = await asyncio.wait_for(w.service.run_jobs(), 5)
    (outcome,) = report.outcomes
    assert (outcome.state, outcome.end_reason) == (CallState.CANCELLED, "kill_switch")
    assert (w.provider.placed, w.provider.hangups) == ([], [])
    assert report.stopped_by == "kill_switch"


async def test_kill_switch_setting_blocks_every_call(world):
    w = world(PHONE, kill_switch=True)
    async with w.service:
        report = await w.service.run_jobs()
        with pytest.raises(KillSwitchEngagedError):
            await w.service.test_call(to_e164=PHONE)
    assert (report.placed, report.stopped_by) == (0, "kill_switch")
    assert w.provider.placed == []


async def test_restart_settles_calls_left_by_a_crash(world):
    w = world(PHONE, PHONE_2)
    first = w.store.begin_job_attempt(provider="mock", simulated=True)
    w.store.mark_dialing(first.attempt_id)
    second = w.store.begin_job_attempt(provider="mock", simulated=True)
    w.store.mark_dialing(second.attempt_id)
    w.store.apply_event(ProviderEvent(second.call_id, EventKind.ANSWERED, "answered-1", "mock"))
    async with w.service:  # a fresh provider knows neither call
        pass
    assert w.store.get_attempt(first.attempt_id)["state"] == "failed"
    assert w.store.get_attempt(second.attempt_id)["state"] == "completed"
    assert jobs(w) == [("failed", "state_lost", 1), ("done", "simulated", 1)]
    assert w.provider.placed == []  # nothing was dialed again


async def test_restart_ends_a_call_that_is_still_live(world):
    w = world(PHONE, scenario="silent")
    ticket = w.store.begin_job_attempt(provider="mock", simulated=True)

    async def ignore(event):
        return None

    await w.provider.start(ignore)  # the "crashed" process placed the call
    await w.provider.place_call(CallRequest(ticket.call_id, ticket.to_e164, 30))
    w.store.mark_dialing(ticket.attempt_id)
    async with w.service:
        await until(lambda: active(w) == [])
    attempt = w.store.get_attempt(ticket.attempt_id)
    assert (attempt["state"], attempt["end_reason"]) == ("failed", "state_lost")
    assert w.provider.hangups == [ticket.call_id]


async def test_restart_ends_a_call_the_provider_cannot_account_for(world):
    w = world(PHONE, provider=UnsureMock("silent", time_scale=0))
    ticket = w.store.begin_job_attempt(provider="mock", simulated=True)

    async def ignore(event):
        return None

    await w.provider.start(ignore)  # the "crashed" process placed the call
    await w.provider.place_call(CallRequest(ticket.call_id, ticket.to_e164, 30))
    w.store.mark_dialing(ticket.attempt_id)
    async with w.service:  # recovery is over before anything new could be dialed
        assert active(w) == []
    assert w.provider.hangups == [ticket.call_id]  # ended, not just forgotten
    attempt = w.store.get_attempt(ticket.attempt_id)
    assert (attempt["state"], attempt["end_reason"]) == ("failed", "state_lost")
    assert jobs(w) == [("failed", "state_lost", 1)]


async def test_only_one_dialer_at_a_time(world):
    w = world()
    other = CallService(w.settings, w.store, MockTelephonyProvider(time_scale=0))
    async with w.service:
        with pytest.raises(DialerBusyError):
            await other.start()
    async with other:
        pass


async def test_unreachable_provider_returns_the_job_to_the_queue(world):
    w = world(PHONE, provider=FailingMock(ProviderUnavailableError("down"), time_scale=0))
    async with w.service:
        report = await w.service.run_jobs()
    assert (report.placed, report.stopped_by) == (0, "provider_unavailable")
    assert jobs(w) == [("queued", "provider_error", 0)]
    (attempt,) = w.store.list_attempts()
    assert (attempt["state"], attempt["error_code"]) == ("failed", "provider_unavailable")
    assert rows(w, "SELECT id FROM call_results") == []


async def test_uncertain_request_is_watched_and_never_repeated(world):
    w = world(
        PHONE, max_attempts=3, provider=FailingMock(ProviderUncertainError("timeout"), time_scale=0)
    )
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await until(lambda: [(a["state"], a["error_code"]) for a in active(w)] == [UNCERTAIN])
        w.clock.advance(w.settings.call_ring_timeout + 20)
        report = await asyncio.wait_for(task, 5)
    (outcome,) = report.outcomes
    assert (outcome.state, outcome.end_reason) == (CallState.FAILED, "provider_uncertain")
    assert jobs(w) == [("failed", "provider_error", 1)]


async def test_interrupted_run_ends_the_call(world):
    w = world(PHONE, scenario="silent", max_attempts=2)
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await until(lambda: dialing(w))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    (attempt,) = w.store.list_attempts()
    assert (attempt["state"], attempt["end_reason"]) == ("cancelled", "interrupted")
    assert jobs(w) == [("queued", None, 1)]


async def test_ctrl_c_during_the_dial_request_never_dials_the_number_again(world):
    # The provider never answers the interrupted request: the call may exist, so the attempt
    # is not given back and the job is not retried.
    w = world(PHONE, max_attempts=3, provider=SlowDialMock(scenario="silent", time_scale=0))
    w.service.place_settle_s = 0.2
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await asyncio.wait_for(w.provider.sent.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    (attempt,) = w.store.list_attempts()
    assert (attempt["state"], attempt["end_reason"]) == ("failed", "provider_uncertain")
    assert jobs(w) == [("failed", "provider_error", 1)]
    async with w.service:
        report = await w.service.run_jobs()
    assert (report.placed, w.provider.placed) == (0, [])


async def test_ctrl_c_during_the_dial_request_waits_for_the_answer_and_hangs_up(world):
    w = world(PHONE, max_attempts=2, provider=SlowDialMock(scenario="silent", time_scale=0))
    async with w.service:
        task = asyncio.create_task(w.service.run_jobs())
        await asyncio.wait_for(w.provider.sent.wait(), 5)
        task.cancel()
        await asyncio.sleep(0.05)
        w.provider.release.set()  # the provider answers after all: the call exists
        with pytest.raises(asyncio.CancelledError):
            await task
    (request,) = w.provider.placed
    assert w.provider.hangups == [request.call_id]
    (attempt,) = w.store.list_attempts()
    assert (attempt["state"], attempt["end_reason"]) == ("cancelled", "interrupted")
    assert jobs(w) == [("queued", None, 1)]  # the next attempt, within «Макс. попыток»


async def test_an_unexpected_error_during_a_call_still_ends_it(world, monkeypatch):
    w = world(PHONE, scenario="silent")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(w.store, "mark_dialing", broken)
    async with w.service:
        with pytest.raises(sqlite3.OperationalError):
            await asyncio.wait_for(w.service.run_jobs(), 5)
        (request,) = w.provider.placed
        assert w.provider.hangups == [request.call_id]
        assert active(w) == []


async def test_a_call_reported_alive_after_it_ended_is_hung_up(world):
    # Its hangup was never confirmed (or the provider created it after wewln gave up).
    w = world(provider=MockTelephonyProvider("answer_hold", time_scale=0))
    ticket = w.store.begin_test_attempt(provider="mock", simulated=True, to_e164=PHONE)
    w.store.mark_dialing(ticket.attempt_id)
    w.store.finish_without_event(ticket.attempt_id, forced=(CallState.FAILED, "hangup_unconfirmed"))
    async with w.service:
        await w.provider.place_call(CallRequest(ticket.call_id, PHONE, 30))
        await until(lambda: w.provider.hangups == [ticket.call_id])
    attempt = w.store.get_attempt(ticket.attempt_id)
    assert (attempt["state"], attempt["end_reason"]) == ("failed", "hangup_unconfirmed")


async def test_everything_is_checked_again_right_before_the_dial_request(world, monkeypatch):
    # An emergency stop after the attempt was written, before the sweeper has seen it.
    w = world(PHONE, max_attempts=2)
    w.service.sweep_interval = 60
    begin = w.store.begin_job_attempt

    def begin_then_stop(**kwargs):
        ticket = begin(**kwargs)
        if ticket is not None:
            w.store.engage_kill_switch(source="test", reason="drill")
        return ticket

    monkeypatch.setattr(w.store, "begin_job_attempt", begin_then_stop)
    async with w.service:
        report = await asyncio.wait_for(w.service.run_jobs(), 5)
    (outcome,) = report.outcomes
    assert (outcome.state, outcome.end_reason) == (CallState.CANCELLED, "kill_switch")
    assert w.provider.placed == []
    assert report.stopped_by == "kill_switch"


async def test_a_permit_revoked_before_the_dial_request_stops_the_call(world, monkeypatch):
    w = world(provider=RealMock(time_scale=0), **REAL)
    w.service.sweep_interval = 60
    w.store.create_permit(PHONE, minutes=5)
    begin = w.store.begin_test_attempt

    def begin_then_revoke(**kwargs):
        ticket = begin(**kwargs)
        assert w.store.revoke_permits() == 1  # also the permit this call has just used
        return ticket

    monkeypatch.setattr(w.store, "begin_test_attempt", begin_then_revoke)
    async with w.service:
        with pytest.raises(RealCallsBlockedError) as blocked:
            await w.service.test_call(to_e164=PHONE)
    assert blocked.value.details["blocked_by"] == ["operator_not_armed"]
    assert w.provider.placed == []
    (attempt,) = w.store.list_attempts()
    assert (attempt["state"], attempt["end_reason"]) == ("failed", "real_calls_blocked")


async def test_campaign_arming_is_checked_again_at_the_dial(world):
    w = world(PHONE, max_attempts=2, **REAL)
    armed = iter([True, False])  # armed when the run starts, no longer when it would dial
    service = CallService(
        w.settings,
        w.store,
        RealMock(time_scale=0),
        campaign_armed=lambda: next(armed),
        sweep_interval=0.01,
    )
    async with service:
        with pytest.raises(RealCallsBlockedError) as blocked:
            await service.run_jobs()
    assert "operator_not_armed" in blocked.value.details["blocked_by"]
    assert service.provider.placed == []
    assert jobs(w) == [("queued", None, 0)]  # the attempt was given back


async def test_test_call_numbers_are_dialed_in_canonical_form(world):
    w = world(call_regions="AZ")
    async with w.service:
        await w.service.test_call(to_e164="+994 50 123 45 60")
    assert w.provider.placed[-1].to_e164 == PHONE


def test_a_phase_3_database_upgrades_and_its_jobs_can_be_called(make_settings):
    settings = make_settings()
    old = Database(settings.resolved_db_path, migrations=MIGRATIONS[:2])
    assert old.migrate() == 2
    now = iso(datetime.now(UTC))
    with old.connect() as conn:
        conn.execute(
            "INSERT INTO campaigns (source, name, status, max_attempts, created_at, updated_at) "
            "VALUES ('local', 'Фаза 3', 'running', 3, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO contacts (source, name, phone_raw, phone_e164, created_at, updated_at) "
            "VALUES ('local', 'Анна', ?, ?, ?, ?)",
            (PHONE, PHONE, now, now),
        )
        conn.execute("INSERT INTO campaign_contacts VALUES (1, 1, ?)", (now,))
        conn.execute(
            "INSERT INTO call_jobs (campaign_id, contact_id, idempotency_key, status, attempts, "
            "created_at, updated_at) VALUES (1, 1, 'job-1', 'queued', 1, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO call_results (job_id, attempt, idempotency_key, outcome, started_at, "
            "created_at) VALUES (1, 1, 'result-1', 'no_answer', ?, ?)",
            (now, now),
        )
    db = Database(settings.resolved_db_path)
    assert db.migrate() == MIGRATIONS[-1].version
    store = CallStore(create_repository(settings, db))
    ticket = store.begin_job_attempt(provider="mock", simulated=True)
    assert (ticket.job_id, ticket.attempt) == (1, 2)  # after the Phase 3 attempt, never over it
    assert len(rows(SimpleNamespace(db=db), "SELECT id FROM call_results")) == 1


async def test_mock_test_calls_are_simulated_and_follow_the_policy(world):
    w = world(call_regions="AZ")
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
        assert (outcome.state, outcome.simulated, outcome.kind) == (
            CallState.COMPLETED,
            True,
            "test",
        )
        w.repo.add_do_not_call(PHONE_2)
        with pytest.raises(InvalidInputError, match="do-not-call"):
            await w.service.test_call(to_e164=PHONE_2)
        with pytest.raises(InvalidInputError, match="number_type_not_allowed"):
            await w.service.test_call(to_e164="+78091234567")
        with pytest.raises(InvalidInputError, match="region_not_allowed"):
            await w.service.test_call(to_e164="+79161234567")
        softphone = await w.service.test_call(softphone=True)
    assert softphone.state == CallState.COMPLETED
    assert w.provider.placed[-1].local_endpoint == "PJSIP/softphone"
    assert rows(w, "SELECT id FROM call_results") == []


async def test_real_test_call_needs_a_permit_for_that_number(world):
    w = world(provider=RealMock(time_scale=0), **REAL)
    async with w.service:
        with pytest.raises(RealCallsBlockedError) as blocked:
            await w.service.test_call(to_e164=PHONE)
        assert blocked.value.details["blocked_by"] == ["operator_not_armed"]
        assert w.provider.placed == []
        w.store.create_permit(PHONE, minutes=5)
        outcome = await w.service.test_call(to_e164=PHONE)
        assert (outcome.state, outcome.simulated) == (CallState.COMPLETED, False)
        with pytest.raises(RealCallsBlockedError):  # one call per permit
            await w.service.test_call(to_e164=PHONE)
        w.store.create_permit(PHONE_2, minutes=1)
        w.clock.advance(61)
        with pytest.raises(RealCallsBlockedError):  # expired
            await w.service.test_call(to_e164=PHONE_2)
    assert len(w.provider.placed) == 1


async def test_softphone_test_needs_no_permit_and_no_phone_network(world):
    w = world(provider=RealMock(time_scale=0), telephony_provider="asterisk", ari_password="pw")
    async with w.service:
        outcome = await w.service.test_call(softphone=True)
    assert outcome.state == CallState.COMPLETED
    (request,) = w.provider.placed
    assert request.local_endpoint == "PJSIP/softphone"


async def test_campaign_calls_over_a_real_provider_stay_blocked(world):
    w = world(PHONE, provider=RealMock(time_scale=0), **REAL)
    async with w.service:
        with pytest.raises(RealCallsBlockedError) as blocked:
            await w.service.run_jobs()
    assert "operator_not_armed" in blocked.value.details["blocked_by"]
    assert w.provider.placed == []
    # Phase 6 arms campaigns; the dialing path itself already works:
    armed = CallService(
        w.settings,
        w.store,
        RealMock(time_scale=0),
        campaign_armed=lambda: True,
        sweep_interval=0.01,
    )
    async with armed:
        report = await armed.run_jobs()
    assert (report.outcomes[0].state, report.outcomes[0].simulated) == (CallState.COMPLETED, False)
    assert jobs(w) == [("done", None, 1)]


async def test_mock_calls_reach_notion_with_their_outcome(notion_world):
    w = notion_world
    camp = w.campaign(**{"Макс. попыток": 2})
    w.contact("Анна", "+994501234567", [camp])
    await w.run()
    store = CallStore(w.repo, retry_delay_s=900)
    service = CallService(
        w.settings, store, MockTelephonyProvider("busy", time_scale=0), sweep_interval=0.01
    )
    async with service:
        report = await service.run_jobs()
    assert [o.state for o in report.outcomes] == [CallState.BUSY]
    await w.run()
    (job,) = w.rows("jobs")
    assert (job["Статус"], job["Попытки"]) == ("В очереди", 1)
    assert job["Причина"] == "Ждёт повторного звонка"
    (result,) = w.rows("results")
    assert (result["Исход"], result["Симуляция"]) == ("Занято", True)
    assert result["Итог"].startswith("Симуляция")
