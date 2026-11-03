import pytest

from wewln_phonecaller import __version__
from wewln_phonecaller.cli import main

TOKEN = "z" * 40


def test_check_config_with_defaults(capsys):
    assert main(["check-config"]) == 0
    out = capsys.readouterr().out
    assert "Configuration is valid." in out
    assert "mode: simulation" in out
    assert "real_calls_allowed: False (blocked by: simulation_mode" in out


def test_check_config_rejects_unconfirmed_production(monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_MODE", "production")
    assert main(["check-config"]) == 2
    assert "requires WEWLN_PRODUCTION_CONFIRM" in capsys.readouterr().err


def test_check_config_never_prints_secrets(monkeypatch, capsys):
    notion_token = "secret_" + "q" * 40
    monkeypatch.setenv("WEWLN_API_TOKEN", TOKEN)
    monkeypatch.setenv("WEWLN_NOTION_ENABLED", "true")
    monkeypatch.setenv("WEWLN_NOTION_TOKEN", notion_token)
    assert main(["check-config"]) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert notion_token not in captured.out + captured.err
    assert "api_token: set" in captured.out
    assert "notion_token: set" in captured.out


def test_rejected_secret_is_not_echoed(monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_API_TOKEN", "short-secret-value")
    assert main(["check-config"]) == 2
    captured = capsys.readouterr()
    assert "short-secret-value" not in captured.out + captured.err
    assert "WEWLN_API_TOKEN" in captured.err


def test_unknown_variable_is_reported(monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_MODEE", "production")
    assert main(["check-config"]) == 0
    assert "unknown variable WEWLN_MODEE" in capsys.readouterr().err


def test_env_file_option(tmp_path, capsys):
    env = tmp_path / "custom.env"
    env.write_text("# комментарий\nWEWLN_TIMEZONE=Asia/Baku\n", encoding="utf-8")
    assert main(["--env-file", str(env), "check-config"]) == 0
    assert "timezone: Asia/Baku" in capsys.readouterr().out


def test_init_db_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_DATA_DIR", str(tmp_path / "d"))
    assert main(["init-db"]) == 0
    assert main(["init-db"]) == 0
    assert "(schema version 1)" in capsys.readouterr().out
    assert (tmp_path / "d" / "wewln.db").is_file()


def test_init_db_reports_storage_errors(tmp_path, monkeypatch, capsys):
    (tmp_path / "folder.db").mkdir()
    monkeypatch.setenv("WEWLN_DB_PATH", str(tmp_path / "folder.db"))
    assert main(["init-db"]) == 1
    assert "Database error" in capsys.readouterr().err


def test_serve_refuses_an_invalid_configuration(monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_HTTP_HOST", "0.0.0.0")
    assert main(["serve"]) == 2
    assert "set WEWLN_API_TOKEN" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert __version__ in capsys.readouterr().out
