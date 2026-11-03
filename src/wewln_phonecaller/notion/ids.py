"""Notion IDs: accept what users paste (IDs with or without dashes, page or database links)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# In links the ID ends a path segment: ".../Title-<32 hex>" or ".../p/<32 hex>".
_HEX32_AT_END = re.compile(r"[0-9a-fA-F]{32}$")
_UUID_AT_END = re.compile(_UUID.pattern + "$")


def _canonical(hex32: str) -> str:
    h = hex32.lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def normalize_notion_id(value: str) -> str:
    """Return the canonical dashed lowercase ID from an ID or a Notion link.

    Links are not a stable format (notion.so and app.notion.com both exist), so only the ID in
    the path counts. The `?v=` view ID and the `?p=` peek ID in the query string are ignored.
    Raises ValueError when no ID can be found.
    """
    text = value.strip()
    if not text:
        raise ValueError("empty Notion ID")
    if _UUID.fullmatch(text):
        return _canonical(text.replace("-", ""))
    if _HEX32.fullmatch(text):
        return _canonical(text)
    if "://" not in text:
        raise ValueError("not a Notion ID or link")
    segments = [segment for segment in urlsplit(text).path.split("/") if segment]
    for segment in reversed(segments):
        match = _UUID_AT_END.search(segment) or _HEX32_AT_END.search(segment)
        if match:
            return _canonical(match.group(0).replace("-", ""))
    raise ValueError("the link contains no Notion ID")


def compact_id(notion_id: str) -> str:
    """32 hex characters without dashes (used inside idempotency keys)."""
    return normalize_notion_id(notion_id).replace("-", "")
