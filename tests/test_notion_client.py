import logging

import pytest

from wewln_phonecaller.notion.client import (
    NOTION_VERSION,
    NotionAccessError,
    NotionAuthError,
    NotionClient,
    NotionRateLimitedError,
    NotionUnavailableError,
    NotionUncertainWriteError,
    NotionValidationError,
    RetryPolicy,
)
from wewln_phonecaller.notion.mock_server import MockNotion, stepping_clock


async def make_data_source(client, mock, extra=None):
    parent = mock.add_page("Корень")
    properties = {"Имя": {"title": {}}, "Ключ": {"rich_text": {}}, **(extra or {})}
    db = await client.create_database(
        {
            "parent": {"type": "page_id", "page_id": parent},
            "title": [{"type": "text", "text": {"content": "Тест"}}],
            "initial_data_source": {"properties": properties},
        }
    )
    return db["data_sources"][0]["id"]


async def test_version_header_and_token_are_sent(notion_mock, notion_client_factory):
    async with notion_client_factory(notion_mock) as client:
        me = await client.get_self()
    assert me["type"] == "bot"
    assert NOTION_VERSION == "2026-03-11"


async def test_wrong_token_is_an_auth_error_without_the_token(
    notion_mock, notion_client_factory, caplog
):
    caplog.set_level(logging.DEBUG)
    wrong = "ntn_wrong_" + "w" * 30
    async with notion_client_factory(notion_mock, token=wrong) as client:
        with pytest.raises(NotionAuthError) as exc:
            await client.get_self()
    assert exc.value.status == 401
    assert exc.value.notion_code == "unauthorized"
    assert wrong not in str(exc.value)
    assert wrong not in caplog.text
    assert notion_mock.count("GET", "/v1/users/me") == 1  # never retried


async def test_unshared_object_is_an_access_error(notion_mock, notion_client_factory):
    page = notion_mock.add_page("Закрыто", shared=False)
    async with notion_client_factory(notion_mock) as client:
        with pytest.raises(NotionAccessError) as exc:
            await client.retrieve_page(page)
    assert exc.value.notion_code == "object_not_found"


async def test_rate_limit_waits_for_retry_after(notion_mock, notion_client_factory, sleeps):
    notion_mock.fail("GET", "/v1/users/me", 429, "rate_limited", times=2, retry_after=3)
    async with notion_client_factory(notion_mock) as client:
        await client.get_self()
    assert notion_mock.count("GET", "/v1/users/me") == 3
    assert len([s for s in sleeps if s >= 3]) == 2


async def test_blocked_access_is_not_retried(notion_mock, notion_client_factory):
    notion_mock.fail(
        "GET",
        "/v1/users/me",
        429,
        "rate_limited",
        retry_after=1,
        rate_limit_reason="public_api_request_blocked",
    )
    async with notion_client_factory(notion_mock) as client:
        with pytest.raises(NotionRateLimitedError):
            await client.get_self()
    assert notion_mock.count("GET", "/v1/users/me") == 1


async def test_long_retry_after_is_left_to_the_caller(notion_mock, notion_client_factory):
    notion_mock.fail("GET", "/v1/users/me", 429, "rate_limited", retry_after=120)
    async with notion_client_factory(notion_mock) as client:
        with pytest.raises(NotionRateLimitedError) as exc:
            await client.get_self()
    assert exc.value.retry_after == 120


async def test_overload_and_server_errors_are_retried_for_reads(notion_mock, notion_client_factory):
    notion_mock.fail("GET", "/v1/users/me", 529, "service_overload", retry_after=1)
    notion_mock.fail("GET", "/v1/users/me", 500, "internal_server_error")
    notion_mock.fail("GET", "/v1/users/me", 502, "bad_gateway")
    async with notion_client_factory(notion_mock) as client:
        await client.get_self()
    assert notion_mock.count("GET", "/v1/users/me") == 4


async def test_reads_give_up_after_the_retry_limit(notion_mock, notion_client_factory):
    notion_mock.fail("GET", "/v1/users/me", 503, "service_unavailable", times=10)
    client = notion_client_factory(notion_mock, retry=RetryPolicy(max_attempts=3))
    async with client:
        with pytest.raises(NotionUnavailableError):
            await client.get_self()
    assert notion_mock.count("GET", "/v1/users/me") == 3


async def test_failed_create_is_never_repeated(notion_mock, notion_client_factory):
    async with notion_client_factory(notion_mock) as client:
        ds = await make_data_source(client, notion_mock)
        notion_mock.fail("POST", "^/v1/pages$", 500, "internal_server_error")
        with pytest.raises(NotionUncertainWriteError) as exc:
            await client.create_page({"parent": {"data_source_id": ds}, "properties": {}})
    assert exc.value.committed_resource_id is None
    assert notion_mock.count("POST", "^/v1/pages$") == 1


async def test_saved_write_answered_with_503_reports_the_new_id(notion_mock, notion_client_factory):
    async with notion_client_factory(notion_mock) as client:
        ds = await make_data_source(client, notion_mock)
        notion_mock.fail(
            "POST",
            "^/v1/pages$",
            503,
            "service_unavailable",
            apply_first=True,
            committed_id=True,
        )
        with pytest.raises(NotionUncertainWriteError) as exc:
            await client.create_page({"parent": {"data_source_id": ds}, "properties": {}})
        created = exc.value.committed_resource_id
        assert created is not None
        page = await client.retrieve_page(created)
    assert page["id"] == created
    assert len(notion_mock.rows(ds)) == 1


async def test_conflicts_are_retried(notion_mock, notion_client_factory):
    async with notion_client_factory(notion_mock) as client:
        ds = await make_data_source(client, notion_mock)
        page = await client.create_page({"parent": {"data_source_id": ds}, "properties": {}})
        notion_mock.fail("PATCH", "/v1/pages/", 409, "conflict_error")
        await client.update_page(page["id"], {"properties": {"Ключ": {"rich_text": []}}})
    assert notion_mock.count("PATCH", "/v1/pages/") == 2


async def test_validation_errors_are_reported_with_masked_values(
    notion_mock, notion_client_factory
):
    async with notion_client_factory(notion_mock) as client:
        ds = await make_data_source(client, notion_mock)
        with pytest.raises(NotionValidationError) as exc:
            await client.create_page(
                {"parent": {"data_source_id": ds}, "properties": {"Нет такого": {"title": []}}}
            )
    assert exc.value.status == 400


async def test_pagination_reads_every_page(notion_mock, notion_client_factory):
    async with notion_client_factory(notion_mock) as client:
        ds = await make_data_source(client, notion_mock)
        for i in range(250):
            notion_mock.create_row(ds, {"Имя": f"r{i}"})
        before = notion_mock.count("POST", "/query")
        result = await client.query_all(ds)
    assert result.complete
    assert len(result.pages) == 250
    assert notion_mock.count("POST", "/query") - before == 3


async def test_created_time_windows_get_past_the_query_cap(aiohttp_server, notion_client_factory):
    mock = MockNotion(query_result_limit=40, clock=stepping_clock())
    server = await aiohttp_server(mock.app())
    mock.base_url = str(server.make_url("")).rstrip("/")
    async with notion_client_factory(mock) as client:
        ds = await make_data_source(client, mock)
        for i in range(130):
            mock.create_row(ds, {"Имя": f"r{i}"})
        result = await client.query_all(ds)
        filtered = await client.query_all(
            ds, filter={"property": "Имя", "title": {"starts_with": "r1"}}
        )
    assert result.complete
    assert sorted(p["id"] for p in result.pages) == sorted(p["id"] for p in mock.rows(ds))
    assert filtered.complete
    expected = [p for p in mock.rows(ds) if mock.simple(p)["Имя"].startswith("r1")]
    assert len(filtered.pages) == len(expected) == 41


async def test_cap_that_cannot_be_split_is_reported_incomplete(
    aiohttp_server, notion_client_factory
):
    mock = MockNotion(query_result_limit=10)  # real clock: every row in the same minute
    server = await aiohttp_server(mock.app())
    mock.base_url = str(server.make_url("")).rstrip("/")
    async with notion_client_factory(mock) as client:
        ds = await make_data_source(client, mock)
        for i in range(25):
            mock.create_row(ds, {"Имя": f"r{i}"})
        result = await client.query_all(ds)
    assert result.complete is False
    assert len(result.pages) == 10


async def test_relation_with_more_than_25_references(notion_mock, notion_client_factory):
    async with notion_client_factory(notion_mock) as client:
        target = await make_data_source(client, notion_mock)
        refs = [notion_mock.create_row(target, {"Имя": f"t{i}"}) for i in range(60)]
        source = await make_data_source(
            client,
            notion_mock,
            {"Связи": {"relation": {"data_source_id": target, "single_property": {}}}},
        )
        page_id = notion_mock.create_row(source, {"Имя": "x", "Связи": refs})
        page = await client.retrieve_page(page_id)
        prop = page["properties"]["Связи"]
        assert prop["has_more"] is True and len(prop["relation"]) == 25
        items = await client.property_items(page_id, prop["id"])
    assert [i["relation"]["id"] for i in items] == refs


async def test_unreachable_api_is_unavailable(notion_client_factory, sleeps):
    class Nowhere:
        token = "ntn_" + "x" * 30
        base_url = "http://127.0.0.1:9"  # discard port: nothing listens there

    client = notion_client_factory(Nowhere(), retry=RetryPolicy(max_attempts=2))
    async with client:
        with pytest.raises(NotionUnavailableError):
            await client.get_self()
    assert len([s for s in sleeps if s >= 0.1]) == 1  # one backoff between two attempts


async def test_logs_contain_neither_token_nor_bodies(notion_mock, notion_client_factory, caplog):
    caplog.set_level(logging.DEBUG, logger="wewln.notion")
    async with notion_client_factory(notion_mock) as client:
        ds = await make_data_source(client, notion_mock)
        await client.create_page(
            {
                "parent": {"data_source_id": ds},
                "properties": {"Имя": {"title": [{"text": {"content": "Секретный Иван"}}]}},
            }
        )
    assert notion_mock.token not in caplog.text
    assert "Секретный Иван" not in caplog.text
    assert "notion_request" in caplog.text


def test_client_requires_a_token(make_settings):
    from wewln_phonecaller.errors import WewlnError

    with pytest.raises(WewlnError):
        NotionClient.from_settings(make_settings())
