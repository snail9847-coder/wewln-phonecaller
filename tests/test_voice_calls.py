"""Calls with the voice engine: the mock provider's simulated person talks with fake models."""

import asyncio
from types import SimpleNamespace

import pytest

from voice_fakes import FAST_FRAME_S, FakeLlm, FakeStt, FakeTts, LlmError, fake_runtime, fast_config
from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE
from wewln_phonecaller.domain import CampaignStatus
from wewln_phonecaller.storage import Database
from wewln_phonecaller.storage.calls import CallStore
from wewln_phonecaller.storage.repository import ContactInput
from wewln_phonecaller.telephony import ProviderInfo
from wewln_phonecaller.telephony.base import MediaError, TelephonyProvider
from wewln_phonecaller.telephony.factory import create_repository
from wewln_phonecaller.telephony.lifecycle import CallState
from wewln_phonecaller.telephony.mock import MockTelephonyProvider
from wewln_phonecaller.telephony.service import CallService
from wewln_phonecaller.voice.runtime import VoiceUnavailableError
from wewln_phonecaller.voice.simulate import SimulatedCaller

PHONE = "+994501234560"
REAL = {
    "mode": "production",
    "production_confirm": PRODUCTION_CONFIRM_PHRASE,
    "telephony_provider": "asterisk",
    "ari_password": "ari-test-password",
    "call_regions": "AZ",
}


class RealMock(MockTelephonyProvider):
    """The mock wearing a real provider's badge: exercises the real-call path, no network."""

    info = ProviderInfo("asterisk", True, "test double")


class NoAudioMock(MockTelephonyProvider):
    async def open_media(self, call_id):
        raise MediaError("Asterisk could not connect the call audio (HTTP 500)")


def person(phrases, recorder=None, **kwargs):
    """A simulated person who says `phrases` (as tones) and hangs up after the last one."""

    async def run(request, phone):
        kwargs.setdefault("frame_s", FAST_FRAME_S)
        # After the last phrase the person waits 1 s (4 s of call audio) before hanging up,
        # so that wewln's goodbye always ends the call first, even on a slow CI runner.
        kwargs.setdefault("hangup_pause_ms", 4000)
        caller = SimulatedCaller(phone, list(phrases), FakeTts(660.0), pause_ms=600, **kwargs)
        if recorder is not None:
            recorder.append(caller)
        await caller.run()

    return run


@pytest.fixture
def voice_world(make_settings):
    def build(
        *,
        phrases=(),
        heard=(),
        replies=(),
        runtime=None,
        campaign=False,
        provider_class=MockTelephonyProvider,
        caller=None,
        **overrides,
    ):
        settings = make_settings(**overrides)
        db = Database(settings.resolved_db_path)
        db.migrate()
        store = CallStore(create_repository(settings, db), retry_delay_s=settings.call_retry_delay)
        cid = None
        if campaign:
            cid = store.repo.create_local_campaign("Опрос", script="Спроси, удобно ли говорить.")
            store.repo.import_contacts(
                [ContactInput(line=2, name="Контакт", phone=PHONE)], campaign_id=cid
            )
            store.repo.set_local_campaign_status(cid, CampaignStatus.RUNNING)
        callers = []
        provider = provider_class(
            "answer", time_scale=0.05, caller=caller or person(phrases, callers)
        )
        runtime = runtime or fake_runtime(heard=heard, replies=replies)
        service = CallService(settings, store, provider, sweep_interval=0.05, voice=runtime)
        return SimpleNamespace(
            settings=settings,
            db=db,
            store=store,
            repo=store.repo,
            cid=cid,
            provider=provider,
            service=service,
            runtime=runtime,
            callers=callers,
        )

    return build


async def test_a_test_call_has_a_conversation_and_a_transcript(voice_world):
    w = voice_world(
        phrases=["Алло, слушаю.", "Да, всё хорошо слышно."],
        heard=["алло слушаю", "да всё хорошо слышно"],
        replies=["Отлично! Вы меня хорошо слышите?", "Спасибо, всего доброго! [END]"],
    )
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.state == CallState.COMPLETED
    assert outcome.end_reason == "conversation_end"
    assert outcome.voice_end == "conversation_end"
    assert outcome.voice_turns == 2
    turns = w.store.transcript(outcome.attempt_id)
    assert [(t["role"], t["kind"]) for t in turns] == [
        ("assistant", "greeting"),
        ("caller", "speech"),
        ("assistant", "reply"),
        ("caller", "speech"),
        ("assistant", "reply"),
    ]
    assert turns[1]["text"] == "алло слушаю"
    assert turns[-1]["text"] == "Спасибо, всего доброго!"
    assert turns[2]["response_ms"] is not None


def results(w):
    with w.db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM call_results ORDER BY id")]


async def until(predicate, limit_s=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_a_simulated_campaign_call_uses_the_campaign_script(voice_world):
    w = voice_world(
        campaign=True,
        phrases=["Да, удобно."],
        heard=["да удобно"],
        replies=["Спасибо, это всё. До свидания! [END]"],
    )
    async with w.service:
        report = await asyncio.wait_for(w.service.run_jobs(limit=1), 30)
    (outcome,) = report.outcomes
    assert outcome.end_reason == "conversation_end" and outcome.simulated
    system = w.runtime.llm.requests[0][0]
    assert system["role"] == "system" and "Спроси, удобно ли говорить." in system["content"]
    (result,) = results(w)
    assert result["outcome"] == "answered"
    assert "реплик собеседника: 1" in result["summary"]
    assert [j["status"] for j in w.repo.list_jobs()] == ["done"]


async def test_a_request_not_to_call_puts_a_real_number_on_the_do_not_call_list(voice_world):
    w = voice_world(
        provider_class=RealMock,
        phrases=["Больше мне не звоните."],
        heard=["больше мне не звоните"],
        replies=["Извините за беспокойство, до свидания. [OPT_OUT] [END]"],
        **REAL,
    )
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.end_reason == "opt_out" and outcome.opt_out
    assert [(e["phone_e164"], e["source"]) for e in w.repo.list_do_not_call()] == [
        (PHONE, "opt_out")
    ]


async def test_a_simulated_request_not_to_call_lists_nobody(voice_world):
    w = voice_world(
        campaign=True,
        phrases=["Не звоните мне больше."],
        heard=["не звоните мне больше"],
        replies=["Хорошо, извините. До свидания. [OPT_OUT] [END]"],
    )
    async with w.service:
        report = await asyncio.wait_for(w.service.run_jobs(limit=1), 30)
    assert report.outcomes[0].opt_out
    assert w.repo.list_do_not_call() == []
    (result,) = results(w)
    assert result["outcome"] == "opt_out"


async def test_the_person_hanging_up_ends_the_conversation(voice_world):
    w = voice_world(
        phrases=["Алло."],
        heard=["алло"],
        # The reply is long; the person hangs up after their only phrase.
        replies=["Здравствуйте, я звоню, чтобы рассказать вам о нашей проверке связи. [END]"],
    )

    async def impatient(request, phone):
        caller = SimulatedCaller(
            phone, ["Алло."], FakeTts(660.0), frame_s=FAST_FRAME_S, patience_s=1.0
        )
        caller.pause_ms = 100_000  # never waits for the assistant: hangs up after speaking
        task = asyncio.create_task(caller.run())
        await until(lambda: caller.said)
        await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    w.provider._caller = impatient
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.state == CallState.COMPLETED
    assert outcome.end_reason == "hangup"
    assert outcome.voice_end == "caller_hangup"


async def test_silence_gets_two_reprompts_then_a_goodbye(voice_world):
    async def silent(request, phone):
        while await phone.read() is not None:
            pass

    w = voice_world(caller=silent, runtime=fake_runtime(config=fast_config(no_input_s=0.3)))
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.end_reason == "no_input"
    kinds = [t["kind"] for t in w.store.transcript(outcome.attempt_id)]
    assert kinds == ["greeting", "reprompt", "reprompt", "goodbye"]
    assert outcome.voice_turns == 0


async def test_no_call_audio_ends_the_call_with_a_voice_error(voice_world):
    w = voice_world(provider_class=NoAudioMock)
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.state == CallState.COMPLETED
    assert outcome.end_reason == "voice_error"
    assert outcome.voice_end == "voice_error"
    assert w.store.transcript(outcome.attempt_id) == []


async def test_the_dialogue_model_failing_ends_the_call_politely(voice_world):
    w = voice_world(
        phrases=["Алло, кто это?"],
        heard=["алло кто это"],
        replies=[LlmError("the model stopped", code="llm_unavailable")],
    )
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.end_reason == "voice_error"
    kinds = [t["kind"] for t in w.store.transcript(outcome.attempt_id)]
    assert kinds[-1] == "failure"


async def test_the_maximum_duration_ends_a_conversation(voice_world):
    async def silent(request, phone):
        while await phone.read() is not None:
            pass

    w = voice_world(
        caller=silent,
        runtime=fake_runtime(config=fast_config(no_input_s=30)),
        call_max_duration=10,
    )
    async with w.service:
        task = asyncio.create_task(w.service.test_call(to_e164=PHONE))
        await until(lambda: w.service._conversations)
        # Pretend the call has lasted longer than allowed.
        with w.db.connect() as conn:
            conn.execute("UPDATE call_attempts SET answered_at = '2000-01-01T00:00:00+00:00'")
        outcome = await asyncio.wait_for(task, 30)
    assert outcome.end_reason == "max_duration"
    assert outcome.voice_end == "max_duration"


async def test_the_emergency_stop_ends_a_conversation(voice_world):
    async def silent(request, phone):
        while await phone.read() is not None:
            pass

    w = voice_world(caller=silent, runtime=fake_runtime(config=fast_config(no_input_s=30)))
    async with w.service:
        task = asyncio.create_task(w.service.test_call(to_e164=PHONE))
        await until(lambda: w.service._conversations)
        w.store.engage_kill_switch(source="test")
        outcome = await asyncio.wait_for(task, 30)
    assert outcome.end_reason == "kill_switch"
    assert outcome.voice_end == "kill_switch"


async def test_transcripts_can_be_switched_off(voice_world):
    w = voice_world(
        phrases=["Алло."],
        heard=["алло"],
        replies=["До свидания! [END]"],
        voice_store_transcripts=False,
    )
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert outcome.voice_turns == 1
    assert w.store.transcript(outcome.attempt_id) == []


async def test_old_transcripts_are_deleted_and_one_can_be_deleted_by_hand(voice_world):
    w = voice_world(phrases=["Алло."], heard=["алло"], replies=["До свидания! [END]"])
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    assert len(w.store.transcript(outcome.attempt_id)) == 3
    assert w.store.prune_turns(days=30) == 0
    with w.db.connect() as conn:
        conn.execute("UPDATE call_turns SET created_at = '2000-01-01T00:00:00+00:00' WHERE seq = 1")
    assert w.store.prune_turns(days=30) == 1
    assert w.store.delete_transcript(outcome.attempt_id) == 2
    assert w.store.transcript(outcome.attempt_id) == []


async def test_the_dialer_does_not_start_when_the_voice_engine_cannot(voice_world):
    runtime = fake_runtime(llm=FakeLlm(status="model qwen3:4b is not pulled"))
    w = voice_world(runtime=runtime)
    with pytest.raises(VoiceUnavailableError, match="not pulled"):
        await w.service.start()
    assert w.db.lock_holder("dialer") is None


async def test_a_provider_without_call_audio_cannot_run_the_voice_engine(voice_world):
    class NoMedia(TelephonyProvider):
        info = ProviderInfo("plain", False, "no audio")

        async def start(self, sink): ...
        async def close(self): ...
        async def place_call(self, request): ...
        async def hangup(self, call_id): ...
        async def call_exists(self, call_id): ...
        async def check(self): ...

    w = voice_world()
    service = CallService(w.settings, w.store, NoMedia(), voice=w.runtime)
    with pytest.raises(MediaError, match="cannot carry call audio"):
        await service.start()


async def test_the_engine_stt_failures_ask_to_repeat(voice_world):
    w = voice_world(
        phrases=["Алло.", "Алло, слышите?"],
        runtime=fake_runtime(
            stt=FakeStt(["алло слышите"], fail=1), replies=["Да, слышу. До свидания! [END]"]
        ),
    )
    async with w.service:
        outcome = await asyncio.wait_for(w.service.test_call(to_e164=PHONE), 30)
    kinds = [t["kind"] for t in w.store.transcript(outcome.attempt_id)]
    assert kinds == ["greeting", "sorry", "speech", "reply"]
    assert outcome.end_reason == "conversation_end"
