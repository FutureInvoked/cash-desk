"""P2b: uncertain receipt identities need an audited decision before reuse.

These regressions deliberately exercise the existing ingestion, allocation and
mail gates before relying on the new overlap inspection/resolution API.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sqlite3
from decimal import Decimal

import pytest

from cashdesk.approval import with_hash
from cashdesk.chasing import plan_followups
from cashdesk.importers import read_bank_csv
from cashdesk.model import Customer, Draft, Invoice, Match, Payment
from cashdesk.store import ConflictError, DuplicateImportError, Store

D = Decimal
TODAY = dt.date(2026, 11, 10)
NOW = dt.datetime(2026, 11, 10, 7, tzinfo=dt.timezone.utc)


def bank_row(*, pid=None, date=TODAY, amount="300", currency="EUR",
             reference="INV-A", counterparty="Alpha Workshop", memo="first export", source="bank"):
    stream = io.StringIO()
    writer = csv.writer(stream)
    header = ["date", "amount", "currency", "reference", "counterparty", "memo", "source"]
    values = [date.isoformat(), amount, currency, reference, counterparty, memo, source]
    if pid is not None:
        header.insert(0, "id")
        values.insert(0, pid)
    writer.writerow(header)
    writer.writerow(values)
    result = read_bank_csv(stream.getvalue())
    assert not result.errors, result.errors
    assert len(result.records) == 1
    return result.records[0]


def seed(value):
    value.upsert_customers([
        Customer("C1", "Alpha Workshop", email="ap@alpha.example"),
        Customer("C2", "Beta Studio", email="ap@beta.example"),
        Customer("C3", "Gamma Company", email="ap@gamma.example"),
    ], at=NOW)
    value.upsert_invoices([
        Invoice(f"INV-{letter}", f"C{index}", dt.date(2026, 10, 1), dt.date(2026, 10, 20),
                D("1000"), "EUR", D("1000"), "open", f"INV-{letter}")
        for index, letter in enumerate("ABC", 1)
    ], at=NOW)


@pytest.fixture
def store():
    with Store(":memory:", today=lambda: TODAY) as value:
        seed(value)
        yield value


def pending_pair(value, **changes):
    first = bank_row()
    second = bank_row(**({"memo": "corrected export"} | changes))
    assert first.id != second.id
    value.add_payments([first], at=NOW)
    value.add_payments([second], at=NOW + dt.timedelta(seconds=1))
    return first, second


def pair_ids(value):
    return {frozenset((item["id1"], item["id2"])) for item in value.payment_overlaps()}


def resolve(value, first, second, decision):
    return value.resolve_overlap(first.id, second.id, decision,
                                 expected_snapshot=value.overlap_snapshot(first.id, second.id), by="Ada")


def reminder(value, number="INV-A", did=None):
    inv = value.get_invoice(number)
    customer = value.get_customer(inv.customer_id)
    draft = with_hash(Draft(did or f"reminder-{number}", "stage14", inv.customer_id, (number,),
                            "en", "Invoice reminder", f"Please pay EUR {inv.balance:.2f} for {number}.",
                            (customer.email,), NOW))
    value.create_draft(draft, at=NOW)
    return draft


def dump(value):
    return "\n".join(value._conn.iterdump())


def test_identity_origin_distinguishes_imported_and_derived_ids():
    direct = Payment("BANK-123", TODAY, D("300"), "EUR")
    provided = bank_row(pid="BANK-123")
    derived = bank_row()
    blank = bank_row(pid="")
    assert direct.identity_origin == provided.identity_origin == "provided"
    assert derived.identity_origin == blank.identity_origin == "derived"
    assert blank.id == derived.id, "an empty id cell is not a bank-provided identity"


@pytest.mark.parametrize("origin", ["provided", "derived", "unknown"])
def test_identity_origin_round_trips_through_sqlite(store, origin):
    payment = Payment("ORIGIN", TODAY, D("300"), "EUR", identity_origin=origin)
    store.add_payments([payment], at=NOW)
    assert store.get_payment(payment.id).identity_origin == origin
    assert store.list_payments()[0].identity_origin == origin


def test_invalid_identity_origin_is_refused():
    with pytest.raises(ValueError):
        Payment("ORIGIN", TODAY, D("300"), "EUR", identity_origin="guessed")


@pytest.mark.parametrize("changes", [
    {"counterparty": "Beta Studio"},
    {"reference": "INV-B"},
    {"memo": "changed narrative"},
    {"source": "stripe", "memo": "different export source"},
    {"counterparty": "Beta Studio", "reference": "INV-B", "memo": "changed", "source": "stripe"},
])
def test_changed_descriptive_fields_cannot_make_two_receipts_available(store, changes):
    first, second = pending_pair(store, **changes)
    assert store.unapplied_payments() == [], "uncertain identities cannot become two usable receipts"
    assert pair_ids(store) == {frozenset((first.id, second.id))}
    assert {p.id for p in store.list_payments()} == {first.id, second.id}, "keep both evidence rows"


def test_exact_reimport_has_one_receipt_and_no_overlap(store):
    payment = bank_row()
    assert store.add_payments([payment], at=NOW) == 1
    assert store.add_payments([payment], at=NOW) == 0
    assert [p.id for p in store.unapplied_payments()] == [payment.id]
    assert store.payment_overlaps() == []


def test_exact_file_repeat_is_refused_without_new_observations(store):
    result = read_bank_csv("date,amount,currency,reference,counterparty,memo\n"
                           "2026-11-10,300,EUR,INV-A,Alpha Workshop,unchanged\n")
    store.ingest(result, at=NOW)
    before = dump(store)
    with pytest.raises(DuplicateImportError):
        store.ingest(result, at=NOW)
    assert dump(store) == before


def test_identical_derived_row_once_in_each_of_two_exports_is_one_receipt(store):
    first_csv = ("date,amount,currency,reference,counterparty,memo\n"
                 "2026-11-08,12,EUR,coffee,Someone Else,earlier row\n"
                 "2026-11-10,300,EUR,INV-A,Alpha Workshop,unchanged\n")
    second_csv = first_csv + "2026-11-09,41,EUR,other,Other Person,unrelated row\n"
    first, second = read_bank_csv(first_csv), read_bank_csv(second_csv)
    assert first.records[0].id == second.records[0].id, "the raw content-derived identity collides"
    store.ingest(first, at=NOW)
    store.ingest(second, at=NOW + dt.timedelta(seconds=1))
    observations = [payment for payment in store.list_payments() if payment.amount == D("300")]
    assert len(observations) == 2, "both raw observations are retained"
    # Exactly once in each of two downloads: one bank line seen twice, resolved automatically and audited.
    assert not store.payment_overlaps()
    assert len([p for p in store.unapplied_payments() if p.amount == D("300")]) == 1
    assert [e for e in store.events() if e.kind == "receipt_identity_resolved"]


def test_identical_row_repeated_inside_the_later_export_still_asks(store):
    first_csv = ("date,amount,currency,reference,counterparty,memo\n"
                 "2026-11-10,300,EUR,INV-A,Alpha Workshop,unchanged\n")
    second_csv = first_csv + "2026-11-10,300,EUR,INV-A,Alpha Workshop,unchanged\n"
    store.ingest(read_bank_csv(first_csv), at=NOW)
    store.ingest(read_bank_csv(second_csv), at=NOW + dt.timedelta(seconds=1))
    assert store.payment_overlaps(), "a second identical line may be a second payment: a person decides"


def test_provided_identity_repeated_across_exports_remains_one_receipt(store):
    first_csv = ("id,date,amount,currency,reference,counterparty,memo\n"
                 "BANK-A,2026-11-10,300,EUR,INV-A,Alpha Workshop,unchanged\n")
    second_csv = first_csv + "BANK-B,2026-11-09,41,EUR,other,Other Person,unrelated row\n"
    store.ingest(read_bank_csv(first_csv), at=NOW)
    store.ingest(read_bank_csv(second_csv), at=NOW)
    assert len([payment for payment in store.list_payments() if payment.id == "BANK-A"]) == 1
    assert store.payment_overlaps() == []


def test_distinct_provided_bank_ids_remain_independent(store):
    first, second = bank_row(pid="BANK-A"), bank_row(pid="BANK-B")
    store.add_payments([first, second], at=NOW)
    assert {p.id for p in store.unapplied_payments()} == {first.id, second.id}
    assert store.payment_overlaps() == []


@pytest.mark.parametrize("changes", [
    {"date": TODAY + dt.timedelta(days=1)}, {"amount": "301"}, {"currency": "CZK"},
    {"amount": "0"}, {"amount": "-300"},
])
def test_overlap_requires_same_date_positive_amount_and_currency(store, changes):
    first = bank_row()
    second = bank_row(memo="different", **changes)
    store.add_payments([first, second], at=NOW)
    assert store.payment_overlaps() == []


@pytest.mark.parametrize("amount", ["0", "-300"])
def test_nonpositive_rows_do_not_create_overlap_even_when_keys_are_equal(store, amount):
    first = bank_row(amount=amount)
    second = bank_row(amount=amount, memo="second export")
    store.add_payments([first, second], at=NOW)
    assert store.payment_overlaps() == []


@pytest.mark.parametrize("provided_first", [True, False])
def test_one_uncertain_identity_is_enough_to_hold_the_pair(store, provided_first):
    first = bank_row(pid="PROVIDED") if provided_first else bank_row()
    second = bank_row(memo="new") if provided_first else bank_row(pid="PROVIDED", memo="new")
    store.add_payments([first, second], at=NOW)
    assert store.unapplied_payments() == []
    assert pair_ids(store) == {frozenset((first.id, second.id))}


def test_pending_overlap_is_excluded_from_matching_and_confident_review_totals(store):
    first, second = pending_pair(store, reference="unidentified", counterparty="Unrelated Payer")
    result = store.match_result(TODAY)
    used = {m.payment_id for m in result.applied}
    used.update(item.payment.id for item in (*result.review, *result.unmatched)
                if item.kind != "POSSIBLE_DUPLICATE")
    assert not used.intersection((first.id, second.id))
    assert result.totals()["review_in"].get("EUR", D("0")) == 0
    assert store.list_matches() == []
    assert store.get_invoice("INV-A").balance == D("1000")


@pytest.mark.parametrize("method", ["person", "match"])
@pytest.mark.parametrize("which", [0, 1])
def test_pending_overlap_blocks_both_allocation_entry_points(store, method, which):
    payments = pending_pair(store)
    before = dump(store)
    with pytest.raises(ConflictError):
        if method == "person":
            store.apply_by_person(payments[which].id, "INV-A", D("100"), by="Ada", at=NOW)
        else:
            store.apply_match(Match(payments[which].id, "INV-A", D("100"), "EUR", "person", "person"), at=NOW)
    assert dump(store) == before


def test_all_plausible_payer_and_reference_invoices_are_held_from_planning(store):
    first = bank_row(reference="INV-A", counterparty="Beta Studio")
    second = bank_row(reference="INV-C", counterparty="Alpha Workshop", memo="corrected")
    store.add_payments([first, second], at=NOW)
    holds = store.reminder_holds(TODAY)
    assert {"INV-A", "INV-B", "INV-C"} <= holds.keys()
    plan = plan_followups(store.list_invoices(), store.list_customers(), store.list_matches(),
                          store.chase_state(), TODAY, sender="Example Ltd", holds=holds, created_at=NOW)
    assert not [d for d in plan.all_drafts if d.kind.startswith("stage")]


@pytest.mark.parametrize("gate", ["approve", "check", "reserve", "record"])
def test_pending_overlap_revokes_approval_and_blocks_mail_gates(store, gate):
    draft = reminder(store)
    store.approve_draft(draft.id, draft.hash, at=NOW)
    first = bank_row(reference="no reference", counterparty="Unknown Payer")
    second = bank_row(reference="INV-B", counterparty="Alpha Workshop", memo="corrected")
    store.add_payments([first, second], at=NOW)
    with pytest.raises(ConflictError):
        if gate == "approve":
            store.approve_draft(draft.id, draft.hash, at=NOW)
        elif gate == "check":
            store.check_draft(draft.id, draft.hash, at=NOW)
        elif gate == "reserve":
            store.reserve_send(draft.id, draft.hash, "eml", at=NOW)
        else:
            store.record_send(draft.id, draft.hash, "eml", at=NOW)
    assert store.approval(draft.id) is None
    assert store.get_draft(draft.id).status == "held"
    assert store.list_sends() == []


@pytest.mark.parametrize("prior", ["applied", "dismissed"])
def test_prior_allocation_or_dismissal_does_not_hide_later_overlap(store, prior):
    first = bank_row()
    store.add_payments([first], at=NOW)
    if prior == "applied":
        store.apply_by_person(first.id, "INV-A", D("100"), by="Ada", at=NOW)
    else:
        store.dismiss_payment(first.id, "other income", at=NOW)
    before_matches = store.list_matches()
    second = bank_row(reference="INV-B", counterparty="Beta Studio", memo="corrected")
    store.add_payments([second], at=NOW + dt.timedelta(seconds=1))
    assert store.unapplied_payments() == []
    assert pair_ids(store) == {frozenset((first.id, second.id))}
    assert {"INV-A", "INV-B"} <= store.reminder_holds(TODAY).keys()
    assert store.list_matches() == before_matches


def test_same_decision_keeps_older_receipt_and_retains_inert_evidence(store):
    first, second = pending_pair(store)
    before = {p.id: p for p in store.list_payments()}
    resolve(store, first, second, "same")
    assert store.payment_overlaps() == []
    assert [p.id for p in store.unapplied_payments()] == [first.id]
    assert {p.id: p for p in store.list_payments()} == before
    with pytest.raises(ConflictError):
        store.apply_by_person(second.id, "INV-A", D("100"), by="Ada", at=NOW)
    with pytest.raises(ConflictError):
        store.apply_match(Match(second.id, "INV-A", D("100"), "EUR", "person", "person"), at=NOW)
    assert store.add_payments([first, second], at=NOW) == 0
    assert store.payment_overlaps() == []
    assert [p.id for p in store.unapplied_payments()] == [first.id]


def test_same_decision_keeps_allocated_original_even_when_other_row_has_older_timestamp(store):
    first = bank_row()
    store.add_payments([first], at=NOW)
    store.apply_by_person(first.id, "INV-A", D("100"), by="Ada", at=NOW)
    matches = store.list_matches()
    second = bank_row(memo="corrected")
    store.add_payments([second], at=NOW - dt.timedelta(days=1))
    resolve(store, first, second, "same")
    assert [(p.id, p.amount) for p in store.unapplied_payments()] == [(first.id, D("200"))]
    assert store.list_matches() == matches
    assert store.get_invoice("INV-A").balance == D("900")
    assert store.applied_from(second.id) == 0


def test_same_decision_does_not_resurrect_dismissed_original(store):
    first = bank_row()
    store.add_payments([first], at=NOW)
    store.dismiss_payment(first.id, "other income", at=NOW)
    second = bank_row(memo="corrected")
    store.add_payments([second], at=NOW + dt.timedelta(seconds=1))
    resolve(store, first, second, "same")
    assert store.unapplied_payments() == []
    assert {p.id for p in store.list_payments()} == {first.id, second.id}


def test_distinct_decision_releases_each_receipts_remaining_funds(store):
    first = bank_row()
    store.add_payments([first], at=NOW)
    store.apply_by_person(first.id, "INV-A", D("100"), by="Ada", at=NOW)
    second = bank_row(reference="INV-B", counterparty="Beta Studio", memo="corrected")
    store.add_payments([second], at=NOW + dt.timedelta(seconds=1))
    resolve(store, first, second, "distinct")
    assert store.payment_overlaps() == []
    assert {p.id: p.amount for p in store.unapplied_payments()} == {first.id: D("200"), second.id: D("300")}
    store.apply_by_person(first.id, "INV-B", D("200"), by="Ada", at=NOW)
    store.apply_by_person(second.id, "INV-C", D("300"), by="Ada", at=NOW)
    assert store.applied_from(first.id) == store.applied_from(second.id) == D("300")
    assert store.add_payments([first, second], at=NOW) == 0
    assert store.payment_overlaps() == []


@pytest.mark.parametrize("decision", ["same", "distinct"])
def test_human_decision_is_audited_with_both_ids_and_reviewer(store, decision):
    first, second = pending_pair(store)
    old_ids = {event.id for event in store.events(limit=10000)}
    resolve(store, first, second, decision)
    audit = [json.dumps({"kind": e.kind, "ref": e.ref, "detail": e.detail}, sort_keys=True)
             for e in store.events(limit=10000) if e.id not in old_ids]
    assert any(all(token in row for token in (first.id, second.id, decision, "Ada")) for row in audit)


def test_third_variant_reopens_review_after_same_decision(store):
    first, alias = pending_pair(store)
    resolve(store, first, alias, "same")
    third = bank_row(reference="INV-C", counterparty="Gamma Company", memo="third export")
    store.add_payments([third], at=NOW + dt.timedelta(seconds=2))
    assert store.payment_overlaps()
    assert any(third.id in pair for pair in pair_ids(store))
    assert store.unapplied_payments() == [], "an unresolved alias member must also freeze its canonical receipt"
    for payment in (first, alias, third):
        with pytest.raises(ConflictError):
            store.apply_by_person(payment.id, "INV-A", D("10"), by="Ada", at=NOW)


def test_decision_through_alias_cannot_discard_allocated_canonical(store):
    first = bank_row()
    store.add_payments([first], at=NOW)
    store.apply_by_person(first.id, "INV-A", D("100"), by="Ada", at=NOW)
    alias = bank_row(memo="alias export")
    store.add_payments([alias], at=NOW + dt.timedelta(seconds=1))
    resolve(store, first, alias, "same")
    third = bank_row(memo="third export")
    store.add_payments([third], at=NOW + dt.timedelta(seconds=2))
    resolve(store, alias, third, "same")
    assert store.list_matches()[0].payment_id == first.id
    assert store.get_invoice("INV-A").balance == D("900")
    if store.payment_overlaps():
        assert store.unapplied_payments() == [], "remaining member reviews must preserve the group hold"
    else:
        assert [(p.id, p.amount) for p in store.unapplied_payments()] == [(first.id, D("200"))]
    for payment in (alias, third):
        with pytest.raises(ConflictError):
            store.apply_by_person(payment.id, "INV-B", D("10"), by="Ada", at=NOW)


@pytest.mark.parametrize("change", ["payment", "allocation", "member"])
@pytest.mark.parametrize("decision", ["same", "distinct"])
def test_snapshot_refuses_changed_payment_allocation_or_group_membership(store, change, decision):
    first, second = pending_pair(store)
    snapshot = store.overlap_snapshot(first.id, second.id)
    if change == "payment":
        store._conn.execute("UPDATE payments SET memo=? WHERE id=?", ("edited after review", first.id))
    elif change == "allocation":
        # Simulate an older writer that committed an allocation after the reviewer read.
        store._conn.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)",
                            (first.id, "INV-A", "10.00", "EUR", "person", "person", "older writer", NOW.isoformat()))
        store._conn.execute("UPDATE invoices SET balance='990.00' WHERE number='INV-A'")
    else:
        store.add_payments([bank_row(memo="new member after review")], at=NOW)
    assert store.overlap_snapshot(first.id, second.id) != snapshot
    before = dump(store)
    with pytest.raises(ConflictError, match="(?i)snapshot|stale|changed"):
        store.resolve_overlap(first.id, second.id, decision, expected_snapshot=snapshot, by="Ada")
    assert dump(store) == before


@pytest.mark.parametrize("decision", ["same", "distinct"])
def test_decision_and_audit_roll_back_as_one_transaction(store, monkeypatch, decision):
    first, second = pending_pair(store)
    snapshot = store.overlap_snapshot(first.id, second.id)
    before = dump(store)

    def audit_unavailable(*args, **kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(store, "log_event", audit_unavailable)
    with pytest.raises(RuntimeError, match="audit storage unavailable"):
        store.resolve_overlap(first.id, second.id, decision, expected_snapshot=snapshot, by="Ada")
    assert dump(store) == before, "no aliases, decisions, approvals or financial rows may commit without the audit"
    assert pair_ids(store) == {frozenset((first.id, second.id))}


@pytest.mark.parametrize("decision", ["same", "distinct"])
def test_resolution_reads_and_writes_under_one_sqlite_transaction(store, decision):
    first, second = pending_pair(store)
    snapshot = store.overlap_snapshot(first.id, second.id)
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        store.resolve_overlap(first.id, second.id, decision, expected_snapshot=snapshot, by="Ada")
    finally:
        store._conn.set_trace_callback(None)
    sql = [statement.strip().upper() for statement in statements]
    starts = [index for index, statement in enumerate(sql) if statement.startswith("BEGIN")]
    commits = [index for index, statement in enumerate(sql) if statement == "COMMIT"]
    assert len(starts) == len(commits) == 1
    assert sql[starts[0]] == "BEGIN IMMEDIATE"
    reads = [index for index, statement in enumerate(sql) if statement.startswith("SELECT")
             and any(table in statement for table in ("PAYMENTS", "MATCHES", "OVERLAP"))]
    writes = [index for index, statement in enumerate(sql)
              if statement.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert reads and writes
    assert all(starts[0] < index < commits[0] for index in (*reads, *writes))


def legacy_database(path, *, two_allocated=False):
    """Create released rows without identity provenance or new overlap tables."""
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
            CREATE TABLE invoice_settlements (number TEXT PRIMARY KEY, external_paid TEXT, export_settled TEXT,
                reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE drafts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, customer_id TEXT NOT NULL,
                invoice_numbers TEXT NOT NULL, language TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                recipients TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, hash TEXT NOT NULL,
                figures TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE sends (idempotency_key TEXT PRIMARY KEY, draft_id TEXT NOT NULL, hash TEXT NOT NULL,
                reserved_at TEXT NOT NULL, sent_at TEXT, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent',
                note TEXT NOT NULL DEFAULT '');
        """)
        conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)",
                     ("C1", "Alpha Workshop", "en", "ap@alpha.example", "", 0, "", NOW.isoformat()))
        for index in range(1, 3 if two_allocated else 2):
            number, pid = f"INV-{index}", f"LEGACY-{index}"
            conn.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (number, "C1", "2026-10-01", "2026-10-20", "1000.00", "EUR", "700.00",
                          "open", number, "", None, "historical invoice", NOW.isoformat()))
            conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?)",
                         (pid, TODAY.isoformat(), "300.00", "EUR", number, "Alpha Workshop", "historical receipt",
                          "bank", NOW.isoformat()))
            conn.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)",
                         (pid, number, "300.00", "EUR", "person", "person", "historical allocation", NOW.isoformat()))
            conn.execute("INSERT INTO invoice_settlements VALUES (?,?,?,?,?)",
                         (number, "0.00", "0.00", "", NOW.isoformat()))
        conn.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     ("already-sent", "stage14", "C1", '["INV-1"]', "en", "Old reminder", "Historical body",
                      '["ap@alpha.example"]', NOW.isoformat(), "sent", "historical-hash", "[]", NOW.isoformat()))
        conn.execute("INSERT INTO sends VALUES (?,?,?,?,?,?,?,?)",
                     ("historical-key", "already-sent", "historical-hash", NOW.isoformat(), NOW.isoformat(),
                      "eml", "sent", "historical mail"))


def financial_rows(value):
    conn = getattr(value, "_conn", value)
    result = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
              for table in ("invoices", "matches", "drafts", "sends", "invoice_settlements")}
    result["payments"] = [tuple(row) for row in conn.execute(
        "SELECT id,date,amount,currency,reference,counterparty,memo,source,added_at FROM payments ORDER BY rowid")]
    return result


def test_legacy_origin_is_unknown_and_new_same_key_is_held_without_history_loss(tmp_path):
    path = tmp_path / "legacy.sqlite"
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        released_rows = financial_rows(conn)
    with Store(path, today=lambda: TODAY) as value:
        before = financial_rows(value)
        assert before == released_rows, "migration must preserve original financial rows and sent history"
        assert value.get_payment("LEGACY-1").identity_origin == "unknown"
        new = bank_row(pid="NEW-PROVIDED", reference="INV-1", memo="new export")
        value.add_payments([new], at=NOW)
        assert value.unapplied_payments() == []
        assert pair_ids(value) == {frozenset(("LEGACY-1", new.id))}
        assert "INV-1" in value.reminder_holds(TODAY)
        after = financial_rows(value)
        assert {key: after[key] for key in before if key != "payments"} == {
            key: before[key] for key in before if key != "payments"}
        assert after["payments"][:1] == before["payments"]
    with Store(path, today=lambda: TODAY) as reopened:
        assert reopened.unapplied_payments() == []
        assert pair_ids(reopened) == {frozenset(("LEGACY-1", new.id))}
        assert financial_rows(reopened) == after


def test_both_historically_allocated_same_refuses_without_rewriting_history(tmp_path):
    path = tmp_path / "legacy-both.sqlite"
    legacy_database(path, two_allocated=True)
    with sqlite3.connect(path) as conn:
        released_rows = financial_rows(conn)
    with Store(path, today=lambda: TODAY) as value:
        assert financial_rows(value) == released_rows
        first, second = value.get_payment("LEGACY-1"), value.get_payment("LEGACY-2")
        assert pair_ids(value) == {frozenset((first.id, second.id))}
        before = financial_rows(value)
        with pytest.raises(ConflictError, match="both receipts already have allocations"):
            resolve(value, first, second, "same")
        assert financial_rows(value) == before
        assert pair_ids(value) == {frozenset((first.id, second.id))}
        assert {"INV-1", "INV-2"} <= value.reminder_holds(TODAY).keys()
        assert value.unapplied_payments() == []


def test_both_historically_allocated_distinct_requires_snapshot_then_preserves_history(tmp_path):
    path = tmp_path / "legacy-distinct.sqlite"
    legacy_database(path, two_allocated=True)
    with sqlite3.connect(path) as conn:
        released_rows = financial_rows(conn)
    with Store(path, today=lambda: TODAY) as value:
        assert financial_rows(value) == released_rows
        first, second = value.get_payment("LEGACY-1"), value.get_payment("LEGACY-2")
        before = financial_rows(value)
        with pytest.raises(ConflictError):
            value.resolve_overlap(first.id, second.id, "distinct", expected_snapshot="unverified", by="Ada")
        assert financial_rows(value) == before
        resolve(value, first, second, "distinct")
        assert value.payment_overlaps() == []
        assert financial_rows(value) == before
        assert not {"INV-1", "INV-2"}.intersection(value.reminder_holds(TODAY))
        assert value.applied_from(first.id) == value.applied_from(second.id) == D("300")


def test_corrupt_alias_cycle_fails_closed_without_financial_changes(store):
    first, second = pending_pair(store)
    store._conn.execute("INSERT INTO receipt_aliases VALUES (?, ?, ?)", (first.id, second.id, NOW.isoformat()))
    store._conn.execute("INSERT INTO receipt_aliases VALUES (?, ?, ?)", (second.id, first.id, NOW.isoformat()))
    before = financial_rows(store)
    with pytest.raises(ConflictError, match="contains a cycle"):
        store.unapplied_payments()
    assert financial_rows(store) == before


def test_same_resolution_cannot_reopen_consumed_cash_through_direct_match(store):
    first = bank_row()
    store.add_payments([first], at=NOW)
    store.apply_by_person(first.id, 'INV-A', D('300'), by='Ada', at=NOW)
    second = bank_row(memo='later observation')
    store.add_payments([second], at=NOW)
    resolve(store, first, second, 'same')
    before = financial_rows(store)
    with pytest.raises(ConflictError, match='is left to apply'):
        store.apply_match(Match(first.id, 'INV-B', D('300'), 'EUR', 'person', 'person'), at=NOW)
    assert financial_rows(store) == before


def test_same_identity_does_not_choose_between_conflicting_invoice_references(store):
    first, second = pending_pair(store, reference='INV-B')
    resolve(store, first, second, 'same')
    result = store.match_result(TODAY)
    assert not result.applied
    assert {'INV-A', 'INV-B'} <= store.reminder_holds(TODAY).keys()
    assert any(i.kind == 'MULTI_INVOICE' for i in result.review)
    store.apply_by_person(first.id, 'INV-B', D('300'), by='Ada', at=NOW)
    assert not store.receipt_placement_reviews()
    assert len(store.list_matches()) == 1


def test_same_identity_honors_dismissal_on_the_newer_alias(store):
    first, second = bank_row(), bank_row(memo='legacy dismissed')
    # Model historical rows before pair discovery, as an older ledger can contain them.
    store.add_payments([first], at=NOW)
    store._conn.execute('INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (second.id, TODAY.isoformat(), str(second.amount), second.currency, second.reference, second.counterparty, second.memo, second.source, NOW.isoformat()))
    store._conn.execute('INSERT INTO dismissed VALUES (?, ?, ?)', (second.id, 'other income', NOW.isoformat()))
    store._discover_overlaps(NOW)
    resolve(store, first, second, 'same')
    assert store.unapplied_payments() == []
    assert not store.match_result(TODAY).applied
    with pytest.raises(ConflictError, match='identity group was dismissed'):
        store.apply_by_person(first.id, 'INV-A', D('300'), by='Ada', at=NOW)


def test_same_after_existing_allocation_keeps_conflicting_reference_held_until_confirmation(store):
    first = bank_row()
    store.add_payments([first], at=NOW)
    store.apply_by_person(first.id, 'INV-A', D('300'), by='Ada', at=NOW)
    second = bank_row(reference='INV-B', memo='reference correction')
    store.add_payments([second], at=NOW)
    resolve(store, first, second, 'same')
    assert {'INV-A', 'INV-B'} <= store.reminder_holds(TODAY).keys()
    reviews = store.receipt_placement_reviews()
    assert len(reviews) == 1
    assert 'receipt allocation review required' in store.reminder_holds(TODAY)['INV-B']
    store.confirm_receipt_placement(first.id, expected_snapshot=store.overlap_snapshot(first.id, first.id), by='Ada', at=NOW)
    assert not store.receipt_placement_reviews()
    assert 'INV-B' not in store.reminder_holds(TODAY)


def test_confirm_placement_requires_existing_completed_disposition_and_fresh_snapshot(store):
    first, second = pending_pair(store, reference='INV-B')
    resolve(store, first, second, 'same')
    snapshot = store.overlap_snapshot(first.id, first.id)
    with pytest.raises(ConflictError, match='place unused receipt money with Apply or Dismiss'):
        store.confirm_receipt_placement(first.id, expected_snapshot=snapshot, by='Ada', at=NOW)
    with pytest.raises(ConflictError, match='receipt snapshot changed'):
        store.confirm_receipt_placement(first.id, expected_snapshot='stale', by='Ada', at=NOW)
    store.apply_by_person(first.id, 'INV-B', D('300'), by='Ada', at=NOW)
    with pytest.raises(ConflictError, match='no receipt allocation review is pending'):
        store.confirm_receipt_placement(first.id, expected_snapshot=store.overlap_snapshot(first.id, first.id), by='Ada', at=NOW)


def test_dismissal_rechecks_identity_after_acquiring_the_write_transaction(tmp_path, monkeypatch):
    from contextlib import contextmanager
    path = tmp_path / 'dismiss-race.sqlite'
    with Store(path, today=lambda: TODAY) as value, Store(path, today=lambda: TODAY) as other:
        seed(value)
        first, second = bank_row(), bank_row(memo='arrives before write lock')
        value.add_payments([first], at=NOW)
        original_tx = value._tx
        injected = False
        @contextmanager
        def interleaved():
            nonlocal injected
            if value._depth == 0 and not injected:
                injected = True
                other.add_payments([second], at=NOW)
            with original_tx() as conn:
                yield conn
        monkeypatch.setattr(value, '_tx', interleaved)
        with pytest.raises(ConflictError, match='possible receipt overlap'):
            value.dismiss_payment(first.id, 'other income', at=NOW)
        assert injected
        assert not value._conn.execute('SELECT * FROM dismissed').fetchall()
        assert value.payment_overlaps()


def test_new_identity_question_suppresses_pending_placement_cash_totals(store):
    first, second = pending_pair(store, reference='INV-B')
    resolve(store, first, second, 'same')
    store.add_payments([bank_row(memo='third export')], at=NOW)
    result = store.match_result(TODAY)
    assert result.totals()['review_in'].get('EUR', D('0')) == 0
    assert len([i for i in result.review if i.payment.id == first.id]) == 1
    assert all(i.kind == 'POSSIBLE_DUPLICATE' for i in result.review)


def test_same_identity_preserves_reference_evidence_for_a_later_imported_invoice(store):
    store.upsert_invoices([Invoice('INV-A','C1',dt.date(2026,10,1),dt.date(2026,10,20),D('300'),'EUR',D('300'))], at=NOW)
    first, second = pending_pair(store, reference='INV-LATER')
    resolve(store, first, second, 'same')
    assert store.receipt_placement_reviews()
    store.upsert_invoices([Invoice('INV-LATER','C1',dt.date(2026,10,1),dt.date(2026,10,20),D('300'),'EUR',D('300'))], at=NOW)
    assert not store.match_result(TODAY).applied
    assert {'INV-A','INV-LATER'} <= store.reminder_holds(TODAY).keys()
