"""model.py: frozen records with Decimal money and validated enumerations."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from cashdesk.model import Customer, Draft, Figure, Invoice, Match, Payment

D = Decimal


def test_invoice_quantises_money_and_normalises_currency_and_status():
    inv = Invoice("INV-1", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 31), "1250", "eur", D("1250.5"), "Open")
    assert inv.amount == D("1250.00") and isinstance(inv.amount, Decimal)
    assert inv.balance == D("1250.50")
    assert inv.currency == "EUR"
    assert inv.status == "open"


def test_money_rounds_half_up_like_parse_amount():
    from cashdesk.money import parse_amount

    inv = Invoice("INV-1", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 31), "1.005", "EUR", "2.675")
    assert inv.amount == D("1.01") and inv.balance == D("2.68")
    assert Figure.money(D("1.005"), "EUR").canonical == "EUR 1.01" == Figure.money(parse_amount("1.005", "en").value, "EUR").canonical


def test_invoice_rejects_unknown_status_and_float_money():
    with pytest.raises(ValueError):
        Invoice("INV-1", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 31), D("1"), "EUR", D("1"), "overdue")
    with pytest.raises(TypeError):
        Invoice("INV-1", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 31), 1.0, "EUR", D("1"))


def test_invoice_days_overdue_and_open_flags():
    inv = Invoice("INV-1", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 27), D("100"), "EUR", D("100"))
    assert inv.days_overdue(dt.date(2026, 11, 10)) == 14
    assert inv.days_overdue(dt.date(2026, 10, 20)) == -7
    assert inv.is_open and not inv.is_void and not inv.is_paid
    void = Invoice("INV-2", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 27), D("100"), "EUR", D("100"), "void")
    assert void.is_void and not void.is_open
    paid = Invoice("INV-3", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 27), D("100"), "EUR", D("0"), "paid")
    assert paid.is_paid and not paid.is_open
    disputed = Invoice("INV-4", "c1", dt.date(2026, 10, 1), dt.date(2026, 10, 27), D("100"), "EUR", D("100"), "disputed")
    assert disputed.is_open and disputed.is_disputed


def test_customer_language_defaults_and_validation():
    assert Customer("c1", "Acme").language == "en"
    assert Customer("c1", "Acme", language="CS").language == "cs"
    with pytest.raises(ValueError):
        Customer("c1", "Acme", language="de")
    assert Customer("c1", "Acme", contact_name="Jana Vzorová").first_name == "Jana"
    assert Customer("c1", "Acme").first_name == ""


def test_payment_direction_and_search_text():
    p = Payment("B1", dt.date(2026, 11, 6), D("-50"), "eur", reference="Refund INV-1", memo="dobropis")
    assert p.is_outgoing and p.currency == "EUR" and p.amount == D("-50.00")
    assert "Refund INV-1" in p.text and "dobropis" in p.text


def test_match_and_draft_defaults():
    m = Match("B1", "INV-1", D("10"), "EUR", "reference+exact", "high")
    assert m.amount == D("10.00")
    d = Draft("d1", "stage07", "c1", ("INV-1",), "en", "Subject", "Body", ("a@b.example",), dt.datetime(2026, 11, 10, 7, 0))
    assert d.status == "pending" and d.hash == "" and d.figures == ()
    with pytest.raises(ValueError):
        Draft("d1", "stage99", "c1", ("INV-1",), "en", "S", "B", (), dt.datetime(2026, 11, 10))


def test_figure_equality_ignores_position_and_raw_text():
    assert Figure("money", "EUR 1250.00", raw="€1,250.00", line=3) == Figure("money", "EUR 1250.00")
    assert Figure("money", "EUR 1250.00") != Figure("money", "CZK 1250.00")
    assert len({Figure("iban", "DE89370400440532013000", raw="x"), Figure("iban", "DE89370400440532013000")}) == 1
