"""Against a real Asterisk (the CI job `asterisk-live`): ARI requests, events and hangups.

Calls go to Local channels of a test dialplan (deploy/asterisk/ci/extensions-ci.conf), so no
SIP and no phone network are involved. Run locally with a configured Asterisk that has this
dialplan (docs/TESTING.md):

    ASTERISK_LIVE_ARI_URL=http://127.0.0.1:8088 ASTERISK_LIVE_ARI_USER=wewln \
    ASTERISK_LIVE_ARI_PASSWORD=... pytest -m asterisk_live
"""

import asyncio
import os
import socket
from types import SimpleNamespace

import pytest

from voice_fakes import fake_runtime, fast_config
from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE
from wewln_phonecaller.storage import Database
from wewln_phonecaller.storage.calls import CallStore
from wewln_phonecaller.telephony.base import CallRequest
from wewln_phonecaller.telephony.factory import create_provider, create_repository
from wewln_phonecaller.telephony.lifecycle import CallState
from wewln_phonecaller.telephony.service import CallService

URL = os.environ.get("ASTERISK_LIVE_ARI_URL", "")
AUDIOSOCKET_HOST = os.environ.get("ASTERISK_LIVE_AUDIOSOCKET_HOST", "127.0.0.1")
pytestmark = [
    pytest.mark.asterisk_live,
    pytest.mark.skipif(not URL, reason="no live Asterisk (set ASTERISK_LIVE_ARI_URL)"),
]

# Extensions of the [wewln-ci] context (digits of these AZ numbers).
ANSWER = "+994124980100"
BUSY = "+994124980117"
RINGING = "+994124980119"
REJECT = "+994124980121"
CONGESTION = "+994124980134"
# Voice calls (call audio over AudioSocket): a person who says "hello world" after 3 s, and
# an echo of wewln's own voice.
SPEAKS = "+994124980150"
ECHO = "+994124980151"


def live_settings(make_settings, **overrides):
    return make_settings(
        mode="production",
        production_confirm=PRODUCTION_CONFIRM_PHRASE,
        telephony_provider="asterisk",
        ari_url=URL,
        ari_username=os.environ.get("ASTERISK_LIVE_ARI_USER", "wewln"),
        ari_password=os.environ.get("ASTERISK_LIVE_ARI_PASSWORD", ""),
        asterisk_endpoint="Local/{number}@wewln-ci",
        asterisk_number_format="digits",
        call_regions="AZ",
        **overrides,
    )


@pytest.fixture
def live(make_settings):
    def build(**overrides):
        settings = live_settings(make_settings, **overrides)
        db = Database(settings.resolved_db_path)
        db.migrate()
        store = CallStore(create_repository(settings, db))
        service = CallService(settings, store, create_provider(settings), sweep_interval=0.1)
        return service

    return build


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def live_voice(make_settings):
    """A voice call service with fake models (real-time audio) against the live Asterisk."""

    def build(*, heard=(), replies=(), **config):
        settings = live_settings(
            make_settings,
            voice_mode="ai",
            # Asterisk in WSL2 with NAT reaches Windows at the WSL adapter (docs/ASTERISK.md).
            voice_audiosocket_host=AUDIOSOCKET_HOST,
            voice_audiosocket_port=free_port(),
        )
        db = Database(settings.resolved_db_path)
        db.migrate()
        store = CallStore(create_repository(settings, db))
        runtime = fake_runtime(
            heard=heard, replies=replies, config=fast_config(frame_s=0.02, **config)
        )
        service = CallService(
            settings, store, create_provider(settings), voice=runtime, sweep_interval=0.1
        )
        return SimpleNamespace(service=service, store=store, runtime=runtime)

    return build


async def call(service, number, *, limit_s=40):
    service.store.create_permit(number, minutes=5)
    return await asyncio.wait_for(service.test_call(to_e164=number), limit_s)


async def test_live_check(live):
    service = live()
    result = await service.provider.check()
    assert result["status"] == "ok", result
    assert result["asterisk_version"]
    async with service:
        assert (await service.provider.check())["app_connected"] is True


async def test_live_answered_call_is_completed(live):
    async with live() as service:
        outcome = await call(service, ANSWER)
        assert (outcome.state, outcome.end_reason, outcome.simulated) == (
            CallState.COMPLETED,
            "hangup",
            False,
        )
        attempt = service.store.get_attempt(outcome.attempt_id)
        assert attempt["answered_at"] is not None
        assert await service.provider.call_exists(attempt["provider_call_id"]) is False


@pytest.mark.parametrize(
    ("number", "state"),
    [(BUSY, CallState.BUSY), (REJECT, CallState.REJECTED), (CONGESTION, CallState.FAILED)],
)
async def test_live_unanswered_calls(live, number, state):
    async with live() as service:
        outcome = await call(service, number)
    assert outcome.state == state, outcome


async def test_live_ring_timeout_ends_as_no_answer(live):
    async with live(call_ring_timeout=5) as service:
        outcome = await call(service, RINGING)
    assert outcome.state == CallState.NO_ANSWER, outcome


async def test_live_emergency_stop_hangs_up(live):
    async with live() as service:
        service.store.create_permit(RINGING, minutes=5)
        task = asyncio.create_task(service.test_call(to_e164=RINGING))

        def ringing():
            return [a["state"] for a in service.store.list_attempts(active_only=True)] == [
                "ringing"
            ]

        for _ in range(200):
            if ringing():
                break
            await asyncio.sleep(0.05)
        assert ringing()
        service.store.engage_kill_switch(source="test", reason="live")
        outcome = await asyncio.wait_for(task, 20)
        attempt = service.store.get_attempt(outcome.attempt_id)
        assert await service.provider.call_exists(attempt["provider_call_id"]) is False
    assert (outcome.state, outcome.end_reason) == (CallState.CANCELLED, "kill_switch")


async def test_live_same_channel_id_is_refused_by_asterisk(live):
    service = live()
    provider = service.provider

    async def sink(_event):
        return None

    await provider.start(sink)
    try:
        request = CallRequest(
            call_id="wewln-" + os.urandom(16).hex(), to_e164=RINGING, ring_timeout_s=20
        )
        first = await provider.place_call(request)
        again = await provider.place_call(request)
        assert (first.already_existed, again.already_existed) == (False, True)
        assert await provider.call_exists(request.call_id) is True
        assert await provider.hangup(request.call_id) is True
        for _ in range(100):
            if await provider.call_exists(request.call_id) is False:
                break
            await asyncio.sleep(0.05)
        assert await provider.call_exists(request.call_id) is False
        assert await provider.hangup(request.call_id) is False
    finally:
        await provider.close()


async def test_live_asterisk_reaches_the_audio_socket(live_voice):
    w = live_voice()
    async with w.service:
        check = await w.service.provider.check()
        media = await w.service.provider.check_media()
    assert check["status"] == "ok", check
    assert media["status"] == "ok", media
    assert media["connect_ms"] < 5000


async def test_live_voice_call_hears_the_person(live_voice):
    # The far end says "hello world" (Asterisk's sound file) 3 s after answering, a bit
    # after wewln's greeting; no_input_s is longer than that pause, so there is no reprompt.
    w = live_voice(heard=["hello world"], replies=["Спасибо, до свидания! [END]"], no_input_s=8.0)
    async with w.service:
        outcome = await call(w.service, SPEAKS)
    kinds = [t["kind"] for t in w.store.transcript(outcome.attempt_id)]
    assert kinds == ["greeting", "speech", "reply"], outcome
    assert (outcome.state, outcome.end_reason) == (CallState.COMPLETED, "conversation_end")
    assert w.runtime.stt.heard_ms[0] >= 500  # the recording came through the call audio


async def test_live_voice_call_audio_goes_both_ways(live_voice):
    # Echo() sends wewln's greeting back; without barge-in it is heard once the greeting ends.
    w = live_voice(heard=["эхо"], replies=["До свидания! [END]"], barge_in=False)
    async with w.service:
        outcome = await call(w.service, ECHO)
    kinds = [t["kind"] for t in w.store.transcript(outcome.attempt_id)]
    assert kinds == ["greeting", "speech", "reply"], outcome
    assert outcome.end_reason == "conversation_end"
    # About the greeting's 1.2 s came back.
    assert w.runtime.stt.heard_ms[0] >= 800, w.runtime.stt.heard_ms


async def test_live_audio_channel_dropped_mid_call_is_reconnected(live_voice, caplog):
    # What the AudioSocket bug of Asterisk 22.5.2 does, done on purpose: the audio channel
    # hangs up while the call stays up (Echo keeps it up). wewln connects a new one.
    w = live_voice(
        heard=["эхо", "эхо", "эхо"],
        replies=["Повторите, пожалуйста.", "До свидания! [END]", "До свидания! [END]"],
        barge_in=False,
    )
    async with w.service:
        w.store.create_permit(ECHO, minutes=5)
        task = asyncio.create_task(w.service.test_call(to_e164=ECHO))

        def heard_once():
            active = w.store.list_attempts(active_only=True)
            if not active:
                return None
            attempt = w.store.get_attempt(active[0]["id"])
            turns = w.store.transcript(attempt["id"])
            return attempt if any(t["kind"] == "speech" for t in turns) else None

        for _ in range(400):
            if heard_once() or task.done():
                break
            await asyncio.sleep(0.05)
        attempt = heard_once()
        assert attempt is not None, "wewln heard nothing before the drop"
        media_id = attempt["provider_call_id"] + "-media"
        assert await w.service.provider._request("DELETE", f"/ari/channels/{media_id}") == 204
        outcome = await asyncio.wait_for(task, 40)
    assert (outcome.state, outcome.end_reason) == (CallState.COMPLETED, "conversation_end"), outcome
    assert "call_media_reconnected" in caplog.text
    assert outcome.voice_turns >= 2
