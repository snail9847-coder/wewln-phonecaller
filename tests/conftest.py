import os

import pytest

from wewln_phonecaller import secret_store
from wewln_phonecaller.config import load_settings
from wewln_phonecaller.logging_setup import shutdown_logging


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """No WEWLN_* variables from the machine and no stray .env from the checkout leak into tests."""
    for key in list(os.environ):
        if key.upper().startswith("WEWLN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    # Never touch the real OS credential store (Windows Credential Manager) from tests.
    store = secret_store.MemoryStore()
    secret_store.set_default_store(store)
    yield
    secret_store.set_default_store(None)
    shutdown_logging()  # release log files; Windows cannot delete open files


@pytest.fixture
def make_settings(tmp_path):
    def factory(**overrides):
        overrides.setdefault("data_dir", tmp_path / "data")
        return load_settings(env_file=None, **overrides)

    return factory


@pytest.fixture
async def notion_mock(aiohttp_server):
    """A MockNotion served on 127.0.0.1; `mock.base_url` is its address."""
    from wewln_phonecaller.notion.mock_server import MockNotion

    mock = MockNotion()
    server = await aiohttp_server(mock.app())
    mock.base_url = str(server.make_url("")).rstrip("/")
    return mock


@pytest.fixture
def sleeps():
    """Recorded (not real) sleeps for clients under test."""
    return []


@pytest.fixture
def notion_client_factory(sleeps):
    from wewln_phonecaller.notion.client import NotionClient

    clients = []

    def factory(mock, **kwargs):
        async def fake_sleep(delay):
            sleeps.append(delay)

        kwargs.setdefault("max_rps", 1000)
        kwargs.setdefault("sleep", fake_sleep)
        client = NotionClient(kwargs.pop("token", mock.token), base_url=mock.base_url, **kwargs)
        clients.append(client)
        return client

    yield factory


class NotionWorld:
    """A bootstrapped MockNotion, settings pointing at it, a local database and a sync."""

    def __init__(self, mock, workspace, make_settings, client_factory, tmp_path):
        from wewln_phonecaller.notion.schema import DATABASES

        self.mock = mock
        self.workspace = workspace
        self.ds = {key: db.data_source_id for key, db in workspace.databases.items()}
        self.tmp_path = tmp_path
        self._client_factory = client_factory
        self.overrides = {
            "notion_enabled": True,
            "notion_token": mock.token,
            "notion_api_base_url": mock.base_url,
            "default_phone_region": "AZ",
            **{spec.setting: workspace[spec.key].database_id for spec in DATABASES},
        }
        self.settings = make_settings(**self.overrides)
        self.repo = self.new_repo(self.settings.resolved_db_path)
        self.sync = self.new_sync(self.repo)

    def client(self, **kwargs):
        return self._client_factory(self.mock, **kwargs)

    def new_repo(self, path):
        from wewln_phonecaller.storage import Database
        from wewln_phonecaller.storage.repository import Repository

        db = Database(path)
        db.migrate()
        return Repository(db, default_region="AZ")

    def new_sync(self, repo=None, **kwargs):
        from wewln_phonecaller.notion.sync import NotionSync

        return NotionSync(
            self.settings, repo or self.repo, client_factory=lambda: self.client(), **kwargs
        )

    async def run(self, sync=None):
        return await (sync or self.sync).run_once()

    def campaign(self, name="Обзвон", status="Запущена", **values):
        return self.mock.create_row(
            self.ds["campaigns"], {"Название": name, "Статус": status, **values}
        )

    def contact(self, name, phone, campaigns=(), *, dnc=False, reason=""):
        values = {"Имя": name, "Телефон": phone, "Кампании": list(campaigns)}
        if dnc:
            values["Не звонить"] = True
        if reason:
            values["Причина запрета"] = reason
        return self.mock.create_row(self.ds["contacts"], values)

    def pages(self, key):
        return self.mock.rows(self.ds[key])

    def rows(self, key):
        return [self.mock.simple(page) for page in self.pages(key)]

    def row(self, page_id):
        return self.mock.simple(self.mock.pages[page_id])

    def writes(self):
        return self.mock.count("POST", r"^/v1/pages$") + self.mock.count("PATCH", r"^/v1/pages/")


@pytest.fixture
async def notion_world(notion_mock, notion_client_factory, make_settings, tmp_path):
    from wewln_phonecaller.notion.setup import bootstrap

    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)
    world = NotionWorld(notion_mock, workspace, make_settings, notion_client_factory, tmp_path)
    world.parent = parent
    return world


@pytest.fixture
async def ari_sim(aiohttp_server):
    """An AriSimulator (imitation of Asterisk ARI) on 127.0.0.1; `sim.base_url` is its address."""
    from ari_simulator import AriSimulator

    sim = AriSimulator()
    server = await aiohttp_server(sim.app())
    sim.base_url = str(server.make_url("")).rstrip("/")
    yield sim
    await sim.close()
