"""Telephony providers.

- `mock`: simulated calls with realistic states (ringing, answer, busy …); never touches a network.
- `asterisk`: your own Asterisk PBX (in WSL2 on Windows) controlled over ARI; calls reach the phone
  network only through a SIP trunk configured in Asterisk. It can place real calls, so it is
  usable only behind the real-call gate (safety.py).
"""

from __future__ import annotations

from .base import ProviderInfo

PROVIDERS: dict[str, ProviderInfo] = {
    "mock": ProviderInfo(
        name="mock",
        places_real_calls=False,
        description="Simulated calls only; never touches a phone network.",
    ),
    "asterisk": ProviderInfo(
        name="asterisk",
        places_real_calls=True,
        description="Own Asterisk PBX over ARI; the phone network only through its SIP trunk.",
    ),
}

__all__ = ["PROVIDERS", "ProviderInfo"]
