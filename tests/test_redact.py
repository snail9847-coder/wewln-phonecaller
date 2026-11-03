import pytest

from wewln_phonecaller.redact import (
    MASK,
    mask_phone_numbers,
    mask_secrets,
    redact,
    redact_value,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+79161234567", "+79*******67"),
        ("+7 916 123-45-67", "+79*******67"),
        ("+7 (916) 123-45-67", "+79*******67"),
        ("8 (916) 123-45-67", "89*******67"),
        ("89161234567", "89*******67"),
        ("+994 50 123 45 67", "+99********67"),
        ("Звонок на +79161234567 завершён", "Звонок на +79*******67 завершён"),
        ("тел.89161234567", "тел.89*******67"),
        ("a: +79161234567, b: 8 916 765 43 21", "a: +79*******67, b: 89*******21"),
    ],
)
def test_phone_numbers_are_masked(text, expected):
    assert mask_phone_numbers(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-11-03",
        "2026-11-03 15:32:26",
        "2026-11-03T15:32:26.123",
        "127.0.0.1:8787",
        "192.168.100.200",
        "attempt 3 of 5",
        "id 123456789",
        "+12345",
        "call_id=CA1234567890abcdef",
    ],
)
def test_non_phone_text_is_left_alone(text):
    assert mask_phone_numbers(text) == text


def test_phone_next_to_a_date_is_still_masked():
    assert mask_phone_numbers("2026-11-03 89161234567") == "2026-11-03 89*******67"


def test_known_secret_values_are_masked():
    secret = "abcdefgh12345678"
    assert mask_secrets(f"token={secret}", [secret]) == f"token={MASK}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abc.def-123_XYZ", f"Authorization: Bearer {MASK}"),
        ("ntn_" + "a1" * 12, MASK),
        ("secret_" + "b2" * 12, MASK),
    ],
)
def test_token_patterns_are_masked(text, expected):
    assert mask_secrets(text) == expected


def test_secret_digits_are_not_half_masked_as_phone():
    secret = "9161234567890123"
    assert redact(f"key {secret}", [secret]) == f"key {MASK}"


def test_redact_value_walks_nested_structures():
    value = {"to": "+79161234567", "list": ["89161234567", 3], "n": None, "ok": True}
    assert redact_value(value) == {
        "to": "+79*******67",
        "list": ["89*******67", 3],
        "n": None,
        "ok": True,
    }
