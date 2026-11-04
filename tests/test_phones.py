"""Number kinds and the dial policy (format and policy only, never reachability)."""

import pytest

from wewln_phonecaller.phones import describe_number, dial_policy_problem


@pytest.mark.parametrize(
    ("number", "region", "kind"),
    [
        ("+994501234567", "AZ", "mobile"),
        ("+994124987654", "AZ", "fixed_line"),
        ("+74951234567", "RU", "fixed_line"),
        ("+77012345678", "KZ", "mobile"),  # +7 is shared by Russia and Kazakhstan
        ("+78001234567", "RU", "toll_free"),
        ("+78091234567", "RU", "premium_rate"),
    ],
)
def test_numbers_are_described(number, region, kind):
    info = describe_number(number)
    assert (info.region, info.kind) == (region, kind)


def test_the_dial_policy():
    assert dial_policy_problem("+994501234567") is None
    assert dial_policy_problem("+994501234567", frozenset({"AZ"})) is None
    assert dial_policy_problem("+77012345678", frozenset({"RU"})) == "region_not_allowed"
    assert dial_policy_problem("+78091234567") == "number_type_not_allowed"
    assert dial_policy_problem("+19005551234") == "number_type_not_allowed"
    assert dial_policy_problem("+7123") == "invalid"
    assert dial_policy_problem(None) == "invalid"
    assert describe_number("not a number") is None
