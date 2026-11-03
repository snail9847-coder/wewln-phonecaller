"""Telephony provider registry.

This version ships only the simulation provider. Real adapters (SIP/PBX or a carrier API) are
added in Phase 4 and must declare `places_real_calls=True` to be usable at all.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    places_real_calls: bool
    description: str


PROVIDERS: dict[str, ProviderInfo] = {
    "mock": ProviderInfo(
        name="mock",
        places_real_calls=False,
        description="Simulated calls only; never touches a phone network.",
    ),
}
