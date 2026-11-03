"""Reading and writing Notion page property values within the API's size limits."""

from __future__ import annotations

from typing import Any

TEXT_CHUNK = 2000  # characters per rich text object
MAX_RICH_TEXT_ITEMS = 100
MAX_TEXT = TEXT_CHUNK * MAX_RICH_TEXT_ITEMS
MAX_PHONE = 200
MAX_RELATION = 100


def _prop(page: dict[str, Any], name: str) -> dict[str, Any]:
    value = (page.get("properties") or {}).get(name)
    return value if isinstance(value, dict) else {}


def plain_text(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return "".join(str(item.get("plain_text") or "") for item in items if isinstance(item, dict))


def read_title(page: dict[str, Any]) -> str:
    for value in (page.get("properties") or {}).values():
        if isinstance(value, dict) and value.get("type") == "title":
            return plain_text(value.get("title"))
    return ""


def read_text(page: dict[str, Any], name: str) -> str:
    return plain_text(_prop(page, name).get("rich_text"))


def read_select(page: dict[str, Any], name: str) -> str | None:
    value = _prop(page, name).get("select")
    return value.get("name") if isinstance(value, dict) else None


def read_checkbox(page: dict[str, Any], name: str) -> bool:
    return bool(_prop(page, name).get("checkbox"))


def read_number(page: dict[str, Any], name: str) -> float | None:
    value = _prop(page, name).get("number")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def read_phone(page: dict[str, Any], name: str) -> str | None:
    value = _prop(page, name).get("phone_number")
    return value if isinstance(value, str) else None


def read_relation(page: dict[str, Any], name: str) -> tuple[list[str], bool]:
    """Related page IDs and whether more exist (a page shows at most 25 references)."""
    prop = _prop(page, name)
    ids = [r["id"] for r in prop.get("relation") or [] if isinstance(r, dict) and r.get("id")]
    return ids, bool(prop.get("has_more"))


def property_id(page: dict[str, Any], name: str) -> str | None:
    value = _prop(page, name).get("id")
    return value if isinstance(value, str) else None


def text_value(text: str) -> list[dict[str, Any]]:
    """Rich text input: chunks of at most 2000 characters; empty text clears the value."""
    text = text[:MAX_TEXT]
    return [
        {"type": "text", "text": {"content": text[i : i + TEXT_CHUNK]}}
        for i in range(0, len(text), TEXT_CHUNK)
    ]


def title(text: str) -> dict[str, Any]:
    return {"title": text_value(text)}


def rich_text(text: str) -> dict[str, Any]:
    return {"rich_text": text_value(text)}


def select(name: str | None) -> dict[str, Any]:
    return {"select": {"name": name} if name else None}


def number(value: float | None) -> dict[str, Any]:
    return {"number": value}


def checkbox(value: bool) -> dict[str, Any]:
    return {"checkbox": bool(value)}


def date(start: str | None) -> dict[str, Any]:
    return {"date": {"start": start} if start else None}


def relation(page_ids: list[str]) -> dict[str, Any]:
    return {"relation": [{"id": page_id} for page_id in page_ids[:MAX_RELATION]]}
