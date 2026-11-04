"""Signed call-status webhooks, for providers that report call progress over HTTP.

    POST /api/v1/telephony/events
    X-Wewln-Timestamp: <Unix time in seconds>
    X-Wewln-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<raw body>" keyed with the secret>
    {"event_id": "...", "call_id": "wewln-...", "type": "ringing", "cause": 16, ...}

- The endpoint stays closed (404) until WEWLN_WEBHOOK_SECRET is set; it does not use the API
  bearer token, the signature is the authentication.
- Signatures are compared in constant time; a timestamp outside the tolerance is refused, and
  an event ID is applied only once, so a captured request cannot be replayed and a sender's
  retries are harmless (a repeat answers 200 with "duplicate").
- Nothing from the request (body, headers, secret) is logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

from ..errors import WewlnError
from .base import EventKind, ProviderEvent

MAX_BODY_BYTES = 64 * 1024
TIMESTAMP_HEADER = "X-Wewln-Timestamp"
SIGNATURE_HEADER = "X-Wewln-Signature"
_EVENT_ID = re.compile(r"[A-Za-z0-9_.:-]{8,100}")
_CALL_ID = re.compile(r"wewln-[0-9a-f]{32}")
_SIGNATURE = re.compile(r"v1=[0-9a-f]{64}")


class WebhookError(WewlnError):
    code = "webhook_rejected"
    http_status = 401


def sign_webhook(secret: str, timestamp: int, body: bytes) -> str:
    """The signature header value for `body` (used by senders and by the tests)."""
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return "v1=" + digest.hexdigest()


def verify_webhook(
    secret: str, headers: Mapping[str, str], body: bytes, *, now: float, tolerance_s: int
) -> None:
    """Raise WebhookError unless the request is signed with `secret` and recent enough."""
    raw_ts = headers.get(TIMESTAMP_HEADER, "")
    if not (raw_ts.isascii() and raw_ts.isdigit()) or len(raw_ts) > 12:
        raise WebhookError("missing or invalid timestamp header")
    if abs(now - int(raw_ts)) > tolerance_s:
        raise WebhookError("the timestamp is too old or in the future (check the clock)")
    expected = sign_webhook(secret, int(raw_ts), body)
    supplied = [part.strip() for part in headers.get(SIGNATURE_HEADER, "").split(",")]
    if not any(
        _SIGNATURE.fullmatch(value) and hmac.compare_digest(value.encode(), expected.encode())
        for value in supplied
    ):
        raise WebhookError("invalid signature")


def _optional_int(payload: dict[str, Any], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
        raise WebhookError(f"'{name}' must be an integer 0-999", http_status=400)
    return value


def parse_webhook_event(body: bytes) -> ProviderEvent:
    """The event of a verified request. Raises WebhookError (400) for an invalid payload."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise WebhookError("the body is not valid JSON", http_status=400) from None
    if not isinstance(payload, dict):
        raise WebhookError("the body must be a JSON object", http_status=400)
    event_id = payload.get("event_id")
    call_id = payload.get("call_id")
    kind = payload.get("type")
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise WebhookError("'event_id' must be 8-100 letters, digits or ._:-", http_status=400)
    if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
        raise WebhookError("'call_id' is not a wewln call ID", http_status=400)
    if kind not in {k.value for k in EventKind}:
        raise WebhookError(
            "'type' must be one of: " + ", ".join(k.value for k in EventKind), http_status=400
        )
    cause_txt = payload.get("cause_txt")
    if cause_txt is not None and not isinstance(cause_txt, str):
        raise WebhookError("'cause_txt' must be a string", http_status=400)
    return ProviderEvent(
        call_id=call_id,
        kind=EventKind(kind),
        event_key="webhook:" + event_id,
        source="webhook",
        cause=_optional_int(payload, "cause"),
        cause_txt=(cause_txt or "")[:100] or None,
        tech_cause=_optional_int(payload, "tech_cause"),
    )
