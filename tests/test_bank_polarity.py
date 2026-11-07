"""Bank polarity evidence must never manufacture an incoming receipt.

All files contain fake records. These checks follow parsed observations into
SQLite matching and real follow-up rendering, including the positive controls.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest

from cashdesk.chasing import plan_followups
from cashdesk.importers import read_bank_csv
from cashdesk.model import Customer, Invoice
from cashdesk.store import ConflictError, Store

D = Decimal
TODAY = dt.date(2026, 11, 5)
NOW = dt.datetime(2026, 11, 5, 10, tzinfo=dt.timezone.utc)


def statement(*money_rows):
    """Semicolon CSV retains comma decimals and accounting notation literally."""
    headers = ["id", "date", "currency", "reference", "counterparty"]
    for row in money_rows:
        headers.extend(key for key in row if key not in headers)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=headers, delimiter=";", lineterminator="\n")
    writer.writeheader()
    for index, row in enumerate(money_rows, 1):
        writer.writerow({"id": f"B{index}", "date": "2026-11-04", "currency": "EUR",
                         "reference": "INV-100", "counterparty": "Alpha Workshop", **row})
    return stream.getvalue()


CONFLICTS = [
    pytest.param({"Amount": "EUR 1200 USD"}, id="two-inline-currencies"),
    pytest.param({"Credit": "€1200 USD"}, id="symbol-and-code-disagree"),
    pytest.param({"Amount": "1200.00", "Value": "-1200.00"}, id="contradictory-amount-aliases"),
    pytest.param({"Credit": "1200.00", "Money in": "1199.00"}, id="contradictory-credit-aliases"),
    pytest.param({"Debit": "1200.00", "Money out": "1199.00"}, id="contradictory-debit-aliases"),
    pytest.param({"Amount": "1200.00", "Direction": "CRDT", "DR/CR": "DR"}, id="contradictory-direction-aliases"),
    pytest.param({"Amount": "1200.00", "Credit": "USD 1200.00"}, id="inline-currency-conflict"),
    pytest.param({"Amount (EUR)": "1200.00", "Credit (USD)": "1200.00"}, id="header-currency-conflict"),
    pytest.param({"Amount": "USD 1200.00"}, id="row-currency-conflict"),
    pytest.param({"Amount": "+EUR-1200.00"}, id="opposing-explicit-signs"),
    pytest.param({"Amount": "+1200.00-"}, id="plus-trailing-minus"),
    pytest.param({"Credit": "-1200.00"}, id="negative-credit"),
    pytest.param({"Credit": "(1,200.00)"}, id="parenthesized-credit"),
    pytest.param({"Credit": "1.200,00-"}, id="trailing-minus-credit"),
    pytest.param({"Debit": "-1200.00"}, id="negative-debit"),
    pytest.param({"Debit": "(1.200,00)"}, id="parenthesized-debit"),
    pytest.param({"Debit": "1200.00-"}, id="trailing-minus-debit"),
    pytest.param({"Credit": "1200.00", "CdtDbtInd": "DBIT"}, id="credit-marked-debit"),
    pytest.param({"Debit": "1200.00", "CdtDbtInd": "CRDT"}, id="debit-marked-credit"),
    pytest.param({"Amount": "-1200.00", "CdtDbtInd": "CRDT"}, id="negative-marked-credit"),
    pytest.param({"Amount": "+1200.00", "CdtDbtInd": "DBIT"}, id="explicit-plus-marked-debit"),
    pytest.param({"Amount": "1200.00", "Debit": "1200.00"}, id="signed-amount-vs-debit"),
    pytest.param({"Amount": "-1200.00", "Credit": "1200.00"}, id="negative-amount-vs-credit"),
    pytest.param({"Amount": "1200.00", "Credit": "1199.00"}, id="amount-vs-credit-value"),
    pytest.param({"Amount": "-1200.00", "Debit": "1199.00"}, id="amount-vs-debit-value"),
    pytest.param({"Amount": "0.00", "Credit": "1200.00"}, id="zero-amount-vs-credit"),
    pytest.param({"Amount": "1200.00", "Credit": "0.00", "Debit": "0.00"}, id="nonzero-amount-vs-zero-columns"),
    pytest.param({"Amount": "1200.00", "Credit": "1200.00", "Debit": "1.00"}, id="both-columns-behind-amount"),
    pytest.param({"Amount": "1200.00", "Credit": "-1200.00"}, id="negative-credit-behind-amount"),
    pytest.param({"Amount": "-1200.00", "Debit": "-1200.00"}, id="negative-debit-behind-amount"),
    pytest.param({"Amount": "1200.00", "Credit": "1200.00", "CdtDbtInd": "DBIT"}, id="amount-direction-vs-credit"),
]


@pytest.mark.parametrize("money", CONFLICTS)
def test_bank_amount_evidence_conflicts(money):
    result = read_bank_csv(statement(money))
    assert not result.ok, "contradictory evidence must not become usable bank cash"
    assert result.records == ()
    assert len(result.errors) == 1 and result.errors[0].row == 2
    if result.errors[0].code == "bank_currency_conflict":
        assert "bank currency evidence conflicts" in result.errors[0].message
        assert "re-export" in result.errors[0].message
    else:
        assert "bank amount evidence conflicts" in result.errors[0].message
        assert "check the bank export" in result.errors[0].message


@pytest.mark.parametrize("money", [
    {"Amount": "1200.00", "Credit": "not a number"},
    {"Amount": "-1200.00", "Debit": "not a number"},
    {"Amount": "1200.00", "Credit": "1200.00", "Debit": "broken"},
])
def test_bank_populated_money_columns_cannot_be_ignored(money):
    result = read_bank_csv(statement(money))
    assert not result.ok and result.records == (), "a valid Amount cannot hide corrupt money evidence"
    assert result.errors[0].row == 2


CONTROLS = [
    pytest.param({"Amount": "EUR 1200 EUR"}, "1200.00", id="repeated-same-currency"),
    pytest.param({"Amount": "1200.00"}, "1200.00", id="signed-incoming"),
    pytest.param({"Amount": "-1200.00"}, "-1200.00", id="signed-outgoing"),
    pytest.param({"Amount": "(1,200.00)"}, "-1200.00", id="accounting-parentheses-refund"),
    pytest.param({"Amount": "1.200,00-"}, "-1200.00", id="comma-trailing-minus-refund"),
    pytest.param({"Amount": "1 200,00"}, "1200.00", id="decimal-comma-incoming"),
    pytest.param({"Amount": "1200.00", "CdtDbtInd": "DBIT"}, "-1200.00", id="unsigned-explicit-debit"),
    pytest.param({"Amount": "(1,200.00)", "CdtDbtInd": "DBIT"}, "-1200.00", id="negative-explicit-debit"),
    pytest.param({"Amount": "+1200.00", "CdtDbtInd": "CRDT"}, "1200.00", id="explicit-plus-credit"),
    pytest.param({"Credit": "1200.00", "Debit": ""}, "1200.00", id="credit-only"),
    pytest.param({"Debit": "1200.00", "Credit": ""}, "-1200.00", id="debit-only"),
    pytest.param({"Credit": "1.200,00", "Debit": "0.00", "CdtDbtInd": "CRDT"}, "1200.00", id="credit-zero-debit"),
    pytest.param({"Debit": "1.200,00", "Credit": "0.00", "CdtDbtInd": "DBIT"}, "-1200.00", id="debit-zero-credit"),
    pytest.param({"Amount": "1200.00", "Credit": "1200.00"}, "1200.00", id="corroborated-incoming"),
    pytest.param({"Amount": "-1200.00", "Debit": "1200.00"}, "-1200.00", id="corroborated-outgoing"),
    pytest.param({"Amount": "1200.00", "Debit": "1200.00", "CdtDbtInd": "DBIT"}, "-1200.00", id="corroborated-unsigned-debit"),
    pytest.param({"Amount": "", "Credit": "1200.00", "CdtDbtInd": "CRDT"}, "1200.00", id="blank-amount-credit"),
    pytest.param({"Amount": "1200.00", "Credit": "", "Debit": ""}, "1200.00", id="blank-side-columns"),
    pytest.param({"Amount": "0.00", "Credit": "0.00", "Debit": "0.00", "CdtDbtInd": "DBIT"}, "0.00", id="zero-debit"),
]


@pytest.mark.parametrize("money,expected", CONTROLS)
def test_bank_consistent_amount_evidence_preserves_signed_cash(money, expected):
    result = read_bank_csv(statement(money))
    assert result.ok, result.errors
    assert len(result.records) == 1
    assert result.records[0].amount == D(expected)
    assert result.records[0].currency == "EUR"


@pytest.fixture
def ledger():
    with Store(":memory:", today=lambda: TODAY) as store:
        store.upsert_customers([Customer("C1", "Alpha Workshop", email="ap@alpha.example")], at=NOW)
        store.upsert_invoices([Invoice("INV-100", "C1", dt.date(2026, 10, 1), dt.date(2026, 10, 29),
                                      D("1200.00"), "EUR", D("1200.00"), "open", "INV-100")], at=NOW)
        yield store


def match_and_draft(store):
    matched = store.match_result(TODAY)
    for match in matched.applied:
        store.apply_match(match, at=NOW)
    plan = plan_followups(store.list_invoices(), store.list_customers(), store.verified_matches(),
                          store.chase_state(), TODAY, sender="Example Studio",
                          holds=store.reminder_holds(TODAY), created_at=NOW)
    store.create_drafts(plan.all_drafts, at=NOW)
    return matched, plan


@pytest.mark.parametrize("money", CONFLICTS)
def test_conflicting_bank_evidence_never_pays_or_thanks(ledger, money):
    before = "\n".join(ledger._conn.iterdump())
    result = read_bank_csv(statement(money))
    with pytest.raises(ConflictError, match="row error"):
        ledger.ingest(result, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before, "default refusal must be atomic"
    matched, plan = match_and_draft(ledger)
    assert matched.applied == ()
    assert ledger.list_payments() == ledger.list_matches() == []
    invoice = ledger.get_invoice("INV-100")
    assert invoice.status == "open" and invoice.balance == D("1200.00")
    assert plan.thanks == ()
    assert len(plan.drafts) == 1 and "1,200.00" in plan.drafts[0].body
    assert all(draft.kind != "thanks" for draft in ledger.list_drafts())


@pytest.mark.parametrize("money,expected", CONTROLS)
def test_consistent_bank_evidence_reaches_matching_and_drafting(ledger, money, expected):
    ledger.ingest(read_bank_csv(statement(money)), at=NOW)
    matched, plan = match_and_draft(ledger)
    invoice = ledger.get_invoice("INV-100")
    if D(expected) > 0:
        assert len(matched.applied) == len(ledger.list_matches()) == 1
        assert invoice.balance == 0 and invoice.status == "paid"
        assert len(plan.thanks) == 1 and plan.drafts == ()
    else:
        assert matched.applied == () and ledger.list_matches() == []
        assert invoice.balance == D("1200.00") and invoice.status == "open"
        assert plan.thanks == (), "outgoing refunds and zero rows are never money received"


def test_mixed_bank_file_refuses_atomically_and_explicit_partial_import_cannot_import_conflict(ledger):
    result = read_bank_csv(statement({"Credit": "1200.00", "CdtDbtInd": "CRDT"},
                                    {"Credit": "1200.00", "CdtDbtInd": "DBIT"}))
    assert [record.id for record in result.records] == ["B1"]
    assert [error.row for error in result.errors] == [3]
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError, match="row error"):
        ledger.ingest(result, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before
    assert ledger.ingest(result, at=NOW, allow_errors=True)["payments"] == 1
    matched, plan = match_and_draft(ledger)
    assert [payment.id for payment in ledger.list_payments()] == ["B1"]
    assert len(matched.applied) == 1 and len(plan.thanks) == 1
    assert ledger.get_invoice("INV-100").balance == 0


@pytest.mark.parametrize("headers,values", [
    ("Amount;Amount", "-1200.00;1200.00"),
    ("Credit;credit", "-1200.00;1200.00"),
    ("Direction;direction;Amount", "DBIT;CRDT;1200.00"),
    ("Amount;Amount", "1200.00;1200.00"),
])
def test_duplicate_normalized_bank_headers_refuse_before_overwrite(headers, values):
    text = f"id;date;currency;{headers}\nB1;2026-11-04;EUR;{values}\n"
    result = read_bank_csv(text)
    assert result.records == () and result.errors
    assert "bank amount evidence conflicts" in result.errors[0].message
    assert "check the bank export" in result.errors[0].message


def test_consistent_bank_aliases_and_inline_currency_are_supported():
    result = read_bank_csv(statement({"Amount (EUR)": "EUR 1200.00", "Value": "1200.00",
                                     "Credit": "1200.00", "Money in": "1200.00",
                                     "Direction": "CRDT", "DR/CR": "CR"}))
    assert result.ok, result.errors
    assert len(result.records) == 1 and result.records[0].amount == D("1200.00")
    assert result.records[0].currency == "EUR"
