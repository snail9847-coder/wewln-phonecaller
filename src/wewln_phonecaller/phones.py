"""Phone number normalization to E.164 (library: phonenumbers, Apache-2.0)."""

from __future__ import annotations

from dataclasses import dataclass

import phonenumbers

SUPPORTED_REGIONS = frozenset(phonenumbers.SUPPORTED_REGIONS)


@dataclass(frozen=True)
class PhoneResult:
    e164: str | None
    # None when valid; otherwise "missing", "needs_country_code" or "invalid".
    error: str | None

    @property
    def valid(self) -> bool:
        return self.e164 is not None


PHONE_ERROR_TEXT_RU = {
    "missing": "Не указан номер телефона",
    "needs_country_code": "Номер без кода страны: укажите его в формате +994… "
    "или задайте WEWLN_DEFAULT_PHONE_REGION",
    "invalid": "Номер телефона не распознан",
}


def normalize_phone(raw: str | None, default_region: str | None = None) -> PhoneResult:
    """International numbers (+…) always work; local ones need `default_region` (e.g. "AZ")."""
    text = (raw or "").strip()
    if not text:
        return PhoneResult(None, "missing")
    if text.startswith("00"):
        text = "+" + text[2:]
    if not text.startswith("+") and default_region is None:
        return PhoneResult(None, "needs_country_code")
    try:
        number = phonenumbers.parse(text, None if text.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        return PhoneResult(None, "invalid")
    if not phonenumbers.is_valid_number(number):
        return PhoneResult(None, "invalid")
    e164 = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    return PhoneResult(e164, None)


# Number kinds that are never dialled: they may cost the caller money (premium rate, shared cost)
# or do not reach a person (pager, voicemail access, universal access numbers).
BLOCKED_NUMBER_KINDS = frozenset({"premium_rate", "shared_cost", "pager", "voicemail", "uan"})

_KIND_NAMES = {
    getattr(phonenumbers.PhoneNumberType, name): name.lower()
    for name in dir(phonenumbers.PhoneNumberType)
    if name.isupper()
}


@dataclass(frozen=True)
class NumberInfo:
    """What libphonenumber metadata says about a number. It describes the format only: a valid
    number may be unassigned, switched off or unreachable, and its country code says nothing
    about permission to call it."""

    e164: str
    region: str | None
    calling_code: int
    kind: str


def describe_number(e164: str) -> NumberInfo | None:
    """Region (RU or KZ for +7, AZ for +994 …) and kind of a normalized number; None if invalid."""
    try:
        number = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    kind = _KIND_NAMES.get(phonenumbers.number_type(number), "unknown")
    return NumberInfo(
        e164=phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164),
        region=phonenumbers.region_code_for_number(number),
        calling_code=int(number.country_code or 0),
        kind=kind,
    )


def dial_policy_problem(
    e164: str | None, allowed_regions: frozenset[str] = frozenset()
) -> str | None:
    """Why a number must not be dialled by policy, or None. Checks format and policy only.

    - "invalid": not a valid E.164 number;
    - "number_type_not_allowed": premium rate, shared cost and similar kinds;
    - "region_not_allowed": `allowed_regions` is set and the number's region is not in it.
    """
    info = describe_number(e164) if e164 else None
    if info is None:
        return "invalid"
    if info.kind in BLOCKED_NUMBER_KINDS:
        return "number_type_not_allowed"
    if allowed_regions and info.region not in allowed_regions:
        return "region_not_allowed"
    return None
