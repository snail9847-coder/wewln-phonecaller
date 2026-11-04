"""The provider interface: what the call service needs from any telephony backend.

A provider places calls, hangs them up and reports what happens to them as `ProviderEvent`s.
It keeps no call state of its own that matters: the call service stores every attempt and every
event in SQLite, so a restart or a repeated event never causes a second call.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..voice.transport import MediaStream


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    places_real_calls: bool
    description: str


class EventKind(StrEnum):
    DIALING = "dialing"
    RINGING = "ringing"
    ANSWERED = "answered"
    HANGUP = "hangup"


@dataclass(frozen=True)
class ProviderEvent:
    """Something that happened to a call. `event_key` is unique per real-world event: the same
    event delivered twice (reconnect, webhook retry) carries the same key and is applied once."""

    call_id: str
    kind: EventKind
    event_key: str
    source: str
    cause: int | None = None
    cause_txt: str | None = None
    tech_cause: int | None = None


@dataclass(frozen=True)
class CallRequest:
    """One dial attempt. `call_id` is derived from the attempt's idempotency key, so asking the
    provider twice for the same attempt can never create two calls."""

    call_id: str
    to_e164: str
    ring_timeout_s: int
    caller_id: str | None = None
    # A PBX-internal destination (a softphone) instead of the phone number: no phone network.
    local_endpoint: str | None = None


@dataclass(frozen=True)
class PlaceCallResult:
    call_id: str
    already_existed: bool = False


class ProviderError(Exception):
    """A provider request failed.

    `retryable`: trying the attempt again later may work (outage, overload).
    `call_may_exist`: the provider may have created the call anyway (timeout after sending), so
    the attempt must not be repeated automatically.
    """

    code = "provider_error"
    retryable = True
    call_may_exist = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ProviderUnavailableError(ProviderError):
    """Nothing was sent (connection refused, event channel down): certainly no call."""

    code = "provider_unavailable"


class ProviderRejectedError(ProviderError):
    """The provider refused the request (authentication, bad parameters): fix the settings."""

    code = "provider_rejected"
    retryable = False


class MediaError(ProviderError):
    """The call's audio could not be connected to wewln (the call itself may live on)."""

    code = "media_failed"
    retryable = False


class ProviderUncertainError(ProviderError):
    """The request may have reached the provider, but no answer arrived."""

    code = "provider_uncertain"
    retryable = False
    call_may_exist = True


EventSink = Callable[[ProviderEvent], Awaitable[None]]


class TelephonyProvider(ABC):
    info: ProviderInfo
    # Whether the provider can connect a call's audio to wewln (the voice engine, Phase 5).
    supports_media: bool = False
    media_enabled: bool = False

    def enable_media(self) -> None:
        """Answered calls get their audio connected by open_media() instead of the fixed
        answer media. Called before start()."""
        if not self.supports_media:
            raise MediaError(
                f"the {self.info.name} provider cannot carry call audio", code="media_unsupported"
            )
        self.media_enabled = True

    async def open_media(self, call_id: str) -> MediaStream:
        """Connect the answered call's audio to wewln (8 kHz 16-bit mono PCM both ways).
        Raises MediaError; the call service then ends the call."""
        raise MediaError(
            f"the {self.info.name} provider cannot carry call audio", code="media_unsupported"
        )

    @abstractmethod
    async def start(self, sink: EventSink) -> None:
        """Connect and start delivering events to `sink`. Raises ProviderError if unreachable."""

    @abstractmethod
    async def close(self) -> None:
        """Stop delivering events and release connections (calls in progress are not touched)."""

    @abstractmethod
    async def place_call(self, request: CallRequest) -> PlaceCallResult:
        """Start dialing. Idempotent: the same `call_id` never creates a second call."""

    @abstractmethod
    async def hangup(self, call_id: str) -> bool:
        """End the call. False when the provider does not know the call (already over)."""

    @abstractmethod
    async def call_exists(self, call_id: str) -> bool | None:
        """Whether the call still exists at the provider; None when that cannot be told."""

    @abstractmethod
    async def check(self) -> dict[str, Any]:
        """Connection and configuration status for `wewln telephony check` (never secrets)."""


def provider_call_id(attempt_key: str) -> str:
    """A provider-safe call ID (letters, digits, dashes) derived from the attempt key."""
    return "wewln-" + hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()[:32]
