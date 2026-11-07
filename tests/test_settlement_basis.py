"""Imported settlements and local allocations must remain separate accounting facts."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from cashdesk.approval import with_hash
from cashdesk.importers import read_invoices
from cashdesk.model import Customer, Draft, Invoice, Match, Payment
from cashdesk.store import ConflictError, Store

D = Decimal
TODAY = dt.date(2026, 11, 10)
NOW = dt.datetime(2026, 11, 10, 7, tzinfo=dt.timezone.utc)
RECONCILE = "settlement reconciliation required"


def invoice(amount="1000", balance="600", status="open", **changes):
    inv = Invoice("INV-S", "C1", dt.date(2026, 10, 1), dt.date(2026, 10, 20),
                  D(amount), "EUR", D(balance), status, "INV-S")
    return replace(inv, **changes)


def seed(store, inv=None):
    store.upsert_customers([Customer("C1", "Settlement Client", email="ap@client.example"),
                            Customer("C2", "Other Client", email="ap@other.example")], at=NOW)
    store.upsert_invoices([inv or invoice()], at=NOW)


def apply_local(store, amount, pid="P1"):
    store.add_payments([Payment(pid, TODAY, D(amount), "EUR", reference="INV-S")], at=NOW)
    assert store.apply_match(Match(pid, "INV-S", D(amount), "EUR", "person", "person"), at=NOW)


def reminder(store, did="reminder"):
    inv = store.get_invoice("INV-S")
    draft = with_hash(Draft(did, "stage14", "C1", ("INV-S",), "en", "Invoice reminder",
                            f"Please pay EUR {inv.balance:.2f} for INV-S.",
                            ("ap@client.example",), NOW))
    store.create_draft(draft, at=NOW)
    return draft


@pytest.fixture
def store():
    with Store(":memory:", today=lambda: TODAY) as value:
        seed(value)
        yield value


def test_initial_imported_partial_settlement_survives_local_closure_and_stale_export(store):
    apply_local(store, "600")
    paid_date = store.get_invoice("INV-S").paid_date
    for note in ("first stale export", "same figures again"):
        store.upsert_invoices([invoice(note=note)], at=NOW)
        inv = store.get_invoice("INV-S")
        assert (inv.balance, inv.status, inv.paid_date) == (D("0"), "paid", paid_date)
    assert store.reminder_holds(TODAY).get("INV-S") is None


def test_multiple_local_allocations_reduce_only_the_initial_open_debt(store):
    apply_local(store, "100", "P1")
    store.upsert_invoices([invoice(note="unreconciled")], at=NOW)
    assert store.get_invoice("INV-S").balance == D("500")
    apply_local(store, "200", "P2")
    for _ in range(2):
        store.upsert_invoices([invoice(note="still unreconciled")], at=NOW)
        assert store.get_invoice("INV-S").balance == D("300")
    assert len(store.list_matches()) == 2


@pytest.mark.parametrize("amount,balance,expected", [("1300", "900", "600"), ("900", "500", "200")])
def test_gross_correction_preserves_absolute_external_and_local_settlements(store, amount, balance, expected):
    apply_local(store, "300")
    for _ in range(2):
        store.upsert_invoices([invoice(amount, balance)], at=NOW)
        assert store.get_invoice("INV-S").balance == D(expected)
        assert "INV-S" not in store.reminder_holds(TODAY)


def test_no_local_external_settlement_correction_requires_verification(store):
    store.upsert_invoices([invoice(balance="700")], at=NOW)
    assert store.get_invoice("INV-S").balance == D("600")
    assert "settlement reconciliation required" in store.reminder_holds(TODAY)["INV-S"]
    store.reconcile_settlement("INV-S", "300", expected_balance="600", currency="EUR",
                               expected_snapshot=store.settlement_snapshot("INV-S"), by="Ada")
    assert store.get_invoice("INV-S").balance == D("700")
    apply_local(store, "100")
    store.upsert_invoices([invoice(balance="700", note="unchanged accounting")], at=NOW)
    assert store.get_invoice("INV-S").balance == D("600")


def test_paid_import_is_not_reopened_by_old_export_even_without_local_matches(store):
    paid = dt.date(2026, 11, 8)
    store.upsert_invoices([invoice(balance="0", status="paid", paid_date=paid)], at=NOW)
    store.upsert_invoices([invoice(note="older open export")], at=NOW)
    inv = store.get_invoice("INV-S")
    assert (inv.balance, inv.status, inv.paid_date) == (D("0"), "paid", paid)


def test_paid_export_does_not_count_reflected_local_payment_twice(store):
    apply_local(store, "100")
    paid = invoice(balance="0", status="paid", paid_date=TODAY)
    for _ in range(2):
        store.upsert_invoices([paid], at=NOW)
        assert store.get_invoice("INV-S").balance == 0
    # Export settled 1000 in total, of which 100 is already a local allocation.
    store.upsert_invoices([invoice("1200", "200")], at=NOW)
    assert store.get_invoice("INV-S").balance == D("200")
    assert store.get_invoice("INV-S").status == "open"
    assert store.get_invoice("INV-S").paid_date is None


@pytest.mark.parametrize("export_balance", ["500", "400", "200"])
def test_changed_positive_settlement_with_local_matches_holds_existing_approval(store, export_balance):
    apply_local(store, "100")
    draft = reminder(store)
    store.approve_draft(draft.id, draft.hash, at=NOW)
    store.upsert_invoices([invoice(balance=export_balance)], at=NOW)
    assert RECONCILE in store.reminder_holds(TODAY)["INV-S"].lower()
    assert store.approval(draft.id) is None
    with pytest.raises(ConflictError, match=RECONCILE):
        store.approve_draft(draft.id, draft.hash, at=NOW)
    with pytest.raises(ConflictError, match=RECONCILE):
        store.reserve_send(draft.id, draft.hash, "eml", at=NOW)
    assert store.list_sends() == []


@pytest.mark.parametrize("changes", [{"currency": "USD"}, {"customer_id": "C2"}])
def test_locally_allocated_invoice_identity_change_cannot_silently_move_money(store, changes):
    apply_local(store, "100")
    before = store.get_invoice("INV-S")
    try:
        store.upsert_invoices([invoice(**changes)], at=NOW)
    except ConflictError:
        assert store.get_invoice("INV-S") == before
    else:
        assert RECONCILE in store.reminder_holds(TODAY)["INV-S"].lower()
    assert store.list_matches()[0].currency == "EUR"


def test_explicit_reconciliation_records_verified_external_cash_and_needs_fresh_approval(store):
    apply_local(store, "100")
    draft = reminder(store)
    store.approve_draft(draft.id, draft.hash, at=NOW)
    store.upsert_invoices([invoice(balance="500")], at=NOW)
    balance = store.get_invoice("INV-S").balance
    store.reconcile_settlement("INV-S", D("500"), expected_balance=balance, currency="EUR", expected_snapshot=store.settlement_snapshot("INV-S"), by="Ada")
    assert store.get_invoice("INV-S").balance == D("400")
    assert "INV-S" not in store.reminder_holds(TODAY)
    assert len(store.list_matches()) == 1
    assert store.approval(draft.id) is None
    with pytest.raises(ConflictError):
        store.approve_draft(draft.id, draft.hash, at=NOW)
    fresh = reminder(store, "fresh-reminder")
    store.approve_draft(fresh.id, fresh.hash, at=NOW)
    assert store.approval(fresh.id) is not None
    audit = [event for event in store.events() if event.ref == "INV-S" and "reconcil" in event.kind]
    assert audit and "Ada" in str(audit[-1].detail)


@pytest.mark.parametrize("external,expected,currency", [
    ("400", "499", "EUR"), ("400", "500", "USD"),
    ("-1", "500", "EUR"), ("1001", "500", "EUR"),
    ("NaN", "500", "EUR"), ("Infinity", "500", "EUR"),
])
def test_explicit_reconciliation_refuses_invalid_or_stale_basis_without_mutation(store, external, expected, currency):
    apply_local(store, "100")
    before, matches, events = store.get_invoice("INV-S"), store.list_matches(), store.events()
    with pytest.raises((ConflictError, ValueError)):
        store.reconcile_settlement("INV-S", D(external), expected_balance=D(expected), currency=currency, expected_snapshot=store.settlement_snapshot("INV-S"), by="Ada")
    assert store.get_invoice("INV-S") == before
    assert store.list_matches() == matches
    assert store.events() == events


def test_verified_reconciliation_keeps_local_allocations_when_total_exceeds_gross(store):
    apply_local(store, "600")
    store.reconcile_settlement("INV-S", D("700"), expected_balance=D("0"), currency="EUR", expected_snapshot=store.settlement_snapshot("INV-S"), by="Ada")
    assert store.get_invoice("INV-S").balance == 0
    assert store.get_invoice("INV-S").status == "paid"
    assert store.applied_from("P1") == D("600")
    assert len(store.list_matches()) == 1


def test_basis_survives_database_reopen(tmp_path):
    path = tmp_path / "basis.sqlite"
    with Store(path, today=lambda: TODAY) as value:
        seed(value)
        apply_local(value, "100")
    with Store(path, today=lambda: TODAY) as value:
        value.upsert_invoices([invoice(note="new process stale export")], at=NOW)
        assert value.get_invoice("INV-S").balance == D("500")


def legacy_database(path, balance):
    """Build released tables directly so the fixture cannot acquire new basis data."""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '', contact_name TEXT NOT NULL DEFAULT '',
                on_hold INTEGER NOT NULL DEFAULT 0, hold_reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE invoices (number TEXT PRIMARY KEY, customer_id TEXT NOT NULL, issue_date TEXT NOT NULL,
                due_date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, balance TEXT NOT NULL,
                status TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '', iban TEXT NOT NULL DEFAULT '',
                paid_date TEXT, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE payments (id TEXT PRIMARY KEY, date TEXT NOT NULL, amount TEXT NOT NULL,
                currency TEXT NOT NULL, reference TEXT NOT NULL DEFAULT '', counterparty TEXT NOT NULL DEFAULT '',
                memo TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'bank', added_at TEXT NOT NULL);
            CREATE TABLE matches (payment_id TEXT NOT NULL, invoice_number TEXT NOT NULL, amount TEXT NOT NULL,
                currency TEXT NOT NULL, method TEXT NOT NULL, confidence TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                applied_at TEXT NOT NULL, PRIMARY KEY (payment_id, invoice_number));
        """)
        conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)",
                     ("C1", "Settlement Client", "en", "ap@client.example", "", 0, "", NOW.isoformat()))


@pytest.mark.parametrize("balance", ["400.00", "0.00"])
def test_legacy_locals_unknown_basis_hold_without_financial_rewrite_and_reopen_idempotently(tmp_path, balance):
    path = tmp_path / "legacy.sqlite"
    legacy_database(path, balance)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("INV-S", "C1", "2026-10-01", "2026-10-20", "1000.00", "EUR", balance,
                      "paid" if balance == "0.00" else "open", "INV-S", "", "2026-11-10" if balance == "0.00" else None,
                      "legacy record", NOW.isoformat()))
        conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?)",
                     ("P1", TODAY.isoformat(), "600.00", "EUR", "INV-S", "", "", "bank", NOW.isoformat()))
        conn.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)",
                     ("P1", "INV-S", "600.00", "EUR", "person", "person", "legacy allocation", NOW.isoformat()))
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in ("invoices", "payments", "matches")}
    for _ in range(2):
        with Store(path, today=lambda: TODAY) as value:
            assert value.get_invoice("INV-S").balance == D(balance)
            if balance != "0.00":
                assert RECONCILE in value.reminder_holds(TODAY)["INV-S"].lower()
            with sqlite3.connect(path) as conn:
                after = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in before}
            assert after == before, "migration must preserve historical financial rows exactly"
    with Store(path, today=lambda: TODAY) as value:
        value.reconcile_settlement("INV-S", D("400"), expected_balance=D(balance), currency="EUR", expected_snapshot=value.settlement_snapshot("INV-S"), by="Ada")
        assert value.get_invoice("INV-S").balance == 0
        assert value.get_invoice("INV-S").status == "paid"
        assert value.applied_from("P1") == D("600")


def test_ambiguous_ingest_cannot_authorize_chasing_a_reconciled_export(store):
    apply_local(store, "100")
    text = ("invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference\n"
            "INV-S,C1,2026-10-01,2026-10-20,1000,EUR,500,open,INV-S\n")
    result = read_invoices("generic", text)
    store.ingest(result, at=NOW, file_name="possibly-reconciled.csv")
    assert store.has_import(result.fingerprint)
    assert RECONCILE in store.reminder_holds(TODAY)["INV-S"].lower()


def test_paid_thank_you_also_pauses_when_ownership_of_allocated_invoice_changes(store):
    apply_local(store, '600')
    inv = store.get_invoice('INV-S')
    thanks = with_hash(Draft('thanks', 'thanks', 'C1', ('INV-S',), 'en', 'Thank you',
                            'Thank you for EUR 600.00 for INV-S.', ('ap@client.example',), NOW))
    store.create_draft(thanks, at=NOW)
    store.approve_draft(thanks.id, thanks.hash, at=NOW)
    store.upsert_invoices([replace(inv, currency='USD')], at=NOW)
    assert store.approval(thanks.id) is None
    with pytest.raises(ConflictError, match='settlement reconciliation required'):
        store.reserve_send(thanks.id, thanks.hash, 'eml', at=NOW)


def test_negative_invoice_revision_cannot_erase_an_existing_local_allocation(store):
    apply_local(store, '100')
    before = store.get_invoice('INV-S')
    store.upsert_invoices([invoice('-1000', '-600')], at=NOW)
    assert store.get_invoice('INV-S') == before
    assert 'settlement reconciliation required' in store.reminder_holds(TODAY)['INV-S']
    assert store.applied_from('P1') == 100


def test_reconciliation_snapshot_detects_same_balance_but_changed_gross(store):
    snapshot = store.settlement_snapshot("INV-S")
    store.upsert_invoices([invoice("1200", "600")], at=NOW)
    with pytest.raises(ConflictError, match="snapshot changed"):
        store.reconcile_settlement("INV-S", "400", expected_balance="600", currency="EUR", expected_snapshot=snapshot)
    assert store.get_invoice("INV-S").amount == 1200
    assert store.get_invoice("INV-S").balance == 600


def test_stale_export_cannot_erase_external_settlement_before_any_local_allocation(store):
    before = store.get_invoice('INV-S')
    store.upsert_invoices([invoice(balance='1000')], at=NOW)
    assert store.get_invoice('INV-S').balance == before.balance
    assert 'settlement reconciliation required' in store.reminder_holds(TODAY)['INV-S']
