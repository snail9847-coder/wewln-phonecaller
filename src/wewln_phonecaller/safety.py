"""The real-call gate: every condition must hold at once, otherwise calls stay simulated."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .config import PRODUCTION_CONFIRM_PHRASE, Settings
from .errors import RealCallsBlockedError
from .telephony import PROVIDERS, ProviderInfo


class BlockReason(StrEnum):
    SIMULATION_MODE = "simulation_mode"
    PRODUCTION_NOT_CONFIRMED = "production_not_confirmed"
    KILL_SWITCH = "kill_switch_engaged"
    PROVIDER_UNKNOWN = "provider_unknown"
    PROVIDER_SIMULATION_ONLY = "provider_simulation_only"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    CALL_REGIONS_NOT_SET = "call_regions_not_set"
    OPERATOR_NOT_ARMED = "operator_not_armed"


@dataclass(frozen=True)
class RealCallDecision:
    allowed: bool
    reasons: tuple[BlockReason, ...]

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "blocked_by": [r.value for r in self.reasons]}


def evaluate_real_calls(
    settings: Settings,
    *,
    operator_armed: bool = False,
    kill_switch_engaged: bool = False,
    providers: Mapping[str, ProviderInfo] = PROVIDERS,
) -> RealCallDecision:
    """Decide whether a real (non-simulated) call may be placed right now.

    `operator_armed`: the operator's explicit permission for this very call. In this version
    only a real-call permit for one test number (`wewln calls permit`) arms; campaigns are armed
    in Phase 6, so real campaign calls stay blocked.
    `kill_switch_engaged`: the emergency stop set at runtime (`wewln calls stop-all`).
    """
    reasons: list[BlockReason] = []
    if settings.mode != "production":
        reasons.append(BlockReason.SIMULATION_MODE)
    elif settings.production_confirm != PRODUCTION_CONFIRM_PHRASE:
        reasons.append(BlockReason.PRODUCTION_NOT_CONFIRMED)
    if settings.kill_switch or kill_switch_engaged:
        reasons.append(BlockReason.KILL_SWITCH)
    provider = providers.get(settings.telephony_provider)
    if provider is None:
        reasons.append(BlockReason.PROVIDER_UNKNOWN)
    elif not provider.places_real_calls:
        reasons.append(BlockReason.PROVIDER_SIMULATION_ONLY)
    else:
        if settings.provider_problems:
            reasons.append(BlockReason.PROVIDER_NOT_CONFIGURED)
        if not settings.allowed_call_regions:
            reasons.append(BlockReason.CALL_REGIONS_NOT_SET)
    if not operator_armed:
        reasons.append(BlockReason.OPERATOR_NOT_ARMED)
    return RealCallDecision(allowed=not reasons, reasons=tuple(reasons))


def ensure_real_calls_allowed(
    settings: Settings,
    *,
    operator_armed: bool = False,
    kill_switch_engaged: bool = False,
    providers: Mapping[str, ProviderInfo] = PROVIDERS,
) -> None:
    """Raise RealCallsBlockedError unless every condition for a real call holds."""
    decision = evaluate_real_calls(
        settings,
        operator_armed=operator_armed,
        kill_switch_engaged=kill_switch_engaged,
        providers=providers,
    )
    if not decision.allowed:
        raise RealCallsBlockedError(
            "Real calls are blocked: " + ", ".join(r.value for r in decision.reasons),
            details=decision.as_dict(),
        )
