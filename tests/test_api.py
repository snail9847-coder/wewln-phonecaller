import logging

import pytest

from wewln_phonecaller.api import create_app
from wewln_phonecaller.api.app import SETTINGS_KEY
from wewln_phonecaller.errors import RealCallsBlockedError
from wewln_phonecaller.storage import Database

TOKEN = "x" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOTION_DBS = {
    f"notion_{name}_db": str(i) * 32
    for i, name in enumerate(("campaigns", "contacts", "jobs", "results"), start=1)
}


@pytest.fixture
def build_client(aiohttp_client, make_settings):
    async def factory(*, migrate=True, routes=(), **overrides):
        settings = make_settings(**overrides)
        db = Database(settings.resolved_db_path)
        if migrate:
            db.migrate()
        app = create_app(settings, db)
        for path, handler in routes:
            app.router.add_get(path, handler)
        return await aiohttp_client(app)

    return factory


async def test_health(build_client):
    client = await build_client()
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "simulation"
    assert len(resp.headers["X-Request-ID"]) == 32


async def test_request_id_is_echoed_only_when_valid(build_client):
    client = await build_client()
    resp = await client.get("/health", headers={"X-Request-ID": "call-42.retry_1"})
    assert resp.headers["X-Request-ID"] == "call-42.retry_1"
    resp = await client.get("/health", headers={"X-Request-ID": "not valid!"})
    assert resp.headers["X-Request-ID"] != "not valid!"
    assert len(resp.headers["X-Request-ID"]) == 32


async def test_ready_in_simulation(build_client):
    client = await build_client()
    resp = await client.get("/ready")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ready"
    checks = body["checks"]
    assert checks["database"]["status"] == "ok"
    assert checks["data_dir"]["status"] == "ok"
    assert checks["notion"] == {"status": "disabled"}
    assert checks["telephony"] == {"status": "ok", "provider": "mock", "places_real_calls": False}
    assert checks["real_calls"]["allowed"] is False
    assert "simulation_mode" in checks["real_calls"]["blocked_by"]


async def test_ready_is_503_without_database_and_does_not_create_it(build_client):
    client = await build_client(migrate=False)
    resp = await client.get("/ready")
    assert resp.status == 503
    body = await resp.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["error"] == "database_missing"
    assert not client.app[SETTINGS_KEY].resolved_db_path.exists()


async def test_ready_reports_configured_notion_without_the_token(build_client):
    notion_token = "secret_" + "n" * 40
    client = await build_client(notion_enabled=True, notion_token=notion_token, **NOTION_DBS)
    resp = await client.get("/ready")
    assert resp.status == 200
    assert (await resp.json())["checks"]["notion"]["status"] == "configured"
    assert notion_token not in await resp.text()


async def test_protected_endpoint_is_closed_without_configured_token(build_client):
    client = await build_client()
    resp = await client.get("/api/v1/info", headers=AUTH)
    assert resp.status == 401
    assert (await resp.json())["error"]["code"] == "auth_not_configured"
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "header", [None, "Bearer wrong", f"Basic {TOKEN}", f"Bearer {TOKEN}x", f"Bearer{TOKEN}"]
)
async def test_wrong_credentials_are_rejected(build_client, header):
    client = await build_client(api_token=TOKEN)
    resp = await client.get("/api/v1/info", headers={"Authorization": header} if header else {})
    assert resp.status == 401
    assert (await resp.json())["error"]["code"] == "unauthorized"


async def test_info_with_token_contains_no_secrets(build_client):
    notion_token = "secret_" + "n" * 40
    client = await build_client(
        api_token=TOKEN, notion_enabled=True, notion_token=notion_token, **NOTION_DBS
    )
    resp = await client.get("/api/v1/info", headers=AUTH)
    assert resp.status == 200
    text = await resp.text()
    assert TOKEN not in text
    assert notion_token not in text
    body = await resp.json()
    assert body["settings"]["api_token"] == "set"
    assert body["settings"]["notion_token"] == "set (argument)"
    assert body["real_calls"]["allowed"] is False


async def test_unknown_route_is_json_404(build_client):
    client = await build_client(api_token=TOKEN)
    resp = await client.get("/nope", headers=AUTH)
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "not_found"


async def test_wrong_method_is_json_405(build_client):
    client = await build_client()
    resp = await client.post("/health")
    assert resp.status == 405
    assert (await resp.json())["error"]["code"] == "method_not_allowed"


async def test_application_errors_keep_their_code(build_client):
    async def blocked(_request):
        raise RealCallsBlockedError("Real calls are blocked")

    client = await build_client(api_token=TOKEN, routes=[("/api/v1/blocked", blocked)])
    resp = await client.get("/api/v1/blocked", headers=AUTH)
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "real_calls_blocked"


async def test_unhandled_error_is_generic_and_logged(build_client, caplog):
    async def boom(_request):
        raise RuntimeError("internal detail")

    client = await build_client(api_token=TOKEN, routes=[("/api/v1/boom", boom)])
    with caplog.at_level(logging.INFO, logger="wewln.api"):
        resp = await client.get("/api/v1/boom", headers=AUTH)
    assert resp.status == 500
    body = await resp.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"] == resp.headers["X-Request-ID"]
    assert "internal detail" not in await resp.text()
    messages = [record.getMessage() for record in caplog.records]
    assert "unhandled_error" in messages
    assert "http_request" in messages


async def wait_until(condition, seconds=10.0):
    import asyncio

    for _ in range(int(seconds / 0.02)):
        if condition():
            return True
        await asyncio.sleep(0.02)
    return False


async def notion_app(aiohttp_client, world, make_settings, **overrides):
    from wewln_phonecaller.notion.sync import NotionSync, SyncService

    settings = make_settings(**{**world.overrides, **overrides})
    sync = NotionSync(settings, world.repo, client_factory=world.client)
    service = SyncService(sync, interval=3600)
    client = await aiohttp_client(create_app(settings, world.repo.db, sync_service=service))
    assert await wait_until(lambda: sync.last_result is not None)
    return client, service


async def test_ready_stays_200_while_notion_is_down(aiohttp_client, notion_world, make_settings):
    notion_world.mock.down = True
    client, _service = await notion_app(aiohttp_client, notion_world, make_settings)
    resp = await client.get("/ready")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ready"
    notion = body["checks"]["notion"]
    assert notion["status"] == "degraded"
    assert notion["last_run"]["error_code"] == "notion_unavailable"
    assert notion["consecutive_failures"] == 1


async def test_notion_and_campaign_endpoints(aiohttp_client, notion_world, make_settings):
    w = notion_world
    camp = w.campaign()
    w.contact("Анна", "+994501234567", [camp])
    client, service = await notion_app(aiohttp_client, w, make_settings, api_token=TOKEN)
    assert service.status()["status"] == "ok"

    resp = await client.post("/api/v1/notion/sync", headers=AUTH)
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"

    resp = await client.get("/api/v1/campaigns", headers=AUTH)
    (campaign,) = (await resp.json())["campaigns"]
    assert campaign["status"] == "running"

    resp = await client.post(
        f"/api/v1/campaigns/{campaign['id']}/stop", json={"reason": "Тест"}, headers=AUTH
    )
    assert resp.status == 200
    assert (await resp.json())["campaign"]["status"] == "stopped"
    # The stop wakes the background sync, which tells Notion.
    assert await wait_until(lambda: w.row(camp)["Статус"] == "Остановлена")

    resp = await client.post("/api/v1/dnc", json={"phone": "+994551112233"}, headers=AUTH)
    body = await resp.json()
    assert body["added"] is True
    assert "551112233" not in body["phone"]

    # Let the background cycle woken by stop/DNC finish before reading the runs.
    assert await wait_until(lambda: not service.status()["running"])
    resp = await client.get("/api/v1/notion/status", headers=AUTH)
    body = await resp.json()
    assert body["enabled"] is True
    statuses = [run["status"] for run in body["recent_runs"]]
    assert statuses
    assert set(statuses) <= {"ok", "partial", "running"}
    assert "ok" in statuses


async def test_endpoint_input_errors(aiohttp_client, notion_world, make_settings):
    client, _service = await notion_app(
        aiohttp_client, notion_world, make_settings, api_token=TOKEN
    )
    resp = await client.post(
        "/api/v1/campaigns/999/stop",
        data="{not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "invalid_json"
    resp = await client.post("/api/v1/campaigns/999/stop", headers=AUTH)
    assert resp.status == 404
    resp = await client.post("/api/v1/dnc", json={"phone": "12"}, headers=AUTH)
    assert resp.status == 400
    resp = await client.post("/api/v1/dnc", json={}, headers=AUTH)
    assert (await resp.json())["error"]["code"] == "invalid_input"


async def test_sync_endpoint_without_notion(build_client):
    client = await build_client(api_token=TOKEN)
    resp = await client.post("/api/v1/notion/sync", headers=AUTH)
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "notion_disabled"
    resp = await client.get("/api/v1/notion/status", headers=AUTH)
    assert (await resp.json())["enabled"] is False
