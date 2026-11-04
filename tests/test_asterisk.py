"""The Asterisk provider against the ARI simulator: no Asterisk, no SIP, no phone network.

What these tests prove is how wewln drives ARI (requests, events, errors). Whether a real trunk
reaches real phones can only be shown with a SIP account; see docs/ASTERISK.md.
"""

import asyncio
import base64
import logging
from types import SimpleNamespace

import pytest

from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE
from wewln_phonecaller.storage import Database
from wewln_phonecaller.storage.calls import CallStore
from wewln_phonecaller.telephony.asterisk import (
    AriConfig,
    AsteriskAriProvider,
    build_endpoint,
    format_number,
    trunk_of,
)
from wewln_phonecaller.telephony.base import (
    CallRequest,
    EventKind,
    ProviderError,
    ProviderRejectedError,
    ProviderUnavailableError,
    ProviderUncertainError,
    provider_call_id,
)
from wewln_phonecaller.telephony.factory import create_provider, create_repository
from wewln_phonecaller.telephony.lifecycle import CallState
from wewln_phonecaller.telephony.service import CallService

PHONE = "+994501234567"
CALLER = "+994121234567"


def provider_for(sim, *, password=None, url=None, **config):
    config.setdefault("connect_timeout", 2.0)
    config.setdefault("request_timeout", 2.0)
    return AsteriskAriProvider(
        AriConfig(
            url=url or sim.base_url,
            username=sim.username,
            password=sim.password if password is None else password,
            **config,
        ),
        reconnect_delays=(0.01,),
    )


async def until(predicate, limit_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class Events:
    def __init__(self):
        self.items = []

    async def __call__(self, event):
        self.items.append(event)

    def kinds(self, call_id):
        return [e.kind for e in self.items if e.call_id == call_id]


def request(call_id="wewln-" + "0" * 32, to=PHONE, **kwargs):
    return CallRequest(call_id=call_id, to_e164=to, ring_timeout_s=30, **kwargs)


def posts(sim, path_prefix="/ari/channels/wewln-"):
    """Requests to create a channel (not to play on one)."""
    return [
        r
        for r in sim.requests
        if r[0] == "POST" and r[1].startswith(path_prefix) and not r[1].endswith("/play")
    ]


@pytest.fixture
def ari_world(ari_sim, make_settings):
    """Settings for real calls through the simulated Asterisk, and the call service over it."""

    def build(**overrides):
        values = {
            "mode": "production",
            "production_confirm": PRODUCTION_CONFIRM_PHRASE,
            "telephony_provider": "asterisk",
            "ari_url": ari_sim.base_url,
            "ari_password": ari_sim.password,
            "call_regions": "AZ",
            "caller_id": CALLER,
            **overrides,
        }
        settings = make_settings(**values)
        db = Database(settings.resolved_db_path)
        db.migrate()
        store = CallStore(create_repository(settings, db), retry_delay_s=settings.call_retry_delay)
        provider = create_provider(settings)
        provider._delays = (0.01,)
        service = CallService(settings, store, provider, sweep_interval=0.01)
        return SimpleNamespace(settings=settings, store=store, provider=provider, service=service)

    return build


def test_numbers_and_dial_strings():
    assert format_number(PHONE, "e164") == "+994501234567"
    assert format_number(PHONE, "digits") == "994501234567"
    assert format_number(PHONE, "00") == "00994501234567"
    with pytest.raises(ValueError):
        format_number("+99450abc", "e164")
    assert build_endpoint("PJSIP/{number}@wewln-trunk", "994501234567") == (
        "PJSIP/994501234567@wewln-trunk"
    )
    assert trunk_of("PJSIP/{number}@wewln-trunk") == ("PJSIP", "wewln-trunk")
    assert trunk_of("Local/{number}@wewln-ci") is None  # a dialplan context, not a trunk


async def test_check_reports_version_app_and_trunk(ari_sim):
    provider = provider_for(ari_sim)
    before = await provider.check()
    assert before["status"] == "ok"
    assert (before["asterisk_version"], before["app_connected"]) == ("22.5.2", False)
    assert before["trunk"] == {"name": "wewln-trunk", "state": "online"}
    await provider.start(Events())
    try:
        assert (await provider.check())["app_connected"] is True
    finally:
        await provider.close()
    missing = await provider_for(ari_sim, endpoint_template="PJSIP/{number}@other").check()
    assert missing["status"] == "error"
    assert "pjsip.conf" in missing["error"]


async def test_check_explains_a_wrong_password_and_a_stopped_asterisk(ari_sim, unused_tcp_port):
    wrong = await provider_for(ari_sim, password="not-the-password").check()
    assert (wrong["status"], wrong["error"]) == ("error", "ARI rejected the user name or password")
    down = await provider_for(ari_sim, url=f"http://127.0.0.1:{unused_tcp_port}").check()
    assert down["status"] == "error"
    assert "cannot connect" in down["error"]


async def test_start_fails_on_a_wrong_password_or_no_asterisk(ari_sim, unused_tcp_port):
    with pytest.raises(ProviderRejectedError) as rejected:
        await provider_for(ari_sim, password="not-the-password").start(Events())
    assert rejected.value.code == "ari_auth"
    nobody = provider_for(ari_sim, url=f"http://127.0.0.1:{unused_tcp_port}", connect_timeout=0.3)
    with pytest.raises(ProviderUnavailableError, match="cannot connect"):
        await nobody.start(Events())


async def test_answered_call_plays_the_message_then_hangs_up(ari_world, ari_sim):
    w = ari_world()
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
    assert (outcome.state, outcome.end_reason, outcome.simulated) == (
        CallState.COMPLETED,
        "hangup",
        False,
    )
    attempt = w.store.get_attempt(outcome.attempt_id)
    call_id = attempt["provider_call_id"]
    assert call_id == provider_call_id(attempt["idempotency_key"])
    ((method, path, query, _headers),) = posts(ari_sim, f"/ari/channels/{call_id}")[:1]
    assert (method, path) == ("POST", f"/ari/channels/{call_id}")
    assert query == {
        "endpoint": "PJSIP/+994501234567@wewln-trunk",
        "app": "wewln",
        "appArgs": "wewln",
        "timeout": "30",
        "callerId": CALLER,
    }
    play = [r for r in ari_sim.requests if r[1] == f"/ari/channels/{call_id}/play"]
    assert [r[2] for r in play] == [{"media": "sound:hello-world"}]
    assert ("DELETE", f"/ari/channels/{call_id}") in [(r[0], r[1]) for r in ari_sim.requests]
    assert ari_sim.channels == {}


@pytest.mark.parametrize(
    ("scenario", "state", "end_reason"),
    [
        ("busy", CallState.BUSY, "busy"),
        ("busy_signal", CallState.BUSY, "busy"),
        ("reject", CallState.REJECTED, "rejected"),
        ("unreachable", CallState.UNREACHABLE, "unreachable"),
        ("provider_refused", CallState.FAILED, "provider_refused"),
    ],
)
async def test_unanswered_calls_are_classified(ari_world, ari_sim, scenario, state, end_reason):
    ari_sim.default_scenario = scenario
    w = ari_world()
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        outcome = await w.service.test_call(to_e164=PHONE)
    assert (outcome.state, outcome.end_reason) == (state, end_reason)


async def test_early_media_counts_as_ringing(ari_sim):
    ari_sim.default_scenario = "early_media"
    events = Events()
    provider = provider_for(ari_sim)
    await provider.start(events)
    try:
        await provider.place_call(request())
        await until(lambda: EventKind.RINGING in events.kinds(request().call_id))
    finally:
        await provider.close()


async def test_number_format_and_softphone_endpoint(ari_world, ari_sim):
    w = ari_world(asterisk_number_format="digits", caller_id=None)
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        await w.service.test_call(to_e164=PHONE)
        await w.service.test_call(softphone=True)
    phone, softphone = (r[2] for r in posts(ari_sim))
    assert phone["endpoint"] == "PJSIP/994501234567@wewln-trunk"
    assert "callerId" not in phone
    assert softphone["endpoint"] == "PJSIP/softphone"


async def test_the_same_attempt_never_creates_a_second_call(ari_sim):
    ari_sim.default_scenario = "ringing"
    provider = provider_for(ari_sim)
    await provider.start(Events())
    try:
        first = await provider.place_call(request())
        again = await provider.place_call(request())
    finally:
        await provider.close()
    assert (first.already_existed, again.already_existed) == (False, True)
    assert len(posts(ari_sim)) == 2
    assert len(ari_sim.channels) == 1


async def test_a_request_without_an_answer_is_not_repeated(ari_sim):
    ari_sim.default_scenario = "ringing"
    ari_sim.post_behavior = "timeout"
    ari_sim.post_delay = 1.0
    provider = provider_for(ari_sim, request_timeout=0.2)
    await provider.start(Events())
    try:
        with pytest.raises(ProviderUncertainError):
            await provider.place_call(request())
        assert len(posts(ari_sim)) == 1  # looked (GET), did not ask again
        ari_sim.post_behavior = "create_then_timeout"
        late = await provider.place_call(request("wewln-" + "1" * 32))
        assert late.already_existed is True  # the channel exists although no answer came
        ari_sim.post_behavior = "error"
        with pytest.raises(ProviderError) as error:
            await provider.place_call(request("wewln-" + "2" * 32))
        assert error.value.code == "ari_server_error"
    finally:
        await provider.close()


async def test_events_reconnect_after_a_dropped_connection(ari_sim):
    events = Events()
    provider = provider_for(ari_sim)
    await provider.start(events)
    try:
        await ari_sim.drop_websockets()
        await until(lambda: ari_sim.ws_connects == 2 and provider.connected)
        await provider.place_call(request())
        await until(lambda: EventKind.HANGUP in events.kinds(request().call_id))
    finally:
        await provider.close()
    assert events.kinds(request().call_id)[:2] == [EventKind.RINGING, EventKind.RINGING]


async def test_dialing_stops_when_the_events_are_down(ari_sim):
    provider = provider_for(ari_sim)
    provider._delays = (60,)
    await provider.start(Events())
    try:
        await ari_sim.drop_websockets()
        await until(lambda: not provider.connected)
        with pytest.raises(ProviderUnavailableError):
            await provider.place_call(request())
    finally:
        await provider.close()
    assert posts(ari_sim) == []


async def test_emergency_stop_hangs_up_over_ari(ari_world, ari_sim):
    ari_sim.default_scenario = "ringing"
    w = ari_world()
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        task = asyncio.create_task(w.service.test_call(to_e164=PHONE))
        await until(lambda: len(ari_sim.channels) == 1)
        w.store.engage_kill_switch(source="test", reason="stop")
        outcome = await asyncio.wait_for(task, 5)
    assert (outcome.state, outcome.end_reason) == (CallState.CANCELLED, "kill_switch")
    assert [r[0] for r in ari_sim.requests if r[1].startswith("/ari/channels/wewln-")].count(
        "DELETE"
    ) == 1
    assert ari_sim.channels == {}


async def test_emergency_stop_while_asterisk_creates_the_channel(ari_world, ari_sim):
    # The channel already exists, but the dial request has not returned yet.
    ari_sim.default_scenario = "ringing"
    ari_sim.post_behavior = "create_then_timeout"
    ari_sim.post_delay = 1.0
    w = ari_world()
    w.store.create_permit(PHONE, minutes=5)

    def deletes():
        methods = [r[0] for r in ari_sim.requests if r[1].startswith("/ari/channels/wewln-")]
        return methods.count("DELETE")

    async with w.service:
        task = asyncio.create_task(w.service.test_call(to_e164=PHONE))
        await until(lambda: len(ari_sim.channels) == 1)
        w.store.engage_kill_switch(source="test", reason="stop")
        requests = lambda: [a["end_request"] for a in w.store.active_attempts()]  # noqa: E731
        await until(lambda: requests() == ["kill_switch"])
        await asyncio.sleep(0.1)
        assert deletes() == 0  # recorded; the hangup waits for the dial request to return
        outcome = await asyncio.wait_for(task, 5)
    assert (outcome.state, outcome.end_reason) == (CallState.CANCELLED, "kill_switch")
    assert deletes() == 1
    assert ari_sim.channels == {}


async def test_restart_ends_a_call_still_live_in_asterisk(ari_world, ari_sim):
    ari_sim.default_scenario = "ringing"
    w = ari_world()
    w.store.create_permit(PHONE, minutes=5)
    # A previous run placed the call and crashed before it ended.
    ticket = w.store.begin_test_attempt(
        provider="asterisk", simulated=False, to_e164=PHONE, require_permit=True
    )
    crashed = provider_for(ari_sim)
    await crashed.start(Events())
    await crashed.place_call(request(ticket.call_id))
    w.store.mark_dialing(ticket.attempt_id)
    await crashed.close()
    async with w.service:
        outcome = await asyncio.wait_for(w.service.wait_for_end(ticket.attempt_id), 5)
    assert (outcome.state, outcome.end_reason) == (CallState.FAILED, "state_lost")
    assert ari_sim.channels == {}


async def test_channels_wewln_did_not_place_are_hung_up(ari_sim):
    events = Events()
    provider = provider_for(ari_sim)
    await provider.start(events)
    try:
        await ari_sim.foreign_stasis_start("wewln")
        await until(lambda: ari_sim.channels == {})
    finally:
        await provider.close()
    assert ("DELETE", "/ari/channels/1730000000.42") in [(r[0], r[1]) for r in ari_sim.requests]
    assert events.items == []


async def test_another_program_taking_the_app_stops_dialing(ari_sim):
    first, second = provider_for(ari_sim), provider_for(ari_sim)
    await first.start(Events())
    await second.start(Events())
    try:
        await until(lambda: first._replaced)
        with pytest.raises(ProviderUnavailableError) as replaced:
            await first.place_call(request())
        assert replaced.value.code == "ari_app_replaced"
        await asyncio.sleep(0.1)
        assert ari_sim.ws_connects == 2  # the replaced one does not fight back
    finally:
        await first.close()
        await second.close()


async def test_the_password_travels_only_in_the_authorization_header(ari_world, ari_sim, caplog):
    caplog.set_level(logging.DEBUG)
    w = ari_world()
    w.store.create_permit(PHONE, minutes=5)
    async with w.service:
        await w.service.test_call(to_e164=PHONE)
        await w.provider.check()
    expected = "Basic " + base64.b64encode(f"wewln:{ari_sim.password}".encode()).decode()
    assert ari_sim.requests
    for _method, path, query, headers in ari_sim.requests:
        assert headers["Authorization"] == expected
        assert ari_sim.password not in path
        assert all(ari_sim.password not in value for value in query.values())
    assert ari_sim.password not in caplog.text
    assert ari_sim.password not in repr(w.provider.cfg)
    assert ari_sim.password not in repr(w.settings)
