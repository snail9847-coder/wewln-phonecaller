"""In-memory imitation of the Notion API for tests and offline trials (no Notion account needed).

It implements the subset of the official API that wewln uses and imitates Notion where it
matters for correctness: auth and Notion-Version checks, error objects, opaque pagination
cursors, the per-query result cap (10,000 in Notion, configurable here), relations truncated to
25 references in page objects, minute precision of created_time, rich text size limits, and
fault injection (429 with Retry-After, 5xx, writes that are saved but answered with 503).

It is not a complete or exact copy of Notion; tests against it do not replace a check with a
real workspace.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import re
import socket
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

from .client import NOTION_VERSION

DEFAULT_TOKEN = "ntn_mock_" + "0" * 40
BOT_ID = "00000000-0000-4000-8000-0000000000b0"
WORKSPACE_ID = "00000000-0000-4000-8000-0000000000a0"
TEXT_LIMIT = 2000
ARRAY_LIMIT = 100
SUPPORTED_TYPES = frozenset(
    {"title", "rich_text", "select", "number", "checkbox", "date", "phone_number", "relation"}
)


class MockError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        additional: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.additional = additional
        self.headers = headers or {}


@dataclass
class Fault:
    method: str
    path: str  # regular expression searched in the request path
    status: int
    code: str
    times: int = 1
    retry_after: int | None = None
    rate_limit_reason: str | None = None
    apply_first: bool = False  # process the request, then answer with the error
    committed_id: bool = False  # report additional_data.committed_resource_id
    message: str = "Injected fault"


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, str]
    body: Any
    status: int = 0


def _uuid() -> str:
    return str(uuid.uuid4())


def _norm(value: str) -> str:
    """Accept IDs with or without dashes, like Notion does."""
    raw = value.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", raw):
        raise MockError(400, "validation_error", f"path failed validation: {value} is not a UUID")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _rich(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    return [_rich_item(text[i : i + TEXT_LIMIT]) for i in range(0, len(text), TEXT_LIMIT)]


def _rich_item(text: str) -> dict[str, Any]:
    return {
        "type": "text",
        "text": {"content": text, "link": None},
        "annotations": {
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
        },
        "plain_text": text,
        "href": None,
    }


def _plain(items: list[dict[str, Any]]) -> str:
    return "".join(i.get("plain_text", "") for i in items)


def _parse_time(value: str) -> datetime:
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class MockNotion:
    def __init__(
        self,
        token: str = DEFAULT_TOKEN,
        *,
        query_result_limit: int = 10_000,
        relation_limit: int = 25,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.token = token
        self.query_result_limit = query_result_limit
        self.relation_limit = relation_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self.pages: dict[str, dict[str, Any]] = {}
        self.databases: dict[str, dict[str, Any]] = {}
        self.data_sources: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[str]] = {}
        self.no_access: set[str] = set()
        self.faults: list[Fault] = []
        self.requests: list[RecordedRequest] = []
        self.down = False
        self._seq = 0
        self._order: dict[str, int] = {}
        self._prop_seq = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers for tests

    def now(self) -> str:
        return self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00.000Z")

    def add_page(self, title: str = "wewln", *, shared: bool = True) -> str:
        """A standalone page (for example the parent page for `wewln notion bootstrap`)."""
        with self._lock:
            page_id = _uuid()
            now = self.now()
            self.pages[page_id] = {
                "object": "page",
                "id": page_id,
                "created_time": now,
                "last_edited_time": now,
                "created_by": {"object": "user", "id": BOT_ID},
                "last_edited_by": {"object": "user", "id": BOT_ID},
                "parent": {"type": "workspace", "workspace": True},
                "in_trash": False,
                "is_archived": False,
                "is_locked": False,
                "icon": None,
                "cover": None,
                "properties": {"title": {"id": "title", "type": "title", "title": _rich(title)}},
                "url": f"https://app.notion.com/p/{page_id.replace('-', '')}",
                "public_url": None,
            }
            self._order[page_id] = self._next_seq()
            if not shared:
                self.no_access.add(page_id)
            return page_id

    def fail(self, method: str, path: str, status: int, code: str, **kwargs: Any) -> Fault:
        fault = Fault(method=method.upper(), path=path, status=status, code=code, **kwargs)
        with self._lock:
            self.faults.append(fault)
        return fault

    def count(self, method: str, path_regex: str) -> int:
        with self._lock:
            return sum(
                1
                for r in self.requests
                if r.method == method.upper() and re.search(path_regex, r.path)
            )

    def data_source_by_title(self, title: str) -> str:
        for ds_id, ds in self.data_sources.items():
            if _plain(ds["title"]) == title:
                return ds_id
        raise KeyError(title)

    def rows(self, data_source_id: str, *, include_trashed: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            return [
                p for p in self._pages_in(data_source_id) if include_trashed or not p["in_trash"]
            ]

    def simple(self, page: dict[str, Any]) -> dict[str, Any]:
        """Property values as plain Python values (for assertions)."""
        out: dict[str, Any] = {}
        for name, value in page["properties"].items():
            kind = value["type"]
            raw = value.get(kind)
            if kind in ("title", "rich_text"):
                out[name] = _plain(raw)
            elif kind == "select":
                out[name] = raw["name"] if raw else None
            elif kind == "relation":
                out[name] = [r["id"] for r in raw]
            elif kind == "date":
                out[name] = raw["start"] if raw else None
            else:
                out[name] = raw
        return out

    def create_row(self, data_source_id: str, values: dict[str, Any]) -> str:
        """Create a page the way a user would in the Notion app (plain Python values)."""
        with self._lock:
            ds = self._data_source(_norm(data_source_id))
            properties = {name: self._simple_input(ds, name, v) for name, v in values.items()}
            page = self._create_page(
                {"parent": {"data_source_id": ds["id"]}, "properties": properties}
            )
            return page["id"]

    def update_row(self, page_id: str, values: dict[str, Any]) -> None:
        with self._lock:
            page = self._page(_norm(page_id))
            ds = self._data_source(page["parent"]["data_source_id"])
            properties = {name: self._simple_input(ds, name, v) for name, v in values.items()}
            self._update_page(page["id"], {"properties": properties})

    def trash(self, page_id: str, in_trash: bool = True) -> None:
        with self._lock:
            page = self.pages[_norm(page_id)]
            page["in_trash"] = in_trash
            page["last_edited_time"] = self.now()

    def _simple_input(self, ds: dict[str, Any], name: str, value: Any) -> dict[str, Any]:
        kind = self._prop_schema(ds, name)["type"]
        if kind in ("title", "rich_text"):
            return {kind: [{"text": {"content": str(value)}}] if value else []}
        if kind == "select":
            return {"select": {"name": value} if value else None}
        if kind == "relation":
            return {"relation": [{"id": v} for v in value]}
        if kind == "date":
            return {"date": {"start": value} if value else None}
        return {kind: value}

    # ------------------------------------------------------------------ internals

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _check_access(self, object_id: str) -> None:
        if object_id in self.no_access:
            raise MockError(
                404,
                "object_not_found",
                f"Could not find object with ID: {object_id}. Make sure the relevant pages "
                "and databases are shared with your connection.",
            )

    def _page(self, page_id: str) -> dict[str, Any]:
        page = self.pages.get(page_id)
        if page is None:
            raise MockError(404, "object_not_found", f"Could not find page with ID: {page_id}.")
        self._check_access(page_id)
        parent = page["parent"]
        if parent["type"] == "data_source_id":
            self._check_access(parent["database_id"])
        return page

    def _database(self, database_id: str) -> dict[str, Any]:
        db = self.databases.get(database_id)
        if db is None:
            raise MockError(
                404, "object_not_found", f"Could not find database with ID: {database_id}."
            )
        self._check_access(database_id)
        return db

    def _data_source(self, data_source_id: str) -> dict[str, Any]:
        ds = self.data_sources.get(data_source_id)
        if ds is None:
            raise MockError(
                404, "object_not_found", f"Could not find data source with ID: {data_source_id}."
            )
        self._check_access(ds["parent"]["database_id"])
        return ds

    def _pages_in(self, data_source_id: str) -> list[dict[str, Any]]:
        return [
            p for p in self.pages.values() if p["parent"].get("data_source_id") == data_source_id
        ]

    def _prop_schema(self, ds: dict[str, Any], key: str) -> dict[str, Any]:
        props = ds["properties"]
        if key in props:
            return props[key]
        for prop in props.values():
            if prop["id"] == key:
                return prop
        raise MockError(
            400, "validation_error", f"{key} is not a property that exists in this data source."
        )

    def _new_prop_id(self) -> str:
        self._prop_seq += 1
        return f"p{self._prop_seq:03d}"

    # ------------------------------------------------------------------ schema

    def _property_from_config(
        self, name: str, config: Any, existing: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise MockError(400, "validation_error", f"Invalid configuration for {name}.")
        kinds = [k for k in config if k in SUPPORTED_TYPES]
        kind = config.get("type") or (kinds[0] if len(kinds) == 1 else None)
        if kind not in SUPPORTED_TYPES or kind not in config:
            raise MockError(400, "validation_error", f"Unsupported property configuration: {name}.")
        settings = config[kind] or {}
        prop: dict[str, Any] = {
            "id": existing["id"]
            if existing
            else ("title" if kind == "title" else self._new_prop_id()),
            "name": name,
            "description": None,
            "type": kind,
        }
        if kind == "select":
            old = {o["id"]: o for o in ((existing or {}).get("select") or {}).get("options", [])}
            if existing and existing.get("type") == "select" and "options" not in settings:
                prop["select"] = copy.deepcopy(existing["select"])
                return prop
            options = []
            seen: set[str] = set()
            for option in settings.get("options") or []:
                if "id" in option and option["id"] in old:
                    entry = copy.deepcopy(old[option["id"]])
                elif "name" in option:
                    entry = {
                        "id": _uuid(),
                        "name": str(option["name"]),
                        "color": option.get("color", "default"),
                        "description": option.get("description"),
                    }
                else:
                    raise MockError(400, "validation_error", "Select option needs a name or id.")
                if entry["name"] in seen or len(entry["name"]) > 100:
                    raise MockError(400, "validation_error", "Invalid or duplicate select option.")
                seen.add(entry["name"])
                options.append(entry)
            if len(options) > ARRAY_LIMIT:
                raise MockError(400, "validation_error", "Too many select options.")
            prop["select"] = {"options": options}
        elif kind == "relation":
            target = settings.get("data_source_id")
            if not target or _norm(target) not in self.data_sources:
                raise MockError(400, "validation_error", f"Relation {name} needs a data source.")
            target = _norm(target)
            prop["relation"] = {
                "database_id": self.data_sources[target]["parent"]["database_id"],
                "data_source_id": target,
                "type": "single_property",
                "single_property": {},
            }
        elif kind == "number":
            prop["number"] = {"format": settings.get("format", "number")}
        else:
            prop[kind] = {}
        return prop

    def _create_database(self, body: dict[str, Any]) -> dict[str, Any]:
        parent = body.get("parent") or {}
        if parent.get("type", "page_id") != "page_id" or "page_id" not in parent:
            raise MockError(400, "validation_error", "body.parent.page_id should be defined.")
        parent_id = _norm(parent["page_id"])
        parent_page = self._page(parent_id)
        if parent_page["in_trash"]:
            raise MockError(400, "validation_error", "The parent page is in the trash.")
        if "database_type" in body and "initial_data_source" in body:
            raise MockError(400, "validation_error", "database_type with initial_data_source.")
        title = _plain(self._rich_input(body.get("title") or [], "title"))
        config = ((body.get("initial_data_source") or {}).get("properties")) or {}
        properties = {name: self._property_from_config(name, c) for name, c in config.items()}
        if sum(1 for p in properties.values() if p["type"] == "title") != 1:
            raise MockError(400, "validation_error", "A data source needs exactly one title.")
        now = self.now()
        db_id, ds_id = _uuid(), _uuid()
        self.databases[db_id] = {
            "object": "database",
            "id": db_id,
            "title": _rich(title),
            "description": body.get("description") or [],
            "parent": {"type": "page_id", "page_id": parent_id},
            "is_inline": bool(body.get("is_inline", False)),
            "in_trash": False,
            "is_locked": False,
            "created_time": now,
            "last_edited_time": now,
            "data_sources": [{"id": ds_id, "name": title}],
            "icon": None,
            "cover": None,
            "url": f"https://app.notion.com/p/{db_id.replace('-', '')}",
            "public_url": None,
        }
        self.data_sources[ds_id] = {
            "object": "data_source",
            "id": ds_id,
            "title": _rich(title),
            "description": [],
            "parent": {"type": "database_id", "database_id": db_id},
            "database_parent": {"type": "page_id", "page_id": parent_id},
            "properties": properties,
            "in_trash": False,
            "created_time": now,
            "last_edited_time": now,
            "url": f"https://app.notion.com/p/{ds_id.replace('-', '')}",
        }
        self.children.setdefault(parent_id, []).append(db_id)
        return copy.deepcopy(self.databases[db_id])

    def _update_data_source(self, ds_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ds = self._data_source(ds_id)
        for key, value in (body.get("properties") or {}).items():
            try:
                existing = self._prop_schema(ds, key)
            except MockError:
                existing = None
            if value is None:
                if existing is None or existing["type"] == "title":
                    raise MockError(400, "validation_error", f"Cannot remove {key}.")
                del ds["properties"][existing["name"]]
                continue
            if existing is not None and set(value) <= {"name", "description"}:
                new_name = value.get("name", existing["name"])
                ds["properties"].pop(existing["name"])
                existing["name"] = new_name
                ds["properties"][new_name] = existing
                continue
            name = value.get("name") or (existing["name"] if existing else key)
            prop = self._property_from_config(name, value, existing)
            if existing is not None:
                ds["properties"].pop(existing["name"])
            ds["properties"][name] = prop
        ds["last_edited_time"] = self.now()
        return copy.deepcopy(ds)

    # ------------------------------------------------------------------ pages

    def _rich_input(self, items: Any, name: str) -> list[dict[str, Any]]:
        if not isinstance(items, list) or len(items) > ARRAY_LIMIT:
            raise MockError(400, "validation_error", f"{name} should be an array of ≤ 100 items.")
        out = []
        for item in items:
            text = (item or {}).get("text") if isinstance(item, dict) else None
            content = text.get("content") if isinstance(text, dict) else None
            if not isinstance(content, str) or not 1 <= len(content) <= TEXT_LIMIT:
                raise MockError(
                    400,
                    "validation_error",
                    f"body failed validation: {name}.text.content.length should be ≤ 2000 and ≥ 1.",
                )
            out.append(_rich_item(content))
        return out

    def _value(self, ds: dict[str, Any], schema: dict[str, Any], raw: Any) -> Any:
        kind = schema["type"]
        name = schema["name"]
        if not isinstance(raw, dict) or kind not in raw:
            raise MockError(400, "validation_error", f"{name} is expected to be {kind}.")
        value = raw[kind]
        if kind in ("title", "rich_text"):
            return self._rich_input(value, name)
        if kind == "select":
            if value is None:
                return None
            options = schema["select"]["options"]
            match = next(
                (
                    o
                    for o in options
                    if o["name"] == value.get("name") or o["id"] == value.get("id")
                ),
                None,
            )
            if match is None:
                # Like Notion: a new option name is added to the schema (the connection can
                # write to the data source); an unknown option ID is an error.
                if not isinstance(value.get("name"), str) or not value["name"].strip():
                    raise MockError(400, "validation_error", f"Invalid select option for {name}.")
                if "," in value["name"]:
                    raise MockError(400, "validation_error", "Option names cannot contain commas.")
                match = {"id": _uuid(), "name": value["name"], "color": "default"}
                options.append(match)
            return {"id": match["id"], "name": match["name"], "color": match["color"]}
        if kind == "number":
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | float)
            ):
                raise MockError(400, "validation_error", f"{name} should be a number.")
            return value
        if kind == "checkbox":
            if not isinstance(value, bool):
                raise MockError(400, "validation_error", f"{name} should be a boolean.")
            return value
        if kind == "phone_number":
            if value is not None and (not isinstance(value, str) or len(value) > 200):
                raise MockError(400, "validation_error", f"{name} should be ≤ 200 characters.")
            return value
        if kind == "date":
            if value is None:
                return None
            if not isinstance(value, dict) or "start" not in value:
                raise MockError(400, "validation_error", f"{name} should have a start.")
            _parse_time(value["start"])
            return {"start": value["start"], "end": value.get("end"), "time_zone": None}
        if kind == "relation":
            if not isinstance(value, list) or len(value) > ARRAY_LIMIT:
                raise MockError(400, "validation_error", f"{name} allows ≤ 100 pages.")
            target = schema["relation"]["data_source_id"]
            ids = []
            for ref in value:
                page_id = _norm(ref["id"])
                page = self.pages.get(page_id)
                if page is None or page["parent"].get("data_source_id") != target:
                    raise MockError(
                        400, "validation_error", f"Relation {name} refers to an unknown page."
                    )
                ids.append({"id": page_id})
            return ids
        raise MockError(400, "validation_error", f"Unsupported property type {kind}.")

    @staticmethod
    def _empty(kind: str) -> Any:
        return (
            []
            if kind in ("title", "rich_text", "relation")
            else (False if kind == "checkbox" else None)
        )

    def _create_page(self, body: dict[str, Any]) -> dict[str, Any]:
        parent = body.get("parent") or {}
        if "data_source_id" in parent:
            ds = self._data_source(_norm(parent["data_source_id"]))
        elif "database_id" in parent:
            db = self._database(_norm(parent["database_id"]))
            if len(db["data_sources"]) != 1:
                raise MockError(400, "validation_error", "Use parent.data_source_id.")
            ds = self._data_source(db["data_sources"][0]["id"])
        else:
            raise MockError(400, "validation_error", "body.parent.data_source_id should be set.")
        values: dict[str, Any] = {}
        for key, raw in (body.get("properties") or {}).items():
            schema = self._prop_schema(ds, key)
            values[schema["name"]] = self._value(ds, schema, raw)
        properties = {}
        for name, schema in ds["properties"].items():
            kind = schema["type"]
            value = values.get(name, self._empty(kind))
            properties[name] = {"id": schema["id"], "type": kind, kind: value}
        now = self.now()
        page_id = _uuid()
        page = {
            "object": "page",
            "id": page_id,
            "created_time": now,
            "last_edited_time": now,
            "created_by": {"object": "user", "id": BOT_ID},
            "last_edited_by": {"object": "user", "id": BOT_ID},
            "parent": {
                "type": "data_source_id",
                "data_source_id": ds["id"],
                "database_id": ds["parent"]["database_id"],
            },
            "in_trash": False,
            "is_archived": False,
            "is_locked": False,
            "icon": None,
            "cover": None,
            "properties": properties,
            "url": f"https://app.notion.com/p/{page_id.replace('-', '')}",
            "public_url": None,
        }
        self.pages[page_id] = page
        self._order[page_id] = self._next_seq()
        return self._page_out(page)

    def _update_page(self, page_id: str, body: dict[str, Any]) -> dict[str, Any]:
        page = self._page(page_id)
        if "archived" in body:
            raise MockError(400, "validation_error", "archived is not supported; use in_trash.")
        if "in_trash" in body:
            page["in_trash"] = bool(body["in_trash"])
        properties = body.get("properties") or {}
        if properties:
            if page["in_trash"]:
                raise MockError(400, "validation_error", "Can't edit a page that is in the trash.")
            if page["parent"]["type"] != "data_source_id":
                raise MockError(400, "validation_error", "Only title can be set on this page.")
            ds = self._data_source(page["parent"]["data_source_id"])
            for key, raw in properties.items():
                schema = self._prop_schema(ds, key)
                page["properties"][schema["name"]] = {
                    "id": schema["id"],
                    "type": schema["type"],
                    schema["type"]: self._value(ds, schema, raw),
                }
        page["last_edited_time"] = self.now()
        return self._page_out(page)

    def _page_out(self, page: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(page)
        for value in out["properties"].values():
            if value["type"] == "relation":
                refs = value["relation"]
                value["relation"] = refs[: self.relation_limit]
                value["has_more"] = len(refs) > self.relation_limit
        return out

    # ------------------------------------------------------------------ queries

    def _matches(self, page: dict[str, Any], ds: dict[str, Any], flt: dict[str, Any]) -> bool:
        if "and" in flt:
            return all(self._matches(page, ds, f) for f in flt["and"])
        if "or" in flt:
            return any(self._matches(page, ds, f) for f in flt["or"])
        if "timestamp" in flt:
            if "property" in flt:
                raise MockError(400, "validation_error", "A timestamp filter takes no property.")
            kind = flt["timestamp"]
            if kind not in ("created_time", "last_edited_time") or kind not in flt:
                raise MockError(400, "validation_error", "Invalid timestamp filter.")
            return self._date_matches(_parse_time(page[kind]), flt[kind])
        if "property" not in flt:
            raise MockError(400, "validation_error", "Invalid filter.")
        schema = self._prop_schema(ds, flt["property"])
        kind = schema["type"]
        condition = flt.get(kind)
        if not isinstance(condition, dict) or len(condition) != 1:
            raise MockError(400, "validation_error", f"Invalid {kind} filter.")
        ((op, operand),) = condition.items()
        value = page["properties"][schema["name"]][kind]
        if kind in ("title", "rich_text", "phone_number"):
            text = _plain(value) if kind != "phone_number" else (value or "")
            ops = {
                "equals": lambda: text == operand,
                "does_not_equal": lambda: text != operand,
                "contains": lambda: operand in text,
                "does_not_contain": lambda: operand not in text,
                "starts_with": lambda: text.startswith(operand),
                "ends_with": lambda: text.endswith(operand),
                "is_empty": lambda: not text,
                "is_not_empty": lambda: bool(text),
            }
        elif kind == "select":
            name = value["name"] if value else None
            names = operand if isinstance(operand, list) else [operand]
            ops = {
                "equals": lambda: name in names,
                "does_not_equal": lambda: name not in names,
                "is_empty": lambda: name is None,
                "is_not_empty": lambda: name is not None,
            }
        elif kind == "checkbox":
            ops = {
                "equals": lambda: bool(value) == operand,
                "does_not_equal": lambda: bool(value) != operand,
            }
        elif kind == "relation":
            ids = {r["id"] for r in value}
            ops = {
                "contains": lambda: _norm(operand) in ids,
                "does_not_contain": lambda: _norm(operand) not in ids,
                "is_empty": lambda: not ids,
                "is_not_empty": lambda: bool(ids),
            }
        elif kind == "number":
            ops = {
                "equals": lambda: value == operand,
                "does_not_equal": lambda: value != operand,
                "is_empty": lambda: value is None,
                "is_not_empty": lambda: value is not None,
            }
        elif kind == "date":
            start = _parse_time(value["start"]) if value else None
            return self._date_matches(start, condition)
        else:
            ops = {}
        if op not in ops:
            raise MockError(400, "validation_error", f"Unsupported {kind} filter: {op}.")
        return bool(ops[op]())

    @staticmethod
    def _date_matches(value: datetime | None, condition: dict[str, Any]) -> bool:
        if not isinstance(condition, dict) or len(condition) != 1:
            raise MockError(400, "validation_error", "A date filter takes one operator.")
        ((op, operand),) = condition.items()
        if op == "is_empty":
            return value is None
        if op == "is_not_empty":
            return value is not None
        if value is None:
            return False
        bound = _parse_time(operand)
        checks = {
            "equals": value == bound,
            "before": value < bound,
            "after": value > bound,
            "on_or_before": value <= bound,
            "on_or_after": value >= bound,
        }
        if op not in checks:
            raise MockError(400, "validation_error", f"Unsupported date filter: {op}.")
        return checks[op]

    def _query(self, ds_id: str, body: dict[str, Any]) -> dict[str, Any]:
        ds = self._data_source(ds_id)
        page_size = body.get("page_size", 100)
        if not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise MockError(400, "validation_error", "page_size should be between 1 and 100.")
        flt = body.get("filter")
        pages = [p for p in self._pages_in(ds_id) if not p["in_trash"]]
        if flt:
            pages = [p for p in pages if self._matches(p, ds, flt)]
        pages.sort(key=lambda p: self._order[p["id"]])
        for sort in reversed(body.get("sorts") or []):
            reverse = sort.get("direction") == "descending"
            if "timestamp" in sort:
                pages.sort(key=lambda p, k=sort["timestamp"]: _parse_time(p[k]), reverse=reverse)
            else:
                schema = self._prop_schema(ds, sort["property"])

                def sort_key(p: dict[str, Any], s: dict[str, Any] = schema) -> str:
                    return json.dumps(p["properties"][s["name"]][s["type"]], sort_keys=True)

                pages.sort(key=sort_key, reverse=reverse)
        incomplete = len(pages) > self.query_result_limit
        pages = pages[: self.query_result_limit]
        query_hash = hashlib.sha256(
            json.dumps({"f": flt, "s": body.get("sorts")}, sort_keys=True).encode()
        ).hexdigest()[:16]
        offset = 0
        if body.get("start_cursor"):
            try:
                cursor = json.loads(base64.urlsafe_b64decode(body["start_cursor"]))
                if cursor["q"] != query_hash:
                    raise ValueError("cursor of another query")
                offset = int(cursor["o"])
            except (ValueError, KeyError, TypeError) as exc:
                raise MockError(400, "validation_error", "start_cursor is invalid.") from exc
        chunk = pages[offset : offset + page_size]
        has_more = offset + page_size < len(pages)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"o": offset + page_size, "q": query_hash}).encode()
            ).decode()
            if has_more
            else None
        )
        response: dict[str, Any] = {
            "object": "list",
            "results": [self._page_out(p) for p in chunk],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "type": "page_or_data_source",
            "page_or_data_source": {},
        }
        if incomplete:
            response["request_status"] = {
                "type": "incomplete",
                "incomplete_reason": "query_result_limit_reached",
            }
        return response

    def _property_items(self, page_id: str, prop_id: str, query: dict[str, str]) -> dict[str, Any]:
        page = self._page(page_id)
        value = next(
            (v for n, v in page["properties"].items() if v["id"] == prop_id or n == prop_id), None
        )
        if value is None:
            raise MockError(404, "object_not_found", "Property not found.")
        kind = value["type"]
        if kind not in ("relation", "title", "rich_text"):
            return {"object": "property_item", "id": value["id"], "type": kind, kind: value[kind]}
        items = value[kind]
        page_size = int(query.get("page_size", 25))
        offset = int(query.get("start_cursor") or 0)
        chunk = items[offset : offset + page_size]
        has_more = offset + page_size < len(items)
        return {
            "object": "list",
            "results": [
                {"object": "property_item", "id": value["id"], "type": kind, kind: item}
                for item in chunk
            ],
            "next_cursor": str(offset + page_size) if has_more else None,
            "has_more": has_more,
            "type": "property_item",
            "property_item": {"id": value["id"], "type": kind, "next_url": None, kind: {}},
        }

    def _children(self, block_id: str) -> dict[str, Any]:
        self._page(block_id)
        results = []
        for child_id in self.children.get(block_id, []):
            db = self.databases.get(child_id)
            if db is None or db["in_trash"]:
                continue
            results.append(
                {
                    "object": "block",
                    "id": child_id,
                    "parent": {"type": "page_id", "page_id": block_id},
                    "created_time": db["created_time"],
                    "last_edited_time": db["last_edited_time"],
                    "has_children": False,
                    "in_trash": False,
                    "type": "child_database",
                    "child_database": {"title": _plain(db["title"])},
                }
            )
        return {"object": "list", "results": results, "next_cursor": None, "has_more": False}

    # ------------------------------------------------------------------ HTTP

    def _route(self, method: str, path: str, query: dict[str, str], body: Any) -> Any:
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[0] != "v1":
            raise MockError(400, "invalid_request_url", "Invalid request URL.")
        resource, rest = parts[1], parts[2:]
        if resource == "users" and rest == ["me"] and method == "GET":
            return {
                "object": "user",
                "id": BOT_ID,
                "type": "bot",
                "name": "wewln mock connection",
                "avatar_url": None,
                "bot": {
                    "owner": {"type": "workspace", "workspace": True},
                    "workspace_id": WORKSPACE_ID,
                    "workspace_name": "Mock workspace",
                    "workspace_limits": {"max_file_upload_size_in_bytes": 5_242_880},
                },
            }
        if resource == "databases":
            if not rest and method == "POST":
                return self._create_database(body)
            if len(rest) == 1 and method == "GET":
                return copy.deepcopy(self._database(_norm(rest[0])))
        if resource == "data_sources" and rest:
            ds_id = _norm(rest[0])
            if len(rest) == 1 and method == "GET":
                return copy.deepcopy(self._data_source(ds_id))
            if len(rest) == 1 and method == "PATCH":
                return self._update_data_source(ds_id, body)
            if rest[1:] == ["query"] and method == "POST":
                return self._query(ds_id, body)
        if resource == "pages":
            if not rest and method == "POST":
                return self._create_page(body)
            if len(rest) == 1 and method == "GET":
                return self._page_out(self._page(_norm(rest[0])))
            if len(rest) == 1 and method == "PATCH":
                return self._update_page(_norm(rest[0]), body)
            if len(rest) == 3 and rest[1] == "properties" and method == "GET":
                return self._property_items(_norm(rest[0]), rest[2], query)
        if resource == "blocks" and len(rest) == 2 and rest[1] == "children" and method == "GET":
            return self._children(_norm(rest[0]))
        raise MockError(400, "invalid_request_url", f"Unsupported request: {method} {path}.")

    def _fault_for(self, method: str, path: str) -> Fault | None:
        for fault in self.faults:
            if fault.times > 0 and fault.method in (method, "*") and re.search(fault.path, path):
                fault.times -= 1
                return fault
        return None

    @staticmethod
    def _error(exc: MockError) -> web.Response:
        payload: dict[str, Any] = {
            "object": "error",
            "status": exc.status,
            "code": exc.code,
            "message": exc.message,
            "request_id": _uuid(),
        }
        if exc.additional:
            payload["additional_data"] = exc.additional
        return web.json_response(payload, status=exc.status, headers=exc.headers)

    @staticmethod
    def _fault_error(fault: Fault, committed: str | None) -> MockError:
        additional: dict[str, Any] = {}
        headers: dict[str, str] = {}
        if fault.retry_after is not None:
            headers["Retry-After"] = str(fault.retry_after)
            additional["retry_after"] = str(fault.retry_after)
        if fault.rate_limit_reason:
            additional["rate_limit_reason"] = fault.rate_limit_reason
        if committed:
            additional["committed_resource_id"] = committed
            additional["retry_guidance"] = [
                "Read the object again to confirm the saved change.",
                "Do not repeat the write.",
            ]
        return MockError(
            fault.status, fault.code, fault.message, additional=additional or None, headers=headers
        )

    async def handle(self, request: web.Request) -> web.Response:
        body: Any = None
        if request.can_read_body:
            raw = await request.read()
            if raw:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                    record = RecordedRequest(
                        request.method, request.path, dict(request.query), None
                    )
                    with self._lock:
                        self.requests.append(record)
                    return self._error(MockError(400, "invalid_json", "Invalid JSON body."))
        record = RecordedRequest(request.method, request.path, dict(request.query), body)
        with self._lock:
            self.requests.append(record)
            response = self._handle_locked(request, body)
            record.status = response.status
        return response

    def _handle_locked(self, request: web.Request, body: Any) -> web.Response:
        try:
            if self.down:
                raise MockError(503, "service_unavailable", "Notion is unavailable (mock).")
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {self.token}":
                raise MockError(401, "unauthorized", "API token is invalid.")
            version = request.headers.get("Notion-Version")
            if not version:
                raise MockError(400, "missing_version", "Notion-Version header is missing.")
            if version != NOTION_VERSION:
                raise MockError(400, "validation_error", f"Unsupported Notion-Version {version}.")
            if request.method in ("POST", "PATCH") and not isinstance(body, dict):
                raise MockError(400, "invalid_json", "The request body must be a JSON object.")
            fault = self._fault_for(request.method, request.path)
            if fault is not None and not fault.apply_first:
                raise self._fault_error(fault, None)
            result = self._route(request.method, request.path, dict(request.query), body or {})
            if fault is not None:
                committed = result.get("id") if fault.committed_id else None
                raise self._fault_error(fault, committed)
            if isinstance(result, dict) and result.get("object") == "list":
                result["request_id"] = _uuid()
            return web.json_response(result)
        except MockError as exc:
            return self._error(exc)

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app


class MockNotionServer:
    """Runs a MockNotion on 127.0.0.1 in a background thread (for CLI tests and trials)."""

    def __init__(self, mock: MockNotion | None = None, *, host: str = "127.0.0.1", port: int = 0):
        self.mock = mock or MockNotion()
        self.host = host
        self.port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, args=(sock,), daemon=True)
        self._thread.start()
        if not self._started.wait(10) or self._error is not None:
            raise RuntimeError(f"mock Notion server did not start: {self._error!r}")
        return self.base_url

    def _run(self, sock: socket.socket) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._runner = web.AppRunner(self.mock.app(), access_log=None)
            loop.run_until_complete(self._runner.setup())
            site = web.SockSite(self._runner, sock)
            loop.run_until_complete(site.start())
        except BaseException as exc:  # reported to start()
            self._error = exc
            self._started.set()
            return
        self._started.set()
        loop.run_forever()
        loop.run_until_complete(self._runner.cleanup())
        loop.close()

    def stop(self) -> None:
        if self._loop is not None and self._thread is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(10)
        self._loop = None
        self._thread = None

    def __enter__(self) -> MockNotionServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def stepping_clock(start: datetime | None = None, step: timedelta = timedelta(minutes=1)):
    """A clock that advances by `step` on every call (tests of created_time windows)."""
    current = [start or datetime(2026, 1, 1, tzinfo=UTC)]

    def clock() -> datetime:
        value = current[0]
        current[0] = value + step
        return value

    return clock


# Numbers reserved for fiction by the Australian regulator (ACMA): valid, never assigned.
DEMO_PHONES = ("+61491570156", "+61491570157", "+61491570158")


async def seed_demo(server: MockNotionServer) -> dict[str, Any]:
    """The four databases and a small demo campaign, for a trial without a Notion account."""
    from .client import NotionClient
    from .setup import bootstrap

    mock = server.mock
    parent = mock.add_page("wewln (демо)")
    async with NotionClient(mock.token, base_url=server.base_url, max_rps=100) as client:
        workspace = await bootstrap(client, parent)
    campaign = mock.create_row(
        workspace["campaigns"].data_source_id,
        {
            "Название": "Демо-кампания",
            "Статус": "Запущена",
            "Сценарий": "Здравствуйте! Это демонстрационный звонок.",
            "Язык": "ru-RU",
            "Макс. попыток": 1,
        },
    )
    contacts = workspace["contacts"].data_source_id
    mock.create_row(
        contacts, {"Имя": "Анна (демо)", "Телефон": DEMO_PHONES[0], "Кампании": [campaign]}
    )
    mock.create_row(
        contacts,
        {
            "Имя": "Борис (демо)",
            "Телефон": DEMO_PHONES[1],
            "Кампании": [campaign],
            "Не звонить": True,
            "Причина запрета": "Просил не звонить",
        },
    )
    mock.create_row(contacts, {"Имя": "Вера (демо)", "Телефон": "12345", "Кампании": [campaign]})
    return {"parent": parent, "campaign": campaign, "env": workspace.env_lines()}
