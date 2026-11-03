"""Masking of phone numbers and secrets before anything reaches logs or the audit trail."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MASK = "***"

# A phone-like run: optional "+", then digits with spaces, dashes or brackets in between.
# ASCII word characters around it mean it is part of an identifier, not a phone number.
_PHONE_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_+])\+?\d[\d \-()]{5,22}\d(?![A-Za-z0-9_])")
_DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?")
_PLACEHOLDER = "\x00"
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:ntn|secret)_[A-Za-z0-9]{16,}\b"),
)


def _mask_candidate(candidate: str) -> str:
    digits = re.sub(r"\D", "", candidate)
    has_plus = candidate.startswith("+")
    minimum = 7 if has_plus else 10
    if not minimum <= len(digits) <= 15:
        return candidate
    masked = digits[:2] + "*" * (len(digits) - 4) + digits[-2:]
    return ("+" if has_plus else "") + masked


def mask_phone_numbers(text: str) -> str:
    """Mask phone-like numbers, keeping the first two and the last two digits.

    Dates and times are protected first so that "2026-11-03 15:30" is never mistaken for a number.
    """
    dates = _DATE_TIME.findall(text)
    if dates:
        text = _DATE_TIME.sub(_PLACEHOLDER, text)
    text = _PHONE_CANDIDATE.sub(lambda m: _mask_candidate(m.group(0)), text)
    if dates:
        restored = iter(dates)
        text = re.sub(_PLACEHOLDER, lambda _m: next(restored), text)
    return text


def mask_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secret values and token-looking strings with a fixed mask."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, MASK)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)} {MASK}" if m.lastindex else MASK, text)
    return text


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Secrets first (their digits must not be half-masked as phones), then phone numbers."""
    return mask_phone_numbers(mask_secrets(text, secrets))


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact strings inside nested dicts, lists and tuples; other non-scalars become strings."""
    secrets = tuple(secrets)
    if isinstance(value, str):
        return redact(value, secrets)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {str(k): redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(v, secrets) for v in value]
    return redact(str(value), secrets)
