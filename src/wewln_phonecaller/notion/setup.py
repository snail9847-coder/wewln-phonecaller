"""Creating and checking the four wewln databases in Notion.

`bootstrap` is idempotent: databases that already exist under the parent page (same title) are
reused, missing properties and select options are added, and nothing is ever deleted or retyped.
`resolve_workspace` finds the configured databases and compares them with the expected schema;
the sync runs it at the start of every cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..errors import WewlnError
from .client import NotionAccessError, NotionClient, NotionUncertainWriteError
from .ids import normalize_notion_id
from .props import plain_text, text_value
from .schema import (
    DATABASES,
    DATABASES_BY_KEY,
    DatabaseSpec,
    SchemaReport,
    compare_schema,
    repair_body,
    title_property_name,
)

log = logging.getLogger("wewln.notion")

SHARE_HINT = (
    "share it with the connection: open the page in Notion, ••• → Connections → "
    "add your connection (child pages and databases inherit the access)"
)


class NotionSetupError(WewlnError):
    """The Notion databases are missing, not shared or do not have the expected structure."""

    code = "notion_setup_error"
    http_status = 409


@dataclass
class ResolvedDatabase:
    key: str
    database_id: str
    data_source_id: str
    data_source: dict[str, Any]
    created: bool = False
    repaired: bool = False
    report: SchemaReport | None = None

    @property
    def spec(self) -> DatabaseSpec:
        return DATABASES_BY_KEY[self.key]

    @property
    def title_property(self) -> str:
        """Name of the title property (users may rename it)."""
        return title_property_name(self.data_source, self.spec)


@dataclass
class Workspace:
    databases: dict[str, ResolvedDatabase]
    parent_page_id: str | None = None
    warnings: list[str] = field(default_factory=list)

    def __getitem__(self, key: str) -> ResolvedDatabase:
        return self.databases[key]

    @property
    def data_source_ids(self) -> dict[str, str]:
        return {key: db.data_source_id for key, db in self.databases.items()}

    def problems(self) -> list[str]:
        return [p for db in self.databases.values() if db.report for p in db.report.problems()]

    def blocking_problems(self) -> list[str]:
        """Problems the sync cannot work around. Missing select options are not among them:
        Notion adds an option when a page is written with a new option name."""
        lines: list[str] = []
        for db in self.databases.values():
            report = db.report
            if report is None:
                continue
            reduced = SchemaReport(
                report.key,
                missing=report.missing,
                wrong_type=report.wrong_type,
                wrong_target=report.wrong_target,
            )
            lines += reduced.problems()
        return lines

    def env_lines(self) -> list[str]:
        lines = [f"{db.spec.env_var}={db.database_id}" for db in self.databases.values()]
        return [*lines, "WEWLN_NOTION_ENABLED=true"]


async def _child_databases(client: NotionClient, page_id: str) -> dict[str, list[str]]:
    """Titles of the databases directly inside a page -> their IDs."""
    found: dict[str, list[str]] = {}
    for block in await client.block_children(page_id):
        if block.get("type") != "child_database" or block.get("in_trash"):
            continue
        title = str((block.get("child_database") or {}).get("title") or "")
        found.setdefault(title.strip(), []).append(block["id"])
    return found


async def _resolve_one(
    client: NotionClient, spec: DatabaseSpec, object_id: str, label: str
) -> tuple[str, dict[str, Any]]:
    """Database (or data source) ID -> (database ID, data source object)."""
    try:
        database = await client.retrieve_database(object_id)
    except NotionAccessError as exc:
        if exc.status == 403:
            raise NotionSetupError(
                f"{label}: the connection lacks a capability (it needs read, update and insert "
                "content)"
            ) from exc
        # The value may be a data source ID instead of a database ID.
        try:
            data_source = await client.retrieve_data_source(object_id)
        except NotionAccessError:
            raise NotionSetupError(
                f"{label}: database {object_id} was not found or is not shared with the "
                f"connection; {SHARE_HINT}"
            ) from exc
        parent = data_source.get("parent") or {}
        return str(parent.get("database_id") or object_id), data_source
    if database.get("in_trash"):
        raise NotionSetupError(f"{label}: the database is in the Notion trash")
    sources = database.get("data_sources") or []
    if len(sources) != 1:
        raise NotionSetupError(
            f"{label}: the database has {len(sources)} data sources; wewln needs exactly one"
        )
    data_source = await client.retrieve_data_source(sources[0]["id"])
    return str(database["id"]), data_source


async def resolve_workspace(
    client: NotionClient, database_ids: dict[str, str | None], *, repair: bool = False
) -> Workspace:
    """Find the configured databases and compare them with the expected schema.

    `repair`: add missing properties and select options (types are never changed).
    """
    missing = [spec.env_var for spec in DATABASES if not database_ids.get(spec.key)]
    if missing:
        raise NotionSetupError(
            "not configured: " + ", ".join(missing) + " (wewln notion bootstrap prints them)"
        )
    resolved: dict[str, ResolvedDatabase] = {}
    for spec in DATABASES:
        configured = normalize_notion_id(str(database_ids[spec.key]))
        database_id, data_source = await _resolve_one(client, spec, configured, spec.env_var)
        resolved[spec.key] = ResolvedDatabase(
            spec.key, database_id, str(data_source["id"]), data_source
        )
    data_source_ids = {key: db.data_source_id for key, db in resolved.items()}
    if len(set(data_source_ids.values())) != len(data_source_ids):
        raise NotionSetupError("the same Notion database is configured more than once")
    workspace = Workspace(resolved)
    for db in resolved.values():
        db.report = compare_schema(db.spec, db.data_source, data_source_ids)
        if repair:
            await _repair(client, db, data_source_ids)
    return workspace


async def _repair(
    client: NotionClient, db: ResolvedDatabase, data_source_ids: dict[str, str]
) -> None:
    if db.report is None:
        return
    body = repair_body(db.report, db.data_source, data_source_ids)
    if body is None:
        return
    db.data_source = await client.update_data_source(db.data_source_id, body)
    db.repaired = True
    db.report = compare_schema(db.spec, db.data_source, data_source_ids)
    log.info("notion_schema_repaired", extra={"database": db.key})


async def bootstrap(client: NotionClient, parent: str) -> Workspace:
    """Create the databases under `parent` (a page shared with the connection), or reuse them."""
    parent_id = normalize_notion_id(parent)
    try:
        page = await client.retrieve_page(parent_id)
    except NotionAccessError as exc:
        raise NotionSetupError(
            f"the parent page was not found or is not shared with the connection; {SHARE_HINT}"
        ) from exc
    if page.get("in_trash"):
        raise NotionSetupError("the parent page is in the Notion trash")
    existing = await _child_databases(client, parent_id)
    resolved: dict[str, ResolvedDatabase] = {}
    for spec in DATABASES:
        data_source_ids = {key: db.data_source_id for key, db in resolved.items()}
        ids = existing.get(spec.title, [])
        if len(ids) > 1:
            raise NotionSetupError(
                f"the parent page contains {len(ids)} databases named «{spec.title}»: "
                "delete or rename the extra ones"
            )
        if ids:
            database_id, data_source = await _resolve_one(client, spec, ids[0], f"«{spec.title}»")
            db = ResolvedDatabase(spec.key, database_id, str(data_source["id"]), data_source)
        else:
            db = await _create(client, spec, parent_id, data_source_ids)
        data_source_ids[spec.key] = db.data_source_id
        db.report = compare_schema(spec, db.data_source, data_source_ids)
        if not db.report.fixable:
            raise NotionSetupError(
                "; ".join(db.report.problems())
                + ". wewln never changes property types: fix the property in Notion "
                "(or rename it so that wewln adds a new one) and run bootstrap again"
            )
        await _repair(client, db, data_source_ids)
        resolved[spec.key] = db
    return Workspace(resolved, parent_page_id=parent_id)


async def _create(
    client: NotionClient, spec: DatabaseSpec, parent_id: str, data_source_ids: dict[str, str]
) -> ResolvedDatabase:
    body = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "title": text_value(spec.title),
        "description": text_value(spec.description),
        "initial_data_source": {"properties": spec.initial_properties(data_source_ids)},
    }
    try:
        database = await client.create_database(body)
    except NotionUncertainWriteError as exc:
        # The database may exist now. Never create it twice: look for it first.
        found = (await _child_databases(client, parent_id)).get(spec.title, [])
        target = exc.committed_resource_id or (found[0] if len(found) == 1 else None)
        if target is None:
            raise NotionSetupError(
                f"Notion did not confirm that «{spec.title}» was created; run bootstrap again "
                "(it reuses the databases that exist)"
            ) from exc
        database = await client.retrieve_database(target)
    log.info("notion_database_created", extra={"database": spec.key})
    sources = database.get("data_sources") or []
    if len(sources) != 1:
        raise NotionSetupError(f"«{spec.title}»: unexpected data sources after creation")
    data_source = await client.retrieve_data_source(sources[0]["id"])
    return ResolvedDatabase(
        spec.key, str(database["id"]), str(data_source["id"]), data_source, created=True
    )


async def describe_connection(client: NotionClient) -> dict[str, Any]:
    """Who the token belongs to (bot name, workspace) without any secret."""
    me = await client.get_self()
    bot = me.get("bot") or {}
    owner = bot.get("owner") or {}
    return {
        "name": me.get("name") or "",
        "type": me.get("type") or "",
        "workspace": bot.get("workspace_name") or "",
        "owner": owner.get("type") or "",
    }


def database_title(db: ResolvedDatabase) -> str:
    return plain_text(db.data_source.get("title")) or db.spec.title
