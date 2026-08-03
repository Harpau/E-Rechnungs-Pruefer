from decimal import Decimal

import pytest

from app.xml_utils import decimal_string, decimal_value, money_string, xml_decimal_value


def test_decimal_value_distinguishes_zero_from_missing_and_invalid_values():
    assert decimal_value("0") == Decimal("0")
    assert decimal_value(None) is None
    assert decimal_value("") is None
    assert decimal_value("kein Betrag") is None


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "+Infinity", "-Infinity"])
def test_decimal_value_rejects_non_finite_numbers(value):
    assert decimal_value(value) is None


def test_decimal_value_keeps_tolerant_input_normalization_for_display_helpers():
    assert decimal_value("1 234,50") == Decimal("1234.50")
    assert decimal_value("1e2") == Decimal("1E+2")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", Decimal("0")),
        ("-12.50", Decimal("-12.50")),
        ("+3", Decimal("3")),
        (".5", Decimal("0.5")),
        ("5.", Decimal("5")),
        (" 1.25 ", Decimal("1.25")),
    ],
)
def test_xml_decimal_value_accepts_xml_schema_decimal_lexical_space(value, expected):
    assert xml_decimal_value(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "NaN",
        "Infinity",
        "-Infinity",
        "1,5",
        "1e2",
        "1 2",
        "kein Betrag",
    ],
)
def test_xml_decimal_value_rejects_values_outside_xml_schema_decimal_lexical_space(value):
    assert xml_decimal_value(value) is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_rendering_rejects_non_finite_numbers(value):
    assert decimal_string(value) is None
    assert money_string(value) is None


def test_money_string_does_not_raise_for_an_unrepresentable_exponent():
    assert money_string("1e999999") is None
