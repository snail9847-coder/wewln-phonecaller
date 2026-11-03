import pytest

from wewln_phonecaller import secret_store
from wewln_phonecaller.config import load_settings
from wewln_phonecaller.errors import ConfigError
from wewln_phonecaller.notion.ids import compact_id, normalize_notion_id
from wewln_phonecaller.phones import normalize_phone

DB = "0123456789abcdef0123456789abcdef"
CANONICAL = "01234567-89ab-cdef-0123-456789abcdef"
TOKEN = "ntn_" + "t" * 40


def problems(exc):
    return " ".join(exc.value.problems)


@pytest.mark.parametrize(
    "value",
    [
        DB,
        CANONICAL,
        DB.upper(),
        f"https://www.notion.so/workspace/Contacts-{DB}?v=fedcba9876543210fedcba9876543210",
        f"https://app.notion.com/p/{DB}?v=fedcba9876543210fedcba9876543210",
        f"https://www.notion.so/ws/{DB}?v=ffffffffffffffffffffffffffffffff&p="
        "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee&pm=s",
        f"https://www.notion.so/Cafe-{DB}",
    ],
)
def test_notion_ids_and_links_are_normalized(value):
    assert normalize_notion_id(value) == CANONICAL
    assert compact_id(value) == DB


@pytest.mark.parametrize(
    "value", ["", "abc", "https://www.notion.so/ws/Title?v=" + "f" * 32, "1234-5678"]
)
def test_bad_notion_ids_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_notion_id(value)


def test_database_links_in_settings(make_settings):
    s = make_settings(notion_contacts_db=f"https://www.notion.so/ws/Contacts-{DB}?v={'f' * 32}")
    assert s.notion_contacts_db == CANONICAL
    with pytest.raises(ConfigError) as exc:
        make_settings(notion_jobs_db="not-a-link")
    assert "WEWLN_NOTION_JOBS_DB" in problems(exc)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://api.notion.com", True),
        ("https://api.notion.com/", True),
        ("http://127.0.0.1:8790", True),
        ("http://localhost:8790", True),
        ("http://api.notion.com", False),
        ("https://api.notion.com/v1", False),
        ("ftp://api.notion.com", False),
        ("https://user:pw@api.notion.com", False),
    ],
)
def test_notion_api_base_url(make_settings, url, ok):
    if ok:
        assert make_settings(notion_api_base_url=url).notion_api_base_url == url.rstrip("/")
    else:
        with pytest.raises(ConfigError) as exc:
            make_settings(notion_api_base_url=url)
        assert "WEWLN_NOTION_API_BASE_URL" in problems(exc)


@pytest.mark.parametrize("bad", ["ntn_ with space", "short", "ntn_" + "x" * 600, "a\nb" * 20])
def test_malformed_notion_token_is_rejected_without_echo(make_settings, bad):
    with pytest.raises(ConfigError) as exc:
        make_settings(notion_token=bad)
    assert "WEWLN_NOTION_TOKEN" in problems(exc)
    assert bad not in str(exc.value)


def test_token_from_credential_store(make_settings):
    store = secret_store.MemoryStore({secret_store.NOTION_TOKEN_KEY: TOKEN})
    s = load_settings(env_file=None, secret_store=store)
    assert s.notion_token.get_secret_value() == TOKEN
    assert s.notion_token_source == "credential store"
    assert s.redacted_summary()["notion_token"] == "set (credential store)"
    assert TOKEN in s.secret_values()


def test_environment_token_wins_over_credential_store(monkeypatch):
    other = "ntn_" + "e" * 40
    monkeypatch.setenv("WEWLN_NOTION_TOKEN", other)
    store = secret_store.MemoryStore({secret_store.NOTION_TOKEN_KEY: TOKEN})
    s = load_settings(env_file=None, secret_store=store)
    assert s.notion_token.get_secret_value() == other
    assert s.notion_token_source == "environment variable"


def test_env_file_token_source(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"WEWLN_NOTION_TOKEN={TOKEN}\n", encoding="utf-8")
    s = load_settings(env_file=env)
    assert s.notion_token_source == ".env file"


def test_default_store_is_used(make_settings):
    secret_store.default_store().set(secret_store.NOTION_TOKEN_KEY, TOKEN)
    assert make_settings().notion_token_source == "credential store"


def test_unavailable_store_is_ignored():
    class Broken:
        name = "broken"

        def get(self, key):
            raise secret_store.SecretStoreUnavailable("no backend")

    s = load_settings(env_file=None, secret_store=Broken())
    assert s.notion_token is None


def test_malformed_stored_token_is_reported_without_echo():
    bad = "ntn bad token with spaces"
    store = secret_store.MemoryStore({secret_store.NOTION_TOKEN_KEY: bad})
    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None, secret_store=store)
    assert "credential store" in problems(exc)
    assert bad not in str(exc.value)


def test_phone_region_setting(make_settings):
    assert make_settings(default_phone_region="az").default_phone_region == "AZ"
    with pytest.raises(ConfigError) as exc:
        make_settings(default_phone_region="Azerbaijan")
    assert "WEWLN_DEFAULT_PHONE_REGION" in problems(exc)


@pytest.mark.parametrize(
    ("raw", "region", "e164", "error"),
    [
        ("+994 50 123 45 67", None, "+994501234567", None),
        ("00994501234567", None, "+994501234567", None),
        ("050 123 45 67", "AZ", "+994501234567", None),
        ("0501234567", None, None, "needs_country_code"),
        ("+7 912 345-67-89", None, "+79123456789", None),
        ("+999", None, None, "invalid"),
        ("hello", "AZ", None, "invalid"),
        ("  ", None, None, "missing"),
        (None, None, None, "missing"),
    ],
)
def test_phone_normalization(raw, region, e164, error):
    result = normalize_phone(raw, region)
    assert (result.e164, result.error) == (e164, error)
    assert result.valid is (e164 is not None)


class FakeKeyringModule:
    """Mimics the parts of the `keyring` API that KeyringStore uses."""

    class PasswordDeleteError(Exception):
        pass

    class Backend:
        pass

    def __init__(self):
        self.data = {}

    def get_keyring(self):
        return self.Backend()

    def get_password(self, service, key):
        return self.data.get((service, key))

    def set_password(self, service, key, value):
        self.data[(service, key)] = value

    def delete_password(self, service, key):
        if (service, key) not in self.data:
            raise FakeKeyringModule.PasswordDeleteError("missing")
        del self.data[(service, key)]


def test_keyring_store_round_trip():
    module = FakeKeyringModule()
    store = secret_store.KeyringStore(module)
    assert store.get("notion_token") is None
    store.set("notion_token", TOKEN)
    assert module.data[(secret_store.SERVICE_NAME, "notion_token")] == TOKEN
    assert store.get("notion_token") == TOKEN
    assert store.delete("notion_token") is True
    assert store.delete("notion_token") is False


def test_keyring_errors_become_unavailable():
    module = FakeKeyringModule()

    def boom(*_args):
        raise RuntimeError("backend exploded")

    module.get_password = boom
    store = secret_store.KeyringStore(module)
    with pytest.raises(secret_store.SecretStoreUnavailable):
        store.get("notion_token")
