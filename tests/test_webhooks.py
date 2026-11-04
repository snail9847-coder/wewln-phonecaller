"""Signed call-status webhooks and the calls endpoints of the local API."""

import json
import logging
import time
from types import SimpleNamespace

import pytest

from wewln_phonecaller.api import create_app
from wewln_phonecaller.storage import Database
from wewln_phonecaller.storage.calls import CallStore
from wewln_phonecaller.telephony.factory import create_repository
from wewln_phonecaller.telephony.webhooks import (
    MAX_BODY_BYTES,
    WebhookError,
    sign_webhook,
    verify_webhook,
)

SECRET = "w" * 40
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PHONE = "+994501234567"
PATH = "/api/v1/telephony/events"


@pytest.fixture
def api(aiohttp_client, make_settings):
    async def build(**overrides):
        overrides.setdefault("webhook_secret", SECRET)
        overrides.setdefault("api_token", TOKEN)
        settings = make_settings(**overrides)
        db = Database(settings.resolved_db_path)
        db.migrate()
        client = await aiohttp_client(create_app(settings, db))
        store = CallStore(create_repository(settings, db))
        return SimpleNamespace(client=client, store=store, settings=settings)

    return build


def signed(payload, *, secret=SECRET, ts=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    ts = int(time.time()) if ts is None else ts
    headers = {
        "Content-Type": "application/json",
        "X-Wewln-Timestamp": str(ts),
        "X-Wewln-Signature": sign_webhook(secret, ts, body),
    }
    return {"data": body, "headers": headers}


def a_call(store):
    ticket = store.begin_test_attempt(provider="mock", simulated=True, to_e164=PHONE)
    store.mark_dialing(ticket.attempt_id)
    return ticket


def event(call_id, kind, event_id="evt-00000001", **extra):
    return {"event_id": event_id, "call_id": call_id, "type": kind, **extra}


def test_signatures_are_checked_in_full():
    body = b'{"x": 1}'
    now = 1_800_000_000
    good = sign_webhook(SECRET, now, body)
    headers = {"X-Wewln-Timestamp": str(now), "X-Wewln-Signature": good}
    verify_webhook(SECRET, headers, body, now=now, tolerance_s=300)
    # A second signature (key rotation at the sender) may come first.
    rotated = {**headers, "X-Wewln-Signature": "v1=" + "0" * 64 + ", " + good}
    verify_webhook(SECRET, rotated, body, now=now, tolerance_s=300)
    for bad in (
        {**headers, "X-Wewln-Signature": good.upper()},
        {**headers, "X-Wewln-Signature": good[:-1]},
        {**headers, "X-Wewln-Signature": sign_webhook("other" * 8, now, body)},
        {**headers, "X-Wewln-Timestamp": "abc"},
        {"X-Wewln-Signature": good},
    ):
        with pytest.raises(WebhookError):
            verify_webhook(SECRET, bad, body, now=now, tolerance_s=300)
    with pytest.raises(WebhookError, match="too old"):
        verify_webhook(SECRET, headers, body, now=now + 301, tolerance_s=300)
    with pytest.raises(WebhookError):
        verify_webhook(SECRET, headers, body + b" ", now=now, tolerance_s=300)


async def test_webhooks_are_closed_without_a_secret(api):
    w = await api(webhook_secret=None)
    resp = await w.client.post(PATH, **signed(event("wewln-" + "0" * 32, "ringing")))
    assert resp.status == 404


async def test_a_signed_event_is_applied_once(api):
    w = await api()
    ticket = a_call(w.store)
    resp = await w.client.post(PATH, **signed(event(ticket.call_id, "ringing")))
    assert (resp.status, await resp.json()) == (200, {"result": "applied"})
    again = await w.client.post(PATH, **signed(event(ticket.call_id, "ringing")))
    assert (again.status, await again.json()) == (200, {"result": "duplicate"})
    hangup = event(ticket.call_id, "hangup", "evt-00000002", cause=17, cause_txt="User busy")
    resp = await w.client.post(PATH, **signed(hangup))
    assert (await resp.json())["result"] == "applied"
    attempt = w.store.get_attempt(ticket.attempt_id)
    assert (attempt["state"], attempt["hangup_cause"]) == ("busy", 17)
    late = await w.client.post(PATH, **signed(event(ticket.call_id, "answered", "evt-00000003")))
    assert (await late.json())["result"] == "ignored"  # states only move forward
    unknown = await w.client.post(
        PATH, **signed(event("wewln-" + "f" * 32, "ringing", "evt-00000004"))
    )
    assert (await unknown.json())["result"] == "unknown_call"


async def test_unsigned_stale_or_forged_events_are_refused(api):
    w = await api()
    ticket = a_call(w.store)
    payload = event(ticket.call_id, "hangup", cause=16)
    body = json.dumps(payload).encode()
    attempts = [
        {"data": body, "headers": {"Content-Type": "application/json"}},
        {"data": body, "headers": {**AUTH, "Content-Type": "application/json"}},
        signed(payload, secret="x" * 40),
        signed(payload, ts=int(time.time()) - 301),
        signed(payload, ts=int(time.time()) + 301),
    ]
    forged = signed(payload)
    forged["data"] = json.dumps({**payload, "cause": 21}).encode()
    attempts.append(forged)
    for request in attempts:
        resp = await w.client.post(PATH, **request)
        assert resp.status == 401
        assert (await resp.json())["error"]["code"] == "webhook_rejected"
    assert w.store.get_attempt(ticket.attempt_id)["state"] == "dialing"


async def test_oversized_and_invalid_events_are_refused(api):
    w = await api()
    ticket = a_call(w.store)
    huge = signed(b'{"pad": "' + b"x" * MAX_BODY_BYTES + b'"}')
    assert (await w.client.post(PATH, **huge)).status == 413
    for payload in (
        b"not json",
        b"[1, 2]",
        event("wewln-123", "ringing"),
        event(ticket.call_id, "exploded"),
        event(ticket.call_id, "hangup", cause=1000),
        event(ticket.call_id, "hangup", cause=True),
        event(ticket.call_id, "ringing", event_id="short"),
    ):
        resp = await w.client.post(PATH, **signed(payload))
        assert resp.status == 400, payload
    assert w.store.get_attempt(ticket.attempt_id)["state"] == "dialing"


async def test_nothing_secret_from_a_webhook_is_logged(api, caplog):
    caplog.set_level(logging.DEBUG)
    w = await api()
    ticket = a_call(w.store)
    request = signed(event(ticket.call_id, "ringing"))
    await w.client.post(PATH, **request)
    await w.client.post(PATH, **signed(event(ticket.call_id, "ringing"), secret="x" * 40))
    assert SECRET not in caplog.text
    assert request["headers"]["X-Wewln-Signature"] not in caplog.text
    assert PHONE not in caplog.text


async def test_calls_endpoints_need_the_token_and_mask_numbers(api):
    w = await api()
    ticket = a_call(w.store)
    assert (await w.client.get("/api/v1/calls")).status == 401
    resp = await w.client.get("/api/v1/calls", headers=AUTH)
    body = await resp.json()
    assert (body["emergency_stop"], body["dialer_running"]) == (None, False)
    (call,) = body["calls"]
    assert (call["id"], call["state"], call["simulated"]) == (ticket.attempt_id, "dialing", True)
    assert PHONE not in json.dumps(body)
    assert call["phone"].startswith("+9") and call["phone"].endswith("67")
    active = await (await w.client.get("/api/v1/calls?active=1&limit=5", headers=AUTH)).json()
    assert len(active["calls"]) == 1
    bad = await w.client.get("/api/v1/calls?limit=many", headers=AUTH)
    assert bad.status == 400


async def test_cancel_and_emergency_stop_over_the_api(api):
    w = await api()
    ticket = a_call(w.store)
    url = f"/api/v1/calls/{ticket.attempt_id}/cancel"
    assert (await w.client.post(url)).status == 401
    resp = await w.client.post(url, headers=AUTH)
    assert (await resp.json()) == {"cancel_requested": True, "dialer_running": False}
    assert (await w.client.post("/api/v1/calls/999/cancel", headers=AUTH)).status == 404
    assert (await w.client.post("/api/v1/calls/stop-all")).status == 401
    stop = await w.client.post("/api/v1/calls/stop-all", json={"reason": "test"}, headers=AUTH)
    assert (await stop.json())["emergency_stop"] == "engaged"
    again = await w.client.post("/api/v1/calls/stop-all", headers=AUTH)
    assert (await again.json())["emergency_stop"] == "already_engaged"
    ready = await (await w.client.get("/ready")).json()
    assert ready["checks"]["telephony"]["emergency_stop"] is True
    assert "kill_switch_engaged" in ready["checks"]["real_calls"]["blocked_by"]
    listed = await (await w.client.get("/api/v1/calls", headers=AUTH)).json()
    assert listed["emergency_stop"]["source"] == "api"
    # Resuming is deliberately possible only on the computer itself (wewln calls resume).
    assert (await w.client.post("/api/v1/calls/resume", headers=AUTH)).status in (404, 405)
    assert w.store.kill_switch() is not None
