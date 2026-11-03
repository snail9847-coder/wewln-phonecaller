import pytest

from wewln_phonecaller.config import (
    PRODUCTION_CONFIRM_PHRASE,
    find_unknown_variables,
    load_settings,
)
from wewln_phonecaller.errors import ConfigError

TOKEN = "t" * 40


def problems(exc_info) -> str:
    return "\n".join(exc_info.value.problems)


def test_defaults_are_safe(make_settings):
    s = make_settings()
    assert s.mode == "simulation"
    assert s.telephony_provider == "mock"
    assert s.kill_switch is False
    assert s.http_host == "127.0.0.1"
    assert s.is_loopback
    assert s.api_token is None
    assert s.default_language == "ru-RU"
    assert s.notion_enabled is False


def test_environment_variables_are_read(monkeypatch, tmp_path):
    monkeypatch.setenv("WEWLN_HTTP_PORT", "9000")
    monkeypatch.setenv("WEWLN_LOG_FORMAT", "text")
    monkeypatch.setenv("WEWLN_DATA_DIR", str(tmp_path / "d"))
    s = load_settings(env_file=None)
    assert s.http_port == 9000
    assert s.log_format == "text"
    assert s.data_dir == tmp_path / "d"


def test_env_file_is_utf8_and_environment_wins(monkeypatch, tmp_path):
    env = tmp_path / "custom.env"
    env.write_text(
        "# комментарий по-русски\nWEWLN_TIMEZONE=Asia/Baku\nWEWLN_HTTP_PORT=8899\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEWLN_HTTP_PORT", "8900")
    s = load_settings(env_file=env)
    assert s.timezone == "Asia/Baku"
    assert s.http_port == 8900


def test_production_requires_confirmation(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(mode="production")
    assert "WEWLN_PRODUCTION_CONFIRM" in problems(exc)


def test_production_rejects_wrong_phrase(make_settings):
    with pytest.raises(ConfigError):
        make_settings(mode="production", production_confirm="yes")


def test_production_with_confirmation_loads(make_settings):
    s = make_settings(mode="production", production_confirm=PRODUCTION_CONFIRM_PHRASE)
    assert s.mode == "production"
    assert s.redacted_summary()["production_confirmed"] is True


@pytest.mark.parametrize("provider", ["twilio", "sip", ""])
def test_unknown_telephony_provider_rejected(make_settings, provider):
    with pytest.raises(ConfigError) as exc:
        make_settings(telephony_provider=provider)
    assert "WEWLN_TELEPHONY_PROVIDER" in problems(exc)


@pytest.mark.parametrize("port", [0, 70000])
def test_invalid_port_rejected(make_settings, port):
    with pytest.raises(ConfigError) as exc:
        make_settings(http_port=port)
    assert "WEWLN_HTTP_PORT" in problems(exc)


def test_invalid_host_rejected(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(http_host="example.com")
    assert "WEWLN_HTTP_HOST" in problems(exc)


def test_non_loopback_host_requires_token(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(http_host="0.0.0.0")
    assert "WEWLN_API_TOKEN" in problems(exc)


def test_non_loopback_host_with_token(make_settings):
    s = make_settings(http_host="0.0.0.0", api_token=TOKEN)
    assert not s.is_loopback


def test_short_token_rejected_without_leaking_it(make_settings):
    short = "short-secret-123"
    with pytest.raises(ConfigError) as exc:
        make_settings(api_token=short)
    assert "WEWLN_API_TOKEN" in problems(exc)
    assert short not in str(exc.value)
    assert short not in problems(exc)


def test_blank_token_means_unset(make_settings):
    assert make_settings(api_token="  ").api_token is None


NOTION_DBS = {
    "notion_campaigns_db": "11111111111111111111111111111111",
    "notion_contacts_db": "22222222222222222222222222222222",
    "notion_jobs_db": "33333333333333333333333333333333",
    "notion_results_db": "44444444444444444444444444444444",
}


def test_notion_enabled_requires_databases(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(notion_enabled=True, notion_token="ntn_" + "x" * 40)
    for env in ("CAMPAIGNS", "CONTACTS", "JOBS", "RESULTS"):
        assert f"WEWLN_NOTION_{env}_DB" in problems(exc)


def test_notion_enabled_requires_token(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(notion_enabled=True, **NOTION_DBS)
    assert "WEWLN_NOTION_TOKEN" in problems(exc)
    assert "wewln notion token set" in problems(exc)


def test_notion_enabled_with_token(make_settings):
    s = make_settings(notion_enabled=True, notion_token="ntn_" + "x" * 40, **NOTION_DBS)
    assert s.redacted_summary()["notion_token"] == "set (argument)"
    assert s.notion_campaigns_db == "11111111-1111-1111-1111-111111111111"


def test_unsupported_language_rejected(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(default_language="en-US")
    assert "WEWLN_DEFAULT_LANGUAGE" in problems(exc)


@pytest.mark.parametrize("tz", ["Europe/Moscow", "Asia/Baku", "UTC"])
def test_time_zones_resolve(make_settings, tz):
    # On Windows this only works when the tzdata package is installed.
    assert make_settings(timezone=tz).timezone == tz


def test_invalid_time_zone_rejected(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(timezone="Mars/Olympus")
    assert "WEWLN_TIMEZONE" in problems(exc)


def test_several_problems_are_reported_together(make_settings):
    with pytest.raises(ConfigError) as exc:
        make_settings(mode="production", http_host="0.0.0.0", notion_enabled=True)
    text = problems(exc)
    for name in ("WEWLN_PRODUCTION_CONFIRM", "WEWLN_API_TOKEN", "WEWLN_NOTION_CAMPAIGNS_DB"):
        assert name in text


def test_secrets_never_appear_in_repr_dump_or_summary(make_settings):
    notion_secret = "ntn_" + "y" * 40
    s = make_settings(api_token=TOKEN, notion_token=notion_secret)
    for text in (repr(s), str(s), s.model_dump_json(), str(s.redacted_summary())):
        assert TOKEN not in text
        assert notion_secret not in text
    assert s.redacted_summary()["api_token"] == "set"
    assert sorted(s.secret_values()) == sorted([TOKEN, notion_secret])


def test_resolved_paths(make_settings, tmp_path):
    s = make_settings()
    assert s.resolved_db_path == tmp_path / "data" / "wewln.db"
    assert s.resolved_log_file == tmp_path / "data" / "logs" / "wewln.log"
    assert make_settings(log_to_file=False).resolved_log_file is None


def test_unknown_variables_are_reported(tmp_path):
    env = tmp_path / ".env"
    env.write_text("WEWLN_MDOE=production\nWEWLN_MODE=simulation\nOTHER=1\n", encoding="utf-8")
    unknown = find_unknown_variables(environ={"WEWLN_KILLSWITCH": "1", "PATH": "x"}, env_file=env)
    assert unknown == ["WEWLN_KILLSWITCH", "WEWLN_MDOE"]
