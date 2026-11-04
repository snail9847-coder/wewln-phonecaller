"""Voice calls through the ARI simulator: bridges, externalMedia and AudioSocket over real TCP.

The simulator connects to wewln's AudioSocket server like Asterisk's chan_audiosocket (the UUID
first, then 8 kHz audio both ways); the person on the line is a simulated caller with fake
models on wewln's side. No Asterisk, no SIP, no phone network.
"""

import asyncio
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest

from voice_fakes import FAST_FRAME_S, FakeTts, fake_runtime
from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE
from wewln_phonecaller.storage import Database
from wewln_phonecaller.storage.calls import CallStore
from wewln_phonecaller.telephony import asterisk
from wewln_phonecaller.telephony.base import MediaError
from wewln_phonecaller.telephony.factory import create_provider, create_repository
from wewln_phonecaller.telephony.lifecycle import CallState
from wewln_phonecaller.telephony.service import CallService
from wewln_phonecaller.voice.audio import rms
from wewln_phonecaller.voice.simulate import SimulatedCaller

PHONE = "+994501234567"


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def voice_ari(ari_sim, make_settings):
    def build(*, heard=(), replies=(), **overrides):
        values = {
            "mode": "production",
            "production_confirm": PRODUCTION_CONFIRM_PHRASE,
            "telephony_provider": "asterisk",
            "ari_url": ari_sim.base_url,
            "ari_password": ari_sim.password,
            "call_regions": "AZ",
            "voice_mode": "ai",
            "voice_audiosocket_port": free_port(),
            **overrides,
        }
        settings = make_settings(**values)
        db = Database(settings.resolved_db_path)
        db.migrate()
        store = CallStore(create_repository(settings, db), retry_delay_s=settings.call_retry_delay)
        provider = create_provider(settings)
        provider._delays = (0.01,)
        provider.cfg = replace(provider.cfg, media_timeout=2.0)
        runtime = fake_runtime(heard=heard, replies=replies)
        service = CallService(settings, store, provider, voice=runtime, sweep_interval=0.01)
        return SimpleNamespace(
            settings=settings, store=store, provider=provider, service=service, runtime=runtime
        )

    return build


def callers(ari_sim, phrases, **kwargs):
    """The person on the line: says `phrases` (tones) and hangs up after the last one."""
    made = []

    async def person(line):
        kwargs.setdefault("pause_ms", 600)
        kwargs.setdefault("hangup_pause_ms", 4000)  # wewln's goodbye ends the call first
        caller = SimulatedCaller(
            line, list(phrases), FakeTts(660.0), frame_s=FAST_FRAME_S, **kwargs
        )
        made.append(caller)
        await caller.run()

    ari_sim.media_person = person
    return made


def requests_to(ari_sim, path):
    return [(r[0], r[2]) for r in ari_sim.requests if r[1] == path]


async def test_a_voice_call_talks_over_audiosocket(voice_ari, ari_sim):
    made = callers(ari_sim, ["Алло, слушаю.", "Да, хорошо."], hangup_after=False)
    w = voice_ari(
        heard=["Алло, слушаю.", "Да, хорошо."],
        replies=["Это проверка связи.", "Спасибо, до свидания! [END]"],
    )
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
    assert (outcome.state, outcome.voice_end, outcome.voice_turns) == (
        CallState.COMPLETED,
        "conversation_end",
        2,
    )
    call_id = w.store.get_attempt(outcome.attempt_id)["provider_call_id"]
    media_id, bridge_id = f"{call_id}-media", f"{call_id}-bridge"
    assert requests_to(ari_sim, f"/ari/bridges/{bridge_id}")[0] == (
        "POST",
        {"type": "mixing", "name": "wewln-voice"},
    )
    ((method, query),) = requests_to(ari_sim, "/ari/channels/externalMedia")
    assert method == "POST"
    assert {k: v for k, v in query.items() if k != "data"} == {
        "channelId": media_id,
        "app": "wewln",
        "external_host": f"127.0.0.1:{w.settings.voice_audiosocket_port}",
        "format": "slin",
        "encapsulation": "audiosocket",
        "transport": "tcp",
        "connection_type": "client",
        "direction": "both",
    }
    assert requests_to(ari_sim, f"/ari/bridges/{bridge_id}/addChannel") == [
        ("POST", {"channel": f"{call_id},{media_id}"})
    ]
    # No playback in voice mode; the audio channel and the bridge are removed after the call.
    assert requests_to(ari_sim, f"/ari/channels/{call_id}/play") == []
    assert ("DELETE", {}) in requests_to(ari_sim, f"/ari/channels/{media_id}")
    assert ("DELETE", {}) in requests_to(ari_sim, f"/ari/bridges/{bridge_id}")
    assert ari_sim.channels == {} and ari_sim.bridges == {} and ari_sim.media == {}
    # The person heard wewln (the greeting and the replies) over the real connection.
    assert rms(b"".join(made[0].assistant_track)) > 100
    transcript = w.store.transcript(outcome.attempt_id)
    assert [t["role"] for t in transcript] == [
        "assistant",
        "caller",
        "assistant",
        "caller",
        "assistant",
    ]
    assert transcript[-1]["text"] == "Спасибо, до свидания!"


async def test_the_person_hanging_up_ends_the_conversation(voice_ari, ari_sim):
    callers(ari_sim, ["Алло?"], pause_ms=100_000, patience_s=1.0)
    w = voice_ari(heard=["Алло?"], replies=["Здравствуйте, это проверка."])
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
    assert (outcome.state, outcome.end_reason, outcome.voice_end) == (
        CallState.COMPLETED,
        "hangup",
        "caller_hangup",
    )
    assert ari_sim.channels == {} and ari_sim.bridges == {}


async def until(predicate, limit_s=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_audio_dropped_by_asterisk_is_reconnected(voice_ari, ari_sim, caplog):
    # Asterisk 22.5 may hang up the AudioSocket channel in the middle of a call; the call stays
    # up. The person waits 1 s before speaking again, longer than the reconnect takes.
    made = callers(ari_sim, ["Алло, слушаю.", "Да, хорошо."], hangup_after=False, pause_ms=4000)
    w = voice_ari(
        heard=["Алло, слушаю.", "Да, хорошо."],
        replies=["Это проверка связи.", "Спасибо, до свидания! [END]"],
    )
    w.store.create_permit(PHONE, minutes=5)

    async def drop_after_the_first_reply():
        await until(lambda: len(made) == 1 and len(made[0].said) == 1)
        attempt_id = w.store.list_attempts(active_only=True)[0]["id"]
        await until(lambda: len(w.store.transcript(attempt_id)) >= 3)
        (media_id,) = ari_sim.media
        await ari_sim.drop_media(media_id)

    async with w.service:
        dropper = asyncio.create_task(drop_after_the_first_reply())
        outcome = await w.service.test_call(to_e164=PHONE)
        await dropper
    assert (outcome.state, outcome.end_reason, outcome.voice_end, outcome.voice_turns) == (
        CallState.COMPLETED,
        "conversation_end",
        "conversation_end",
        2,
    )
    call_id = w.store.get_attempt(outcome.attempt_id)["provider_call_id"]
    media = [q["channelId"] for _, q in requests_to(ari_sim, "/ari/channels/externalMedia")]
    assert media == [f"{call_id}-media", f"{call_id}-media-2"]
    assert requests_to(ari_sim, f"/ari/bridges/{call_id}-bridge/addChannel") == [
        ("POST", {"channel": f"{call_id},{call_id}-media"}),
        ("POST", {"channel": f"{call_id}-media-2"}),
    ]
    assert ari_sim.media_drops == 1 and made[0].said == ["Алло, слушаю.", "Да, хорошо."]
    assert "call_media_reconnected" in caplog.text
    assert ari_sim.channels == {} and ari_sim.bridges == {} and ari_sim.media == {}


async def test_audio_lost_too_often_ends_the_call(voice_ari, ari_sim, monkeypatch):
    monkeypatch.setattr(asterisk, "MAX_MEDIA_RECONNECTS", 0)
    made = callers(ari_sim, ["Алло, слушаю."], hangup_after=False, pause_ms=4000)
    w = voice_ari(heard=["Алло, слушаю."], replies=["Это проверка связи."])
    w.store.create_permit(PHONE, minutes=5)

    async def drop():
        await until(lambda: len(made) == 1 and made[0].said)
        (media_id,) = ari_sim.media
        await ari_sim.drop_media(media_id)

    async with w.service:
        dropper = asyncio.create_task(drop())
        outcome = await w.service.test_call(to_e164=PHONE)
        await dropper
    assert (outcome.state, outcome.end_reason) == (CallState.COMPLETED, "media_lost")
    assert len(requests_to(ari_sim, "/ari/channels/externalMedia")) == 1
    assert ari_sim.channels == {} and ari_sim.bridges == {} and ari_sim.media == {}


async def test_audio_that_cannot_connect_ends_the_call(voice_ari, ari_sim):
    ari_sim.media_behavior = "refuse"
    w = voice_ari()
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
    assert outcome.state == CallState.COMPLETED
    assert outcome.voice_end == "voice_error"
    assert w.service.conversations[outcome.attempt_id].error == "media_failed"
    # The bridge made for the call is removed; the call itself is ended.
    assert ari_sim.channels == {} and ari_sim.bridges == {}


async def test_the_audio_channel_must_reach_the_application(voice_ari, ari_sim):
    ari_sim.media_behavior = "no_stasis"
    callers(ari_sim, ["Алло?"], pause_ms=100_000, patience_s=1.0)
    w = voice_ari(heard=["Алло?"], replies=["Здравствуйте."])
    w.provider.cfg = replace(w.provider.cfg, media_timeout=0.3)
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
    # Bridging is still tried (Asterisk may be slow with the event); the call is talked over.
    assert outcome.state == CallState.COMPLETED
    assert ari_sim.channels == {}


async def test_check_media_connects_a_test_channel_without_a_call(voice_ari, ari_sim):
    w = voice_ari()
    w.provider.enable_media()

    async def sink(_event):
        return None

    await w.provider.start(sink)
    try:
        result = await w.provider.check_media()
        assert result["status"] == "ok"
        assert result["target"] == f"127.0.0.1:{w.settings.voice_audiosocket_port}"
        assert isinstance(result["connect_ms"], int)
        ((_method, query),) = requests_to(ari_sim, "/ari/channels/externalMedia")
        assert query["channelId"].startswith("wewln-") and query["channelId"].endswith("-media")
        # Nothing was dialed and nothing is left behind.
        assert [
            r
            for r in ari_sim.requests
            if r[1].startswith("/ari/channels/wewln-") and r[0] == "POST"
        ] == []
        await asyncio.sleep(0.05)
        assert ari_sim.channels == {}
        ari_sim.media_behavior = "refuse"
        refused = await w.provider.check_media()
        assert refused["status"] == "error" and "HTTP 500" in refused["error"]
    finally:
        await w.provider.close()


async def test_check_reports_the_audiosocket_module(voice_ari, ari_sim):
    w = voice_ari()
    w.provider.enable_media()
    result = await w.provider.check()
    assert result["status"] == "ok"
    assert result["audiosocket"]["module"] == "Running"
    ari_sim.modules.discard("chan_audiosocket.so")
    missing = await w.provider.check()
    assert missing["status"] == "error"
    assert "chan_audiosocket.so" in missing["error"]


async def test_a_busy_audiosocket_port_stops_the_start(voice_ari, ari_sim):
    w = voice_ari()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", w.settings.voice_audiosocket_port))
        taken.listen()
        w.provider.enable_media()

        async def sink(_event):
            return None

        with pytest.raises(MediaError) as error:
            await w.provider.start(sink)
    assert error.value.code == "audiosocket_unavailable"
    await w.provider.close()
