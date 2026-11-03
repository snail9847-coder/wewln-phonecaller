"""The `wewln` commands for Notion, campaigns, contacts and the do-not-call list.

Notion commands run against MockNotionServer (a local imitation of the API in a thread).
"""

import functools
import io

import pytest

from wewln_phonecaller import secret_store
from wewln_phonecaller.cli import main, read_contacts_csv
from wewln_phonecaller.notion.mock_server import DEFAULT_TOKEN, MockNotionServer

PHONE = "+994501234567"
OTHER = "+994551112233"


@pytest.fixture
def server():
    with MockNotionServer() as running:
        yield running


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("WEWLN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WEWLN_DEFAULT_PHONE_REGION", "AZ")
    return tmp_path


@pytest.fixture
def notion_env(env, server, monkeypatch):
    from wewln_phonecaller.notion import client

    # Real sleeps, but short ones: retries of a "down" mock should not slow the suite.
    fast = functools.partial(client.RetryPolicy, base_delay=0.01, max_delay=0.05)
    monkeypatch.setattr(client, "RetryPolicy", fast)
    monkeypatch.setenv("WEWLN_NOTION_API_BASE_URL", server.base_url)
    monkeypatch.setenv("WEWLN_NOTION_MAX_RPS", "100")
    monkeypatch.setenv("WEWLN_NOTION_TOKEN", server.mock.token)
    return env


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def bootstrapped(server, capsys):
    parent = server.mock.add_page("wewln")
    code, out, err = run(capsys, "notion", "bootstrap", "--parent", parent, "--write-env")
    assert code == 0, err
    return parent


# ---------------------------------------------------------------------- token


def test_token_set_status_delete(env, monkeypatch, capsys):
    token = "ntn_" + "t" * 46
    monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
    code, out, err = run(capsys, "notion", "token", "set", "--stdin")
    assert code == 0, err
    assert "saved in the OS credential store (memory)" in out
    assert secret_store.default_store().get(secret_store.NOTION_TOKEN_KEY) == token

    code, out, _ = run(capsys, "notion", "token", "status")
    assert code == 0
    assert "Notion token: set (credential store)" in out
    code, out, _ = run(capsys, "check-config")
    assert "notion_token: set (credential store)" in out
    assert token not in out

    code, out, _ = run(capsys, "notion", "token", "delete")
    assert out.strip() == "Notion token deleted."
    code, out, _ = run(capsys, "notion", "token", "delete")
    assert out.strip() == "No Notion token was stored."
    code, out, _ = run(capsys, "notion", "token", "status")
    assert "Notion token: not set" in out


def test_environment_token_takes_precedence(env, monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_NOTION_TOKEN", "ntn_" + "e" * 46)
    monkeypatch.setattr("sys.stdin", io.StringIO("ntn_" + "s" * 46 + "\n"))
    code, out, _ = run(capsys, "notion", "token", "set", "--stdin")
    assert code == 0
    assert "WEWLN_NOTION_TOKEN is set in the environment and takes precedence" in out
    code, out, _ = run(capsys, "notion", "token", "status")
    assert "set (environment variable)" in out


@pytest.mark.parametrize("value", ["short", "ntn_with space " + "x" * 40])
def test_malformed_token_is_not_saved_or_echoed(env, monkeypatch, capsys, value):
    monkeypatch.setattr("sys.stdin", io.StringIO(value + "\n"))
    code, out, err = run(capsys, "notion", "token", "set", "--stdin")
    assert code == 2
    assert "The token was not saved" in err
    assert value.strip() not in out + err
    assert secret_store.default_store().get(secret_store.NOTION_TOKEN_KEY) is None


def test_unavailable_credential_store(env, monkeypatch, capsys):
    secret_store.set_default_store(secret_store._UnavailableStore("no backend"))
    monkeypatch.setattr("sys.stdin", io.StringIO("ntn_" + "u" * 46 + "\n"))
    code, _, err = run(capsys, "notion", "token", "set", "--stdin")
    assert code == 1
    assert "Set the WEWLN_NOTION_TOKEN environment variable instead" in err
    code, _, err = run(capsys, "notion", "token", "delete")
    assert code == 1
    assert "Credential store unavailable" in err


def test_notion_commands_need_a_token(env, capsys):
    for argv in (["notion", "check"], ["notion", "sync"], ["notion", "bootstrap", "--parent", "x"]):
        code, _, err = run(capsys, *argv)
        assert code == 2
        assert "Notion token is not set" in err


def test_enabled_notion_needs_databases_and_a_token(env, monkeypatch, capsys):
    monkeypatch.setenv("WEWLN_NOTION_ENABLED", "true")
    code, _, err = run(capsys, "check-config")
    assert code == 2
    assert "requires WEWLN_NOTION_CAMPAIGNS_DB" in err
    for name in ("CAMPAIGNS", "CONTACTS", "JOBS", "RESULTS"):
        monkeypatch.setenv(f"WEWLN_NOTION_{name}_DB", str(len(name)) * 32)
    code, _, err = run(capsys, "check-config")
    assert code == 2
    assert "wewln notion token set" in err


# ---------------------------------------------------------------------- notion against the mock


def test_bootstrap_check_sync_and_simulate(notion_env, server, capsys):
    mock = server.mock
    parent = mock.add_page("wewln")
    code, out, err = run(capsys, "notion", "bootstrap", "--parent", parent, "--write-env")
    assert code == 0, err
    assert out.count(": created") == 4
    env_text = (notion_env / ".env").read_text(encoding="utf-8")
    assert "WEWLN_NOTION_ENABLED=true" in env_text
    assert "WEWLN_NOTION_RESULTS_DB=" in env_text
    assert mock.count("POST", r"^/v1/databases$") == 4

    # Again: everything is found, nothing is created and the .env lines are not duplicated.
    code, out, _ = run(capsys, "notion", "bootstrap", "--parent", parent, "--write-env")
    assert code == 0
    assert out.count(": found") == 4
    assert mock.count("POST", r"^/v1/databases$") == 4
    env_text = (notion_env / ".env").read_text(encoding="utf-8")
    assert env_text.count("WEWLN_NOTION_JOBS_DB=") == 1

    code, out, err = run(capsys, "notion", "check")
    assert code == 0, err
    assert "Connected as «wewln mock connection» (bot)" in out
    assert out.count(" ok") >= 4
    assert "Notion is ready for the sync." in out

    campaign = mock.create_row(
        mock.data_source_by_title("Кампании (wewln)"), {"Название": "Тест", "Статус": "Запущена"}
    )
    contacts = mock.data_source_by_title("Контакты (wewln)")
    mock.create_row(contacts, {"Имя": "Анна", "Телефон": PHONE, "Кампании": [campaign]})
    mock.create_row(
        contacts, {"Имя": "Борис", "Телефон": OTHER, "Кампании": [campaign], "Не звонить": True}
    )

    code, out, err = run(capsys, "notion", "sync")
    assert code == 0, err
    assert out.startswith("Sync ok.")
    code, out, _ = run(capsys, "jobs", "list")
    assert "queued" in out and "blocked" in out
    assert PHONE not in out and "501234567" not in out
    code, out, _ = run(capsys, "jobs", "list", "--unmask")
    assert PHONE in out

    code, out, _ = run(capsys, "simulate")
    assert "Simulated 1 call(s); no real call was placed." in out
    assert "Results reach Notion on the next sync" in out
    code, out, _ = run(capsys, "notion", "sync")
    assert code == 0
    results = mock.rows(mock.data_source_by_title("Результаты звонков (wewln)"))
    assert len(results) == 1
    jobs_ds = mock.data_source_by_title("Задания на звонки (wewln)")
    jobs = [mock.simple(p) for p in mock.rows(jobs_ds)]
    assert sorted(j["Статус"] for j in jobs) == ["Выполнено", "Заблокировано"]

    # Repeated syncs create nothing new.
    pages_created = mock.count("POST", r"^/v1/pages$")
    for _ in range(2):
        code, _, _ = run(capsys, "notion", "sync")
        assert code == 0
    assert mock.count("POST", r"^/v1/pages$") == pages_created

    code, out, _ = run(capsys, "notion", "status", "--limit", "3")
    assert "Notion sync: enabled" in out
    assert out.count(" ok ") + out.count(" ok\n") >= 3

    log_text = (notion_env / "data" / "logs" / "wewln.log").read_text(encoding="utf-8")
    assert "notion_sync" in log_text
    for secret in (DEFAULT_TOKEN, PHONE, "501234567", OTHER, "551112233"):
        assert secret not in log_text


def test_check_reports_and_repairs_the_schema(notion_env, server, capsys):
    bootstrapped(server, capsys)
    mock = server.mock
    contacts = mock.data_sources[mock.data_source_by_title("Контакты (wewln)")]
    del contacts["properties"]["Причина запрета"]

    code, out, err = run(capsys, "notion", "check")
    assert code == 1
    assert "missing property «Причина запрета»" in out
    assert "try: wewln notion check --repair" in err
    code, out, _ = run(capsys, "notion", "sync")
    assert code == 1
    assert "Sync failed." in out

    code, out, err = run(capsys, "notion", "check", "--repair")
    assert code == 0, err
    assert "missing properties or options were added" in out
    assert "Причина запрета" in contacts["properties"]
    code, out, _ = run(capsys, "notion", "sync")
    assert code == 0


def test_sync_when_notion_is_down(notion_env, server, capsys):
    bootstrapped(server, capsys)
    server.mock.down = True
    code, out, _ = run(capsys, "notion", "sync")
    assert code == 1
    assert "Sync failed." in out
    assert "error:" in out
    code, out, _ = run(capsys, "notion", "status")
    assert "failed" in out and "notion_unavailable" in out


def test_check_with_a_wrong_token(notion_env, server, monkeypatch, capsys):
    bootstrapped(server, capsys)
    wrong = "ntn_" + "w" * 46
    monkeypatch.setenv("WEWLN_NOTION_TOKEN", wrong)
    code, out, err = run(capsys, "notion", "check")
    assert code == 1
    assert err.startswith("Error:")
    assert wrong not in out + err


def test_bootstrap_with_an_unshared_page(notion_env, server, capsys):
    parent = server.mock.add_page("private", shared=False)
    code, out, err = run(capsys, "notion", "bootstrap", "--parent", parent)
    assert code == 1
    assert "not shared with the connection" in err
    assert not (notion_env / ".env").exists()


def test_bootstrap_prints_the_env_lines(notion_env, server, capsys):
    parent = server.mock.add_page("wewln")
    link = f"https://www.notion.so/wewln-{parent.replace('-', '')}"
    code, out, _ = run(capsys, "notion", "bootstrap", "--parent", link)
    assert code == 0
    assert "Add these lines to your .env file" in out
    assert "  WEWLN_NOTION_CAMPAIGNS_DB=" in out
    assert not (notion_env / ".env").exists()


def test_mock_server_command_with_demo_data(env, monkeypatch, capsys):
    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("wewln_phonecaller.cli.time.sleep", stop)
    code, out, _ = run(capsys, "notion", "mock-server", "--port", "0", "--demo")
    assert code == 0
    assert "Mock Notion API on http://127.0.0.1:" in out
    assert f'$env:WEWLN_NOTION_TOKEN = "{DEFAULT_TOKEN}"' in out
    assert out.count("$env:WEWLN_NOTION_") == 7  # base URL, token, four databases, enabled
    assert '$env:WEWLN_NOTION_ENABLED = "true"' in out


# ---------------------------------------------------------------------- without Notion


def write_csv(path, text, encoding):
    path.write_bytes(text.encode(encoding))
    return path


def test_local_campaign_from_csv_without_notion(env, capsys):
    csv_file = write_csv(
        env / "contacts.csv",
        "Имя;Телефон;Не звонить\nАнна;+994 50 123 45 67;\nБорис;055 111 22 33;да\nВера;12345;\n",
        "cp1251",
    )
    code, out, _ = run(capsys, "campaign", "create", "Весна", "--script", "Здравствуйте!")
    assert code == 0
    assert "Campaign 1 created (draft)" in out
    code, out, _ = run(capsys, "contacts", "import", str(csv_file), "--campaign", "1")
    assert code == 0
    assert "2 new, 0 updated, 2 added to the campaign, 1 marked do-not-call, 1 rejected" in out
    assert "line 4:" in out

    code, out, _ = run(capsys, "campaign", "start", "1")
    assert "Campaign 1: running (2 job(s) planned)." in out
    code, out, _ = run(capsys, "jobs", "list", "--campaign", "1")
    assert "queued" in out and "blocked" in out
    assert "501234567" not in out

    code, out, _ = run(capsys, "simulate", "--campaign", "1")
    assert "Simulated 1 call(s)" in out
    code, out, _ = run(capsys, "campaign", "stop", "1", "--reason", "Готово")
    assert code == 0
    assert "Campaign 1 is stopped" in out
    code, out, _ = run(capsys, "campaign", "list")
    assert "stopped" in out and "Весна" in out
    code, out, err = run(capsys, "campaign", "start", "1")
    assert code == 1
    assert err.startswith("Error:")
    code, out, _ = run(capsys, "simulate")
    assert "Simulated 0 call(s)" in out


def test_contacts_import_needs_a_phone_column(env, capsys):
    csv_file = write_csv(env / "bad.csv", "name,email\nАнна,a@example.com\n", "utf-8")
    code, _, err = run(capsys, "contacts", "import", str(csv_file))
    assert code == 1
    assert "Error: no phone column" in err
    code, _, err = run(capsys, "contacts", "import", str(env / "missing.csv"))
    assert code == 1
    assert "Cannot read" in err


@pytest.mark.parametrize(
    ("text", "encoding"),
    [
        ("\ufeffname,phone,dnc\nАнна,+994501234567,yes\n", "utf-8"),
        ("Name\tPhone\nАнна\t+994501234567\n", "utf-8"),
        ("ФИО;Номер телефона;DNC\nАнна;+994501234567;1\n", "cp1251"),
    ],
)
def test_read_contacts_csv_formats(tmp_path, text, encoding):
    rows = read_contacts_csv(write_csv(tmp_path / "c.csv", text, encoding))
    assert len(rows) == 1
    assert rows[0].name == "Анна"
    assert rows[0].phone == "+994501234567"
    assert rows[0].dnc is ("dnc" in text.lower())


def test_do_not_call_commands(env, capsys):
    code, out, _ = run(capsys, "dnc", "add", PHONE, "--reason", "Просил не звонить")
    assert code == 0
    assert "added to the do-not-call list" in out
    assert PHONE not in out
    code, out, _ = run(capsys, "dnc", "add", "+994 50 123-45-67")
    assert "already on the do-not-call list" in out
    code, out, _ = run(capsys, "dnc", "list")
    assert "manual" in out and "Просил не звонить" in out
    assert PHONE not in out
    code, out, _ = run(capsys, "dnc", "list", "--unmask")
    assert PHONE in out
    code, out, _ = run(capsys, "dnc", "remove", PHONE)
    assert "1 entry removed." in out
    code, out, _ = run(capsys, "dnc", "list")
    assert "(nothing)" in out
    code, _, err = run(capsys, "dnc", "add", "12345")
    assert code == 1
    assert err.startswith("Error:")
