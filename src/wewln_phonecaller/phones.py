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
