"""`wewln calls` and `wewln telephony` from the command line (mock provider, ARI simulator)."""

import asyncio
import io

import pytest

from wewln_phonecaller import secret_store
from wewln_phonecaller.cli import main
from wewln_phonecaller.config import PRODUCTION_CONFIRM_PHRASE

PHONE = "+994501234567"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEWLN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WEWLN_DEFAULT_PHONE_REGION", "AZ")
    return tmp_path


@pytest.fixture
def asterisk_env(env, ari_sim, monkeypatch):
    """Production settings for real calls through the simulated Asterisk."""
    for name, value in {
        "WEWLN_MODE": "production",
        "WEWLN_PRODUCTION_CONFIRM": PRODUCTION_CONFIRM_PHRASE,
        "WEWLN_TELEPHONY_PROVIDER": "asterisk",
        "WEWLN_ARI_URL": ari_sim.base_url,
        "WEWLN_ARI_PASSWORD": ari_sim.password,
        "WEWLN_CALL_REGIONS": "AZ",
    }.items():
        monkeypatch.setenv(name, value)
    return env


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


async def run_async(capsys, *argv):
    """Commands that dial run their own event loop: keep them off the simulator's loop."""
    code = await asyncio.to_thread(main, list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def campaign(env, capsys, *phones):
    csv_file = env / "contacts.csv"
    rows = "".join(f"Контакт {i},{p}\n" for i, p in enumerate(phones))
    csv_file.write_text("name,phone\n" + rows, encoding="utf-8")
    assert run(capsys, "campaign", "create", "Тест")[0] == 0
    assert run(capsys, "contacts", "import", str(csv_file), "--campaign", "1")[0] == 0
    assert run(capsys, "campaign", "start", "1")[0] == 0


def test_mock_calls_from_the_command_line(env, capsys):
    campaign(env, capsys, "+994501234560", "+994501234561")
    code, out, _ = run(capsys, "calls", "run", "--fast", "--scenario", "busy")
    assert code == 0
    assert "Calls placed: 2 (mock provider: simulated, no real call)." in out
    assert out.count("busy (Занято)") == 2
    code, out, _ = run(capsys, "calls", "list")
    assert code == 0
    assert "Занято" in out and "50123456" not in out
    code, out, _ = run(capsys, "jobs", "list")
    assert out.count("попытки исчерпаны") == 2  # a local campaign tries each number once


def test_mock_test_call_follows_the_policy(env, capsys):
    code, out, _ = run(capsys, "calls", "test", "--to", "050 123 45 67", "--fast")
    assert code == 0
    assert "simulated test call to +9" in out and PHONE not in out
    assert "completed" in out
    code, _, err = run(capsys, "calls", "test", "--to", "+78091234567", "--fast")
    assert code == 1
    assert "number_type_not_allowed" in err
    code, _, err = run(capsys, "calls", "test", "--to", "12345", "--fast")
    assert code == 2
    code, out, _ = run(capsys, "calls", "test", "--softphone", "--fast")
    assert code == 0
    assert "PJSIP/softphone" in out and "[simulated]" in out


def test_emergency_stop_from_the_command_line(env, capsys):
    code, out, _ = run(capsys, "calls", "stop-all", "--reason", "проверка")
    assert code == 0
    assert "Emergency stop engaged" in out
    assert "already engaged" in run(capsys, "calls", "stop-all")[1]
    code, _, err = run(capsys, "calls", "test", "--to", PHONE, "--fast")
    assert code == 1
    assert "emergency stop" in err
    assert "Emergency stop is ON" in run(capsys, "calls", "list")[1]
    code, out, _ = run(capsys, "calls", "resume")
    assert "Emergency stop released." in out
    assert run(capsys, "calls", "test", "--to", PHONE, "--fast")[0] == 0
    code, _, err = run(capsys, "calls", "cancel", "999")
    assert code == 1
    assert "not found" in err


def test_secrets_are_stored_without_echo(env, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("ari-secret-from-stdin\n"))
    code, out, _ = run(capsys, "telephony", "secret", "set", "ari-password", "--stdin")
    assert code == 0
    assert "ari-secret-from-stdin" not in out
    store = secret_store.default_store()
    assert store.get("ari_password") == "ari-secret-from-stdin"
    code, out, _ = run(capsys, "telephony", "secret", "status")
    assert "ari-password: credential store" in out
    assert "webhook-secret: not set" in out
    monkeypatch.setenv("WEWLN_TELEPHONY_PROVIDER", "asterisk")
    code, out, err = run(capsys, "check-config")
    assert code == 0, err
    assert "ari-secret-from-stdin" not in out + err
    code, out, _ = run(capsys, "telephony", "secret", "set", "webhook-secret", "--generate")
    assert code == 0
    assert len(store.get("webhook_secret")) >= 32
    assert store.get("webhook_secret") not in out
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    code, _, err = run(capsys, "telephony", "secret", "set", "webhook-secret", "--stdin")
    assert code == 2
    assert "short" not in err.replace("characters long", "")
    assert run(capsys, "telephony", "secret", "delete", "ari-password")[0] == 0
    code, _, err = run(capsys, "check-config")
    assert code == 2
    assert "requires the ARI password" in err


def test_telephony_check_with_the_mock(env, capsys):
    code, out, _ = run(capsys, "telephony", "check")
    assert code == 0
    assert "provider: mock" in out
    assert "real_calls_allowed: False" in out


async def test_telephony_check_against_asterisk(asterisk_env, ari_sim, capsys):
    code, out, err = await run_async(capsys, "telephony", "check")
    assert code == 0, err
    assert "asterisk_version: 22.5.2" in out
    assert "'state': 'online'" in out
    assert "Telephony check passed." in out
    assert ari_sim.password not in out + err
    ari_sim.password = "changed-on-the-server"
    code, out, err = await run_async(capsys, "telephony", "check")
    assert code == 1
    assert "rejected the user name or password" in out


async def test_real_calls_need_a_permit_and_campaigns_stay_blocked(asterisk_env, ari_sim, capsys):
    campaign(asterisk_env, capsys, PHONE)
    code, _, err = await run_async(capsys, "calls", "run")
    assert code == 1
    assert "not enabled in this version" in err and "operator_not_armed" in err
    code, _, err = await run_async(capsys, "calls", "test", "--to", PHONE)
    assert code == 1
    assert "wewln calls permit" in err
    assert ari_sim.requests == []  # blocked before connecting to Asterisk
    code, _, err = run(capsys, "calls", "permit", "--to", PHONE, "--confirm", "0000")
    assert code == 1
    assert "Not confirmed" in err
    code, out, _ = run(capsys, "calls", "permit", "--to", PHONE, "--confirm", "4567")
    assert code == 0
    assert "Permit 1 is valid" in out and "Everything else is ready" in out
    assert PHONE not in out
    code, out, err = await run_async(capsys, "calls", "test", "--to", PHONE)
    assert code == 0, err
    assert "REAL test call" in out
    assert "completed" in out and "[simulated]" not in out
    code, _, err = await run_async(capsys, "calls", "test", "--to", PHONE)
    assert code == 1  # the permit was for one call
    code, out, _ = run(capsys, "calls", "permits")
    assert "1/1" in out and PHONE not in out
    code, _, err = run(capsys, "calls", "permit", "--to", "+79161234567", "--confirm", "4567")
    assert code == 2
    assert "region_not_allowed" in err


async def test_scenario_option_is_only_for_the_mock(asterisk_env, ari_sim, capsys):
    code, _, err = await run_async(capsys, "calls", "test", "--softphone", "--scenario", "busy")
    assert code == 1
    assert "only with the mock provider" in err
