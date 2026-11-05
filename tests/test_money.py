"""money.py: Decimal amounts in every format the desk sees, house-style output."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cashdesk.money import (
    Amount,
    cs_days,
    format_money,
    format_totals,
    parse_amount,
    quantize,
    sum_by_currency,
)

D = Decimal
NBSP = " "
NNBSP = " "


@pytest.mark.parametrize(
    "text, lang, value, currency",
    [
        ("1,250.00", None, "1250.00", None),
        ("1250.00", None, "1250.00", None),
        ("1250", None, "1250.00", None),
        ("€1,250", None, "1250.00", "EUR"),
        ("€ 1,250.00", None, "1250.00", "EUR"),
        ("1 250,00 Kč", None, "1250.00", "CZK"),
        (f"1{NBSP}250,00{NBSP}Kč", None, "1250.00", "CZK"),
        (f"1{NNBSP}250,00 Kč", None, "1250.00", "CZK"),
        ("1.250,00 €", None, "1250.00", "EUR"),
        ("CZK 1 250", None, "1250.00", "CZK"),
        ("12 500,-", None, "12500.00", None),
        ("12 500,- Kč", None, "12500.00", "CZK"),
        ("1,250,000.50", None, "1250000.50", None),
        ("1.250.000,50", None, "1250000.50", None),
        ("1'250.00", None, "1250.00", None),
        ("-1500.00", None, "-1500.00", None),
        ("EUR -12.50", None, "-12.50", "EUR"),
        ("−12,50 Kč", None, "-12.50", "CZK"),
        ("1250 EUR", None, "1250.00", "EUR"),
        ("EUR1250", None, "1250.00", "EUR"),
        ("1,250.00 euros", None, "1250.00", "EUR"),
        ("1 250 korun", None, "1250.00", "CZK"),
        ("$99.90", None, "99.90", "USD"),
        ("£5", None, "5.00", "GBP"),
        ("1.250", "en", "1.25", None),
        ("1.250", "cs", "1250.00", None),
        ("1,250", "en", "1250.00", None),
        ("1,250", "cs", "1.25", None),
        ("1,5", "en", "1.50", None),
        ("1.5", "cs", "1.50", None),
        ("0.00", None, "0.00", None),
    ],
)
def test_parse_amount_formats(text, lang, value, currency):
    assert parse_amount(text, lang) == Amount(D(value), currency)


@pytest.mark.parametrize("text", ["", "   ", "abc", "2026-11-05", "11/05/2026", "7 days", "INV-2026-0042", "1,2,3", "1..5"])
def test_parse_amount_rejects_non_amounts(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize(
    "text, lang, value",
    [
        ("1250.500", None, "1250.50"),
        ("1250.500", "cs", "1250.50"),
        ("1250,500", "en", "1250.50"),
        ("12500.000", None, "12500.00"),
        ("1,250.500", None, "1250.50"),
        ("1.250.000", None, "1250000.00"),
        ("100.000", "cs", "100000.00"),
        ("100.000", "en", "100.00"),
        ("0.500", None, "0.50"),
        ("0,500", None, "0.50"),
        ("1250.5000", None, "1250.50"),
        ("833.3300", None, "833.33"),
    ],
)
def test_three_digits_after_a_long_group_are_decimals(text, lang, value):
    """A separator before three digits is thousands only after a group of one to three digits."""
    assert parse_amount(text, lang) == Amount(D(value), None)


@pytest.mark.parametrize("text", ["1250.505", "1250.005", "1,250.505", "833.3333", "1.2345"])
def test_more_than_two_non_zero_decimals_are_refused_when_strict(text):
    assert parse_amount(text, strict=True) is None


def test_more_decimals_round_half_up_when_not_strict():
    assert parse_amount("1250.505") == Amount(D("1250.51"), None), "a decimal point, never 1,250,505"
    assert parse_amount("1.005", "en") == Amount(D("1.01"), None)
    assert parse_amount("1250.500", strict=True) == Amount(D("1250.50"), None)


@pytest.mark.parametrize(
    "text, value, currency",
    [
        ("1.200,00-", "-1200.00", None),
        ("1 200,00 -", "-1200.00", None),
        ("1200.00- EUR", "-1200.00", "EUR"),
        ("(1,200.00)", "-1200.00", None),
        ("(EUR 50.00)", "-50.00", "EUR"),
        ("12 500,-", "12500.00", None),
    ],
)
def test_trailing_minus_and_parentheses_are_negative(text, value, currency):
    assert parse_amount(text) == Amount(D(value), currency)


@pytest.mark.parametrize("text", ["(1,200.00", "1,200.00)", "-1.200,00-", "(-5.00)", "1250.000.000"])
def test_unbalanced_or_doubled_signs_are_refused(text):
    assert parse_amount(text) is None


def test_parse_amount_is_decimal_not_float():
    amount = parse_amount("0.10")
    assert amount is not None
    assert isinstance(amount.value, Decimal)
    assert amount.value + parse_amount("0.20").value == D("0.30")


def test_quantize_rounds_half_up_to_cents():
    assert quantize(D("1.005")) == D("1.01")
    assert quantize("2") == D("2.00")
    assert str(quantize(D("1250"))) == "1250.00"


@pytest.mark.parametrize(
    "amount, currency, lang, expected",
    [
        ("1200", "EUR", "en", "EUR 1,200.00"),
        ("24200", "CZK", "en", "CZK 24,200.00"),
        ("-12.5", "EUR", "en", "EUR -12.50"),
        ("1200", "CZK", "cs", f"1{NBSP}200,00{NBSP}Kč"),
        ("500", "EUR", "cs", f"500,00{NBSP}EUR"),
        ("1234567.5", "CZK", "cs", f"1{NBSP}234{NBSP}567,50{NBSP}Kč"),
        ("-12.5", "CZK", "cs", f"-12,50{NBSP}Kč"),
    ],
)
def test_format_money_house_style(amount, currency, lang, expected):
    assert format_money(D(amount), currency, lang) == expected


def test_format_money_round_trips_through_parse_amount():
    for lang in ("en", "cs"):
        for currency in ("EUR", "CZK"):
            text = format_money(D("1250.00"), currency, lang)
            assert parse_amount(text, lang) == Amount(D("1250.00"), currency)


def test_sum_by_currency_never_adds_across_currencies():
    totals = sum_by_currency([("EUR", D("1")), ("CZK", D("2")), ("EUR", D("3.5")), ("czk", D("0.5"))])
    assert totals == {"EUR": D("4.50"), "CZK": D("2.50")}


def test_format_totals_lists_each_currency():
    totals = {"EUR": D("4"), "CZK": D("2")}
    assert format_totals(totals, "en") == "CZK 2.00 + EUR 4.00"
    assert format_totals(totals, "cs") == f"2,00{NBSP}Kč + 4,00{NBSP}EUR"
    assert format_totals({}, "en") == "-"


@pytest.mark.parametrize("n, expected", [(1, "1 den"), (2, "2 dny"), (4, "4 dny"), (5, "5 dní"), (14, "14 dní"), (22, "22 dní")])
def test_cs_days(n, expected):
    assert cs_days(n) == expected
