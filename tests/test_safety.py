import pytest
from pydantic import SecretStr

from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE, Settings
from wewln_phonecaller.errors import RealCallsBlockedError
from wewln_phonecaller.safety import BlockReason, ensure_real_calls_allowed, evaluate_real_calls
from wewln_phonecaller.telephony import PROVIDERS, ProviderInfo

# A test double that claims real-call capability (needs no configuration of its own).
FAKE_REAL = {"fake-real": ProviderInfo("fake-real", True, "test double")}


def production(**fields):
    base = {
        "mode": "production",
        "production_confirm": PRODUCTION_CONFIRM_PHRASE,
        "telephony_provider": "fake-real",
        "call_regions": "AZ",
    }
    base.update(fields)
    return Settings.model_construct(**base)


def test_only_asterisk_can_place_real_calls():
    assert {name for name, p in PROVIDERS.items() if p.places_real_calls} == {"asterisk"}
    assert PROVIDERS["mock"].places_real_calls is False


def test_real_provider_needs_call_regions():
    decision = evaluate_real_calls(
        production(call_regions=""), operator_armed=True, providers=FAKE_REAL
    )
    assert decision.reasons == (BlockReason.CALL_REGIONS_NOT_SET,)


def test_asterisk_without_password_is_not_configured():
    settings = production(telephony_provider="asterisk")
    decision = evaluate_real_calls(settings, operator_armed=True)
    assert decision.reasons == (BlockReason.PROVIDER_NOT_CONFIGURED,)
    ready = production(telephony_provider="asterisk", ari_password=SecretStr("pw"))
    assert evaluate_real_calls(ready, operator_armed=True).allowed is True


def test_runtime_emergency_stop_blocks():
    decision = evaluate_real_calls(
        production(), operator_armed=True, kill_switch_engaged=True, providers=FAKE_REAL
    )
    assert decision.reasons == (BlockReason.KILL_SWITCH,)


def test_asterisk_in_simulation_mode_is_blocked(make_settings):
    s = make_settings(telephony_provider="asterisk", ari_password="pw", call_regions="AZ")
    decision = evaluate_real_calls(s, operator_armed=True)
    assert decision.reasons == (BlockReason.SIMULATION_MODE,)


def test_default_configuration_blocks_real_calls(make_settings):
    decision = evaluate_real_calls(make_settings())
    assert decision.allowed is False
    assert set(decision.reasons) == {
        BlockReason.SIMULATION_MODE,
        BlockReason.PROVIDER_SIMULATION_ONLY,
        BlockReason.OPERATOR_NOT_ARMED,
    }


def test_confirmed_production_with_mock_provider_is_still_blocked(make_settings):
    s = make_settings(mode="production", production_confirm=PRODUCTION_CONFIRM_PHRASE)
    decision = evaluate_real_calls(s, operator_armed=True)
    assert decision.allowed is False
    assert decision.reasons == (BlockReason.PROVIDER_SIMULATION_ONLY,)


def test_all_conditions_together_allow_real_calls():
    decision = evaluate_real_calls(production(), operator_armed=True, providers=FAKE_REAL)
    assert decision.allowed is True
    assert decision.reasons == ()


def test_kill_switch_blocks_even_when_everything_else_is_ready():
    decision = evaluate_real_calls(
        production(kill_switch=True), operator_armed=True, providers=FAKE_REAL
    )
    assert decision.reasons == (BlockReason.KILL_SWITCH,)


def test_operator_must_arm():
    decision = evaluate_real_calls(production(), operator_armed=False, providers=FAKE_REAL)
    assert decision.reasons == (BlockReason.OPERATOR_NOT_ARMED,)


def test_unconfirmed_production_is_blocked_even_if_validation_was_bypassed():
    decision = evaluate_real_calls(
        production(production_confirm=""), operator_armed=True, providers=FAKE_REAL
    )
    assert decision.reasons == (BlockReason.PRODUCTION_NOT_CONFIRMED,)


def test_unknown_provider_is_blocked():
    decision = evaluate_real_calls(
        production(telephony_provider="nope"), operator_armed=True, providers=FAKE_REAL
    )
    assert decision.reasons == (BlockReason.PROVIDER_UNKNOWN,)


def test_ensure_raises_with_reasons(make_settings):
    with pytest.raises(RealCallsBlockedError) as exc:
        ensure_real_calls_allowed(make_settings())
    assert exc.value.code == "real_calls_blocked"
    assert "simulation_mode" in exc.value.details["blocked_by"]
