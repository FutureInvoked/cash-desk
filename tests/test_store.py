"""store.py: SQLite persistence where every write is idempotent or refuses."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

import pytest

from cashdesk.approval import draft_hash, with_hash
from cashdesk.brief import morning_brief
from cashdesk.chasing import ChaseState
from cashdesk.importers import read_bank_csv, read_customers, read_invoices
from cashdesk.model import Customer, Draft, Invoice, Match, Payment
from cashdesk.store import (
    AlreadySentError,
    BriefSnapshot,
    ConflictError,
    DuplicateImportError,
    HashMismatchError,
    NotApprovedError,
    NotFoundError,
    StatusSnapshot,
    Store,
)

D = Decimal
AS_OF = dt.date(2026, 11, 10)
NOW = dt.datetime(2026, 11, 10, 7, 0, tzinfo=dt.timezone.utc)

INVOICES_CSV = """invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference,iban
INV-1,C1,2026-10-01,2026-10-20,1250.00,EUR,1250.00,open,20261,DE89370400440532013000
INV-2,C1,2026-09-01,2026-09-20,500.00,EUR,500.00,open,20262,DE89370400440532013000
INV-3,C2,2026-10-20,2026-11-20,48400.00,CZK,48400.00,open,20263,CZ6508000000192000145399
INV-4,C2,2026-07-01,2026-07-20,1000.00,CZK,1000.00,open,20264,CZ6508000000192000145399
INV-5,C3,2026-10-01,2026-10-15,600.00,EUR,600.00,disputed,20265,DE89370400440532013000
INV-6,C1,2026-10-01,2026-10-15,99.00,EUR,0.00,paid,20266,DE89370400440532013000
"""
CUSTOMERS_CSV = """customer_id,name,language,email,contact_name,on_hold,hold_reason
C1,Northgate Sample Studio Ltd,en,ann@northgate.example,Ann Example,,
C2,Modrá Dílna Demo s.r.o.,cs,petr@modra.example,Petr Ukázka,,
C3,Oakridge Placeholder Partners LLP,en,lee@oakridge.example,Lee Placeholder,,
"""
BANK_CSV = """id,date,amount,currency,counterparty,reference,memo
B1,2026-11-06,1250.00,EUR,NORTHGATE,INV-1,
B2,2026-11-06,700.00,EUR,SOMEONE,project fee,
"""


def draft(did="d1", cid="C1", numbers=("INV-1",), body="Body EUR 1,250.00", subject="Reminder", status="pending") -> Draft:
    d = Draft(did, "stage14", cid, tuple(numbers), "en", subject, body, ("ann@northgate.example",), NOW, status)
    return Draft(d.id, d.kind, d.customer_id, d.invoice_numbers, d.language, d.subject, d.body, d.recipients, d.created_at, d.status, draft_hash(body, d.recipients, subject))


@pytest.fixture
def store() -> Store:
    s = Store(":memory:")
    s.ingest(read_customers(CUSTOMERS_CSV), at=NOW)
    s.ingest(read_invoices("generic", INVOICES_CSV), at=NOW)
    yield s
    s.close()


def test_file_store_persists_between_opens(tmp_path):
    path = tmp_path / "cashdesk.sqlite"
    with Store(path) as s:
        s.upsert_customers([Customer("C9", "Persisted Ltd")])
    with Store(path) as s:
        assert s.get_customer("C9").name == "Persisted Ltd"
    with pytest.raises(NotFoundError):
        Store(":memory:").get_customer("nobody")


def test_duplicate_import_is_refused(store):
    result = read_bank_csv(BANK_CSV)
    counts = store.ingest(result, at=NOW)
    assert counts == {"payments": 2} and store.has_import(result.fingerprint)
    with pytest.raises(DuplicateImportError):
        store.ingest(result, at=NOW)
    with pytest.raises(DuplicateImportError):
        store.record_import(read_invoices("generic", INVOICES_CSV), at=NOW)
    assert len(store.list_payments()) == 2
    imports = store.list_imports()
    assert [i.kind for i in imports] == ["customers", "invoices", "bank"]


def test_ingest_refuses_an_import_with_row_errors_unless_told_otherwise(store):
    bad = read_bank_csv(BANK_CSV + "B3,not-a-date,1,EUR,x,,\n")
    with pytest.raises(ConflictError):
        store.ingest(bad, at=NOW)
    assert store.ingest(bad, at=NOW, allow_errors=True) == {"payments": 2}


def test_upserts_are_idempotent_and_keep_holds(store):
    assert store.upsert_customers([Customer("C1", "Northgate Sample Studio Ltd", "en", "ann@northgate.example", "Ann Example")]) == 1
    store.set_hold("C1", "asked us to pause")
    store.upsert_customers([Customer("C1", "Northgate Sample Studio Limited", "en", "new@northgate.example", "Ann Example")])
    c1 = store.get_customer("C1")
    assert c1.name == "Northgate Sample Studio Limited" and c1.email == "new@northgate.example"
    assert c1.on_hold and c1.hold_reason == "asked us to pause", "an import never clears a hold"
    store.upsert_invoices([Invoice("INV-1", "C1", dt.date(2026, 10, 1), dt.date(2026, 10, 20), D("1250"), "EUR", D("250"), "open", "20261")])
    assert store.get_invoice("INV-1").balance == D("250.00")
    assert len(store.list_invoices()) == 6


def test_add_payments_ignores_known_ids(store):
    payments = read_bank_csv(BANK_CSV).records
    assert store.add_payments(payments) == 2
    assert store.add_payments(payments) == 0
    assert store.add_payments([Payment("B9", AS_OF, D("1"), "EUR")]) == 1


def test_apply_match_once(store):
    store.add_payments(read_bank_csv(BANK_CSV).records)
    m = Match("B1", "INV-1", D("1250.00"), "EUR", "reference+exact", "high")
    assert store.apply_match(m, at=NOW) is True
    inv = store.get_invoice("INV-1")
    assert inv.balance == D("0.00") and inv.status == "paid" and inv.paid_date == dt.date(2026, 11, 6)
    assert store.apply_match(m, at=NOW) is False
    assert store.get_invoice("INV-1").balance == D("0.00")
    assert [x.invoice_number for x in store.list_matches()] == ["INV-1"]
    with pytest.raises(ConflictError):
        store.apply_match(Match("B2", "INV-404", D("1"), "EUR", "reference+exact", "high"), at=NOW)
    part = Match("B2", "INV-2", D("200.00"), "EUR", "manual", "high")
    assert store.apply_match(part, at=NOW) and store.get_invoice("INV-2").balance == D("300.00") and store.get_invoice("INV-2").status == "open"


def test_create_draft_is_idempotent_and_refuses_a_conflicting_hash(store):
    d = draft()
    assert store.create_draft(d) is True
    assert store.create_draft(d) is False
    with pytest.raises(ConflictError):
        store.create_draft(draft(body="Body EUR 1,520.00"))
    assert store.get_draft("d1") == d
    assert [x.id for x in store.list_drafts(status="pending")] == ["d1"]
    assert store.list_drafts(customer_id="C2") == []
    assert store.create_drafts([draft("d2", "C2", ("INV-3",)), d]) == 1


def test_approve_by_hash(store):
    d = draft()
    store.create_draft(d)
    with pytest.raises(HashMismatchError):
        store.approve_draft("d1", "0" * 64, approved_by="reviewer", at=NOW)
    store.approve_draft("d1", d.hash, approved_by="reviewer", at=NOW)
    assert store.get_draft("d1").status == "approved"
    store.approve_draft("d1", d.hash, approved_by="reviewer", at=NOW)  # same approval twice is fine
    assert store.approval("d1").approved_by == "reviewer"
    with pytest.raises(NotFoundError):
        store.approve_draft("nope", d.hash)


def test_send_needs_an_approval_and_happens_once(store):
    d = draft()
    store.create_draft(d)
    with pytest.raises(NotApprovedError):
        store.record_send("d1", d.hash, "eml", at=NOW)
    store.approve_draft("d1", d.hash, at=NOW)
    with pytest.raises(HashMismatchError):
        store.record_send("d1", "0" * 64, "eml", at=NOW)
    key = store.record_send("d1", d.hash, "eml", at=NOW)
    assert re.fullmatch(r"[0-9a-f]{32}", key) and store.get_draft("d1").status == "sent"
    with pytest.raises(AlreadySentError):
        store.record_send("d1", d.hash, "eml", at=NOW)
    with pytest.raises(AlreadySentError):
        store.record_send("d1", d.hash, "smtp", at=NOW)
    (sent,) = store.list_sends()
    assert sent.idempotency_key == key and sent.mode == "eml" and sent.draft_id == "d1"


def test_revision_after_approval_drops_the_approval(store):
    d = draft()
    store.create_draft(d)
    store.approve_draft("d1", d.hash, at=NOW)
    revised = draft(body="Body, kindly: EUR 1,250.00")
    store.replace_draft(revised)
    assert store.get_draft("d1").status == "pending" and store.get_draft("d1").hash == revised.hash
    assert store.approval("d1") is None
    with pytest.raises(NotApprovedError):
        store.record_send("d1", revised.hash, "eml", at=NOW)
    with pytest.raises(HashMismatchError):
        store.approve_draft("d1", d.hash, at=NOW), "the old hash no longer approves anything"
    store.approve_draft("d1", revised.hash, at=NOW)
    store.record_send("d1", revised.hash, "eml", at=NOW)
    with pytest.raises(ConflictError):
        store.replace_draft(draft(body="too late"))


def test_reject_and_hold_move_drafts(store):
    store.create_drafts([draft("d1"), draft("d2", "C1", ("INV-2",)), draft("d3", "C2", ("INV-3",))])
    store.reject_draft("d2", reason="duplicate")
    assert store.get_draft("d2").status == "rejected"
    store.set_hold("C1", "dispute in reply")
    assert store.get_customer("C1").on_hold and store.get_customer("C1").hold_reason == "dispute in reply"
    assert store.get_draft("d1").status == "held" and store.get_draft("d2").status == "rejected" and store.get_draft("d3").status == "pending"
    assert [d.id for d in store.list_drafts(status="held")] == ["d1"]
    store.clear_hold("C1")
    assert not store.get_customer("C1").on_hold and store.get_draft("d1").status == "pending"
    with pytest.raises(NotFoundError):
        store.set_hold("C404", "x")


def test_chase_state_round_trip(store):
    assert store.chase_state().drafted == {} and store.chase_state().thanked == set()
    state = ChaseState({"INV-1": "stage14"})
    state.record_thanks("INV-6", "B6")
    store.save_chase_state(state)
    assert store.chase_state().drafted == state.drafted and store.chase_state().was_thanked("INV-6", "B6")
    state.record("INV-2", "stage07")
    store.save_chase_state(state)
    assert store.chase_state().drafted == {"INV-1": "stage14", "INV-2": "stage07"} and store.chase_state().thanked == {"INV-6:B6"}


def test_events_are_logged(store):
    store.log_event("import", "abc", {"rows": 3}, at=NOW)
    store.log_event("send", "d1", {"mode": "eml"}, at=NOW)
    events = store.events()
    assert [e.kind for e in events][-2:] == ["import", "send"] and events[-1].detail == {"mode": "eml"}


def test_status_snapshot_buckets_per_currency(store):
    d3 = draft("d3", "C1", ("INV-2",))
    store.create_drafts([draft("d1"), draft("d2", "C2", ("INV-3",)), d3])
    store.approve_draft("d3", d3.hash, at=NOW)
    store.set_hold("C2", "dispute")
    snap = store.status_snapshot(AS_OF)
    assert isinstance(snap, StatusSnapshot)
    assert snap.open_by_bucket["EUR"] == {"1-30": D("1850.00"), "31-60": D("500.00")}
    assert snap.open_by_bucket["CZK"] == {"current": D("48400.00"), "90+": D("1000.00")}
    assert snap.open_count == 5 and snap.awaiting_approval == 1 and snap.held_drafts == 1 and snap.approved_unsent == 1
    assert snap.on_hold == 2, "C2 is on hold and C3 has a disputed invoice"
    assert snap.last_import.kind == "invoices" and snap.last_import.imported_at == NOW
    as_dict = snap.to_dict()
    assert as_dict["open_by_bucket"]["EUR"]["1-30"] == "1850.00" and as_dict["last_import"]["source"] == "generic"


def test_hold_after_approval_blocks_the_send_and_drops_the_approval(store):
    d = draft()
    store.create_draft(d)
    store.approve_draft("d1", d.hash, at=NOW)
    store.set_hold("C1", "dispute in reply", at=NOW)
    assert store.get_draft("d1").status == "held" and store.approval("d1") is None
    with pytest.raises(ConflictError, match="on hold"):
        store.record_send("d1", d.hash, "eml", at=NOW)
    store.clear_hold("C1", at=NOW)
    with pytest.raises(NotApprovedError):
        store.record_send("d1", d.hash, "eml", at=NOW)
    assert store.get_draft("d1").status == "pending", "needs a fresh approval after the hold"


def test_hold_arriving_by_import_or_dispute_status_blocks_approval_and_send(store):
    d = draft()
    store.create_draft(d)
    store.upsert_customers([Customer("C1", "Northgate Sample Studio Ltd", "en", "ann@northgate.example", "Ann Example", on_hold=True, hold_reason="asked us to pause")])
    assert store.get_draft("d1").status == "held"
    with pytest.raises(ConflictError):
        store.approve_draft("d1", d.hash, at=NOW)
    d2 = draft("d2", "C2", ("INV-3",))
    store.create_draft(d2)
    store.approve_draft("d2", d2.hash, at=NOW)
    disputed = Invoice("INV-3", "C2", dt.date(2026, 10, 20), dt.date(2026, 11, 20), D("48400"), "CZK", D("48400"), "disputed", "20263")
    store.upsert_invoices([disputed])
    with pytest.raises(ConflictError):
        store.record_send("d2", d2.hash, "eml", at=NOW)
    with pytest.raises(ConflictError):
        store.approve_draft("d2", d2.hash, at=NOW)


def test_stale_reminder_is_refused_but_a_thank_you_for_a_paid_invoice_is_fine(store):
    d = draft()
    store.create_draft(d)
    store.approve_draft("d1", d.hash, at=NOW)
    store.add_payments(read_bank_csv(BANK_CSV).records)
    store.apply_match(Match("B1", "INV-1", D("1250.00"), "EUR", "reference+exact", "high"), at=NOW)
    with pytest.raises(ConflictError):
        store.record_send("d1", d.hash, "eml", at=NOW)
    with pytest.raises(ConflictError):
        store.approve_draft("d1", d.hash, at=NOW)
    thanks = with_hash(Draft("t1", "thanks", "C1", ("INV-1",), "en", "Thanks", "Body", ("ann@northgate.example",), NOW))
    store.create_draft(thanks)
    store.approve_draft("t1", thanks.hash, at=NOW)
    assert store.record_send("t1", thanks.hash, "eml", at=NOW)
    unknown = with_hash(Draft("u1", "stage14", "C1", ("INV-404",), "en", "S", "B", ("ann@northgate.example",), NOW))
    store.create_draft(unknown)
    with pytest.raises(ConflictError):
        store.approve_draft("u1", unknown.hash, at=NOW)


def test_store_refuses_a_draft_whose_hash_does_not_cover_its_text(store):
    bogus = Draft("d9", "stage14", "C1", ("INV-1",), "en", "S", "B", ("a@b.example",), NOW, "pending", "0" * 64)
    with pytest.raises(ConflictError):
        store.create_draft(bogus)
    d = draft()
    store.create_draft(d)
    with pytest.raises(ConflictError):
        store.replace_draft(Draft("d1", "stage14", "C1", ("INV-1",), "en", "S", "changed", d.recipients, NOW, "pending", d.hash))
    with pytest.raises(ConflictError):
        store.replace_draft(with_hash(Draft("d1", "stage14", "C2", ("INV-3",), "en", "S", "B", d.recipients, NOW)))
    assert store.get_draft("d1") == d


def test_naive_timestamps_are_taken_as_utc_and_never_crash_the_brief(store):
    store.add_payments(read_bank_csv(BANK_CSV).records)
    store.apply_match(Match("B1", "INV-1", D("1250.00"), "EUR", "reference+exact", "high"))
    store.record_brief(at=dt.datetime(2026, 11, 10, 7))
    snap = store.brief_snapshot(AS_OF)
    assert snap.previous_brief_at.tzinfo is not None
    naive = with_hash(Draft("dn", "stage14", "C1", ("INV-2",), "en", "S", "B", ("a@b.example",), dt.datetime(2026, 11, 10, 7)))
    store.create_draft(naive)
    store.create_draft(draft())
    assert store.get_draft("dn").created_at.tzinfo is not None
    assert "## Awaiting approval (2)" in morning_brief(store, AS_OF, "en")


def test_unapplied_payments_excludes_matched_ones(store):
    store.add_payments(read_bank_csv(BANK_CSV).records)
    assert [p.id for p in store.unapplied_payments()] == ["B1", "B2"]
    store.apply_match(Match("B1", "INV-1", D("1250.00"), "EUR", "reference+exact", "high"), at=NOW)
    assert [p.id for p in store.unapplied_payments()] == ["B2"]


def test_two_phase_send_refuses_a_blind_retry_while_reserved(store):
    d = draft()
    store.create_draft(d)
    store.approve_draft("d1", d.hash, at=NOW)
    key = store.reserve_send("d1", d.hash, "smtp", at=NOW)
    assert store.get_draft("d1").status == "approved" and store.list_sends()[0].status == "reserved"
    with pytest.raises(AlreadySentError):
        store.reserve_send("d1", d.hash, "smtp", at=NOW)
    with pytest.raises(AlreadySentError):
        store.record_send("d1", d.hash, "smtp", at=NOW)
    store.confirm_send(key, at=NOW)
    sent = store.list_sends()[0]
    assert store.get_draft("d1").status == "sent" and sent.status == "sent" and sent.sent_at == NOW
    with pytest.raises(ConflictError):
        store.confirm_send(key, at=NOW)
    d2 = draft("d2", "C1", ("INV-2",))
    store.create_draft(d2)
    store.approve_draft("d2", d2.hash, at=NOW)
    key2 = store.reserve_send("d2", d2.hash, "smtp", at=NOW)
    store.fail_send(key2, "connection refused", at=NOW)
    assert store.get_draft("d2").status == "approved" and store.list_sends()[1].status == "failed"
    assert store.reserve_send("d2", d2.hash, "smtp", at=NOW) == key2, "a failed send can be retried under the same key"


def test_reimport_of_an_invoice_export_keeps_language_email_and_contact(store):
    store.upsert_customers([Customer("C1", "Northgate Sample Studio Ltd", "cs", "ann@northgate.example", "Ann Example")])
    store.upsert_customers([Customer("C1", "Northgate Sample Studio Ltd", "en", "", "")], mode="fill")
    c = store.get_customer("C1")
    assert c.language == "cs" and c.email == "ann@northgate.example" and c.contact_name == "Ann Example"
    store.upsert_customers([Customer("C1", "Northgate Sample Studio Limited", "en", "", "")])
    c = store.get_customer("C1")
    assert c.name == "Northgate Sample Studio Limited" and c.language == "en" and c.email == "ann@northgate.example", "a blank never clears a value"
    store.upsert_customers([Customer("northgate-sample-studio-ltd", "Northgate Sample Studio Ltd", "cs", "x@y.example", "Ann")])
    xero = "InvoiceNumber,ContactName,InvoiceDate,DueDate,Total,AmountDue,Currency,Status\nGA-1,Northgate Sample Studio Ltd,2026-10-01,2026-10-31,10,10,EUR,AUTHORISED\n"
    store.ingest(read_invoices("xero", xero), at=NOW)
    c = store.get_customer("northgate-sample-studio-ltd")
    assert c.language == "cs" and c.email == "x@y.example", "an invoice export only fills blanks"
    with pytest.raises(ValueError):
        store.upsert_customers([], mode="overwrite")


def test_brief_snapshot_carries_payments_since_the_previous_brief(store):
    store.add_payments(read_bank_csv(BANK_CSV).records)
    store.apply_match(Match("B1", "INV-1", D("1250.00"), "EUR", "reference+exact", "high"), at=NOW)
    store.create_draft(draft("d1"))
    first = store.brief_snapshot(AS_OF)
    assert isinstance(first, BriefSnapshot) and first.previous_brief_at is None
    assert [p.match.payment_id for p in first.payments] == ["B1"] and first.payments[0].customer.id == "C1" and first.payments[0].payment.date == dt.date(2026, 11, 6)
    assert [d.id for d in first.pending_drafts] == ["d1"]
    assert set(first.invoices) == {"INV-1", "INV-2", "INV-3", "INV-4", "INV-5", "INV-6"} and first.customers["C2"].language == "cs"
    store.record_brief(NOW + dt.timedelta(hours=1))
    later = store.brief_snapshot(AS_OF + dt.timedelta(days=1))
    assert later.previous_brief_at == NOW + dt.timedelta(hours=1)
    assert later.payments == (), "nothing applied since the previous brief"
