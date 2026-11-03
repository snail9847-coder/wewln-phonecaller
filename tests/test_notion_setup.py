"""Creating, checking and repairing the four Notion databases (against MockNotion)."""

import pytest

from wewln_phonecaller.notion.props import text_value
from wewln_phonecaller.notion.schema import DATABASES, DATABASES_BY_KEY
from wewln_phonecaller.notion.setup import (
    NotionSetupError,
    bootstrap,
    describe_connection,
    resolve_workspace,
)

CREATE_DB = r"^/v1/databases$"


def database_ids(workspace):
    return {key: db.database_id for key, db in workspace.databases.items()}


def titled(mock, title):
    return [db for db in mock.databases.values() if db["title"][0]["plain_text"] == title]


async def test_bootstrap_creates_the_four_databases(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)

    assert notion_mock.count("POST", CREATE_DB) == 4
    assert [db.key for db in workspace.databases.values()] == [s.key for s in DATABASES]
    for db in workspace.databases.values():
        assert db.created is True
        assert db.report is not None and db.report.ok
        properties = db.data_source["properties"]
        assert {p.name for p in db.spec.properties} <= set(properties)
    # Relations point to the right data sources.
    contacts = workspace["contacts"].data_source["properties"]
    assert contacts["Кампании"]["relation"]["data_source_id"] == (
        workspace["campaigns"].data_source_id
    )
    results = workspace["results"].data_source["properties"]
    assert results["Задание"]["relation"]["data_source_id"] == workspace["jobs"].data_source_id
    lines = workspace.env_lines()
    assert lines[-1] == "WEWLN_NOTION_ENABLED=true"
    assert {line.split("=")[0] for line in lines[:-1]} == {s.env_var for s in DATABASES}


async def test_bootstrap_again_reuses_everything(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        first = await bootstrap(client, parent)
        second = await bootstrap(client, parent.replace("-", ""))  # IDs work without dashes

    assert notion_mock.count("POST", CREATE_DB) == 4  # nothing created the second time
    assert notion_mock.count("PATCH", r"^/v1/data_sources/") == 0
    assert database_ids(second) == database_ids(first)
    assert not any(db.created or db.repaired for db in second.databases.values())
    assert len(notion_mock.databases) == 4


async def test_bootstrap_adds_missing_properties_and_options(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)
        campaigns = notion_mock.data_sources[workspace["campaigns"].data_source_id]
        del campaigns["properties"]["Сводка"]
        jobs = notion_mock.data_sources[workspace["jobs"].data_source_id]
        options = jobs["properties"]["Статус"]["select"]["options"]
        removed = options.pop()  # the user deleted an option
        kept = {o["name"]: o["id"] for o in options}

        again = await bootstrap(client, parent)

    assert notion_mock.count("POST", CREATE_DB) == 4
    assert again["campaigns"].repaired and again["jobs"].repaired
    assert not again["contacts"].repaired
    assert "Сводка" in campaigns["properties"]
    names = {o["name"]: o["id"] for o in jobs["properties"]["Статус"]["select"]["options"]}
    assert removed["name"] in names
    # Existing options keep their IDs (pages that use them are not touched).
    assert all(names[name] == option_id for name, option_id in kept.items())
    assert all(db.report.ok for db in again.databases.values())


async def test_wrong_property_type_is_never_changed(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)
        contacts = notion_mock.data_sources[workspace["contacts"].data_source_id]
        phone = contacts["properties"]["Телефон"]
        contacts["properties"]["Телефон"] = {
            "id": phone["id"],
            "name": "Телефон",
            "type": "rich_text",
            "rich_text": {},
        }
        with pytest.raises(NotionSetupError) as excinfo:
            await bootstrap(client, parent)

    message = str(excinfo.value)
    assert "«Телефон» must be phone_number, found rich_text" in message
    assert "never changes property types" in message
    assert contacts["properties"]["Телефон"]["type"] == "rich_text"
    assert notion_mock.count("PATCH", r"^/v1/data_sources/") == 0


async def test_relation_to_another_database_is_refused(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)
        contacts = notion_mock.data_sources[workspace["contacts"].data_source_id]
        relation = contacts["properties"]["Кампании"]["relation"]
        relation["data_source_id"] = workspace["jobs"].data_source_id
        with pytest.raises(NotionSetupError, match="points to another database"):
            await bootstrap(client, parent)
        resolved = await resolve_workspace(client, database_ids(workspace))
    assert resolved.blocking_problems() == [
        "Контакты (wewln): relation «Кампании» points to another database"
    ]


async def test_parent_page_must_be_shared(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("private", shared=False)
    async with notion_client_factory(notion_mock) as client:
        with pytest.raises(NotionSetupError) as excinfo:
            await bootstrap(client, parent)
    assert "not shared with the connection" in str(excinfo.value)
    assert "Connections" in str(excinfo.value)
    assert notion_mock.count("POST", CREATE_DB) == 0


async def test_parent_page_in_trash(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("old")
    notion_mock.pages[parent]["in_trash"] = True
    async with notion_client_factory(notion_mock) as client:
        with pytest.raises(NotionSetupError, match="trash"):
            await bootstrap(client, parent)
    assert notion_mock.count("POST", CREATE_DB) == 0


async def test_saved_create_answered_with_an_error_is_not_repeated(
    notion_mock, notion_client_factory
):
    """Notion saved the database but answered 503: bootstrap finds it instead of a 2nd POST."""
    parent = notion_mock.add_page("wewln")
    notion_mock.fail("POST", CREATE_DB, 503, "service_unavailable", apply_first=True)
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)

    assert notion_mock.count("POST", CREATE_DB) == 4
    assert len(titled(notion_mock, DATABASES_BY_KEY["campaigns"].title)) == 1
    assert all(db.report.ok for db in workspace.databases.values())


async def test_saved_create_with_committed_id(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    notion_mock.fail(
        "POST", CREATE_DB, 500, "internal_server_error", apply_first=True, committed_id=True
    )
    async with notion_client_factory(notion_mock) as client:
        workspace = await bootstrap(client, parent)
    assert notion_mock.count("POST", CREATE_DB) == 4
    assert len(notion_mock.databases) == 4
    assert workspace["campaigns"].created


async def test_unsaved_create_asks_to_run_again(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    notion_mock.fail("POST", CREATE_DB, 503, "service_unavailable")
    async with notion_client_factory(notion_mock) as client:
        with pytest.raises(NotionSetupError, match="run bootstrap again"):
            await bootstrap(client, parent)
        assert len(notion_mock.databases) == 0
        workspace = await bootstrap(client, parent)
    assert len(notion_mock.databases) == 4
    assert all(db.report.ok for db in workspace.databases.values())


async def test_two_databases_with_the_same_title_are_refused(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    title = DATABASES_BY_KEY["campaigns"].title
    async with notion_client_factory(notion_mock) as client:
        await bootstrap(client, parent)
        await client.create_database(
            {
                "parent": {"type": "page_id", "page_id": parent},
                "title": text_value(title),
                "initial_data_source": {"properties": {"Name": {"title": {}}}},
            }
        )
        with pytest.raises(NotionSetupError, match="2 databases named"):
            await bootstrap(client, parent)


async def test_trashed_database_is_created_again(notion_mock, notion_client_factory):
    parent = notion_mock.add_page("wewln")
    async with notion_client_factory(notion_mock) as client:
        first = await bootstrap(client, parent)
        notion_mock.databases[first["results"].database_id]["in_trash"] = True
        second = await bootstrap(client, parent)
    assert second["results"].created
    assert second["results"].database_id != first["results"].database_id
    assert not second["jobs"].created


async def test_resolve_accepts_data_source_ids(notion_world, notion_client_factory):
    w = notion_world
    async with w.client() as client:
        workspace = await resolve_workspace(client, dict(w.ds))
    assert database_ids(workspace) == database_ids(w.workspace)
    assert workspace.data_source_ids == w.ds
    assert workspace.problems() == []


async def test_resolve_requires_every_database(notion_world):
    ids = database_ids(notion_world.workspace)
    ids["jobs"] = None
    async with notion_world.client() as client:
        with pytest.raises(NotionSetupError, match="WEWLN_NOTION_JOBS_DB"):
            await resolve_workspace(client, ids)


async def test_resolve_refuses_the_same_database_twice(notion_world):
    ids = database_ids(notion_world.workspace)
    ids["results"] = ids["jobs"]
    async with notion_world.client() as client:
        with pytest.raises(NotionSetupError, match="more than once"):
            await resolve_workspace(client, ids)


async def test_resolve_reports_an_unshared_database(notion_world):
    w = notion_world
    w.mock.no_access.add(w.workspace["contacts"].database_id)
    async with w.client() as client:
        with pytest.raises(NotionSetupError) as excinfo:
            await resolve_workspace(client, database_ids(w.workspace))
    assert "WEWLN_NOTION_CONTACTS_DB" in str(excinfo.value)
    assert "not shared with the connection" in str(excinfo.value)


async def test_resolve_reports_missing_capabilities(notion_world):
    w = notion_world
    w.mock.fail("GET", r"^/v1/databases/", 403, "restricted_resource")
    async with w.client() as client:
        with pytest.raises(NotionSetupError, match="lacks a capability"):
            await resolve_workspace(client, database_ids(w.workspace))


async def test_resolve_reports_a_database_in_trash(notion_world):
    w = notion_world
    w.mock.databases[w.workspace["jobs"].database_id]["in_trash"] = True
    async with w.client() as client:
        with pytest.raises(NotionSetupError, match="trash"):
            await resolve_workspace(client, database_ids(w.workspace))


async def test_resolve_and_repair(notion_world):
    w = notion_world
    contacts = w.mock.data_sources[w.ds["contacts"]]
    del contacts["properties"]["Причина запрета"]
    campaigns = w.mock.data_sources[w.ds["campaigns"]]
    campaigns["properties"]["Язык"]["select"]["options"].pop()
    async with w.client() as client:
        checked = await resolve_workspace(client, database_ids(w.workspace))
        assert len(checked.problems()) == 2
        # A missing option is not blocking: Notion adds it when a page uses it.
        assert checked.blocking_problems() == [
            "Контакты (wewln): missing property «Причина запрета» (rich_text)"
        ]
        assert w.mock.count("PATCH", r"^/v1/data_sources/") == 0

        repaired = await resolve_workspace(client, database_ids(w.workspace), repair=True)
    assert repaired.problems() == []
    assert repaired["contacts"].repaired and repaired["campaigns"].repaired
    assert "Причина запрета" in contacts["properties"]


async def test_renamed_title_property_is_accepted(notion_world):
    w = notion_world
    campaigns = w.mock.data_sources[w.ds["campaigns"]]
    title = campaigns["properties"].pop("Название")
    title["name"] = "Name"
    campaigns["properties"]["Name"] = title
    async with w.client() as client:
        workspace = await resolve_workspace(client, database_ids(w.workspace))
    assert workspace.problems() == []
    assert workspace["campaigns"].title_property == "Name"


async def test_describe_connection_has_no_secret(notion_world):
    async with notion_world.client() as client:
        info = await describe_connection(client)
    assert info == {
        "name": "wewln mock connection",
        "type": "bot",
        "workspace": "Mock workspace",
        "owner": "workspace",
    }
    assert notion_world.mock.token not in repr(info)
