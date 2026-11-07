"""Credit identity and closure must remain consistent across accounting exports.

Fixtures are fake. Approval and send-reservation probes use only isolated SQLite,
never Hermes, email export or SMTP.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest

from cashdesk.chasing import plan_followups
from cashdesk.importers import customer_key, read_customers, read_invoices
from cashdesk.model import Customer, Invoice
from cashdesk.store import ConflictError, NotApprovedError, Store

D = Decimal
TODAY = dt.date(2026, 11, 5)
NOW = dt.datetime(2026, 11, 5, 10, tzinfo=dt.timezone.utc)
FORMATS = [("xero", "ACCRECCREDIT"), ("quickbooks", "Credit Memo"),
           ("quickbooks", "Payment"), ("generic", "credit note")]
HEADER = ["InvoiceNumber", "ContactName", "customer_id", "InvoiceDate", "DueDate", "Total",
          "InvoiceAmountDue", "InvoiceAmountPaid", "Currency", "Type", "Status", "EmailAddress", "Description"]


def export(source, kind, *, remaining="200", status=None, number="CN-1", name="Alpha Studio",
           customer="C1", currency="EUR", gross="200", marker="first", mixed=False, include_id=True):
    status = status if status is not None else ("AUTHORISED" if source == "xero" else "Open")
    credit_sign = -1 if source == "generic" else 1
    row = [number, name, customer, "2026-10-02", "", str(D(gross) * credit_sign),
           str(D(remaining) * credit_sign), "", currency, kind, status, "ap@alpha.example", marker]
    rows = [row]
    if mixed:
        rows.insert(0, ["INV-1", "Alpha Studio", "C1", "2026-10-01", "2026-10-22", "1200", "1200", "0",
                        "EUR", "ACCREC" if source == "xero" else "Invoice", "AUTHORISED" if source == "xero" else "Open",
                        "ap@alpha.example", "services"])
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER if include_id else [v for v in HEADER if v!="customer_id"])
    writer.writerows(rows if include_id else [r[:2]+r[3:] for r in rows])
    return out.getvalue()


def import_credit(store, source, kind, **kw):
    result = read_invoices(source, export(source, kind, **kw))
    assert result.ok, result.errors
    return store.ingest(result, at=NOW, file_name=f"{source}-credits.csv")


def draft_current(store):
    plan = plan_followups(store.list_invoices(), store.list_customers(), store.verified_matches(),
                          store.chase_state(), TODAY, sender="Example Studio", holds=store.reminder_holds(TODAY),
                          created_at=NOW)
    store.create_drafts(plan.all_drafts, at=NOW)
    store.save_chase_state(plan.state, at=NOW)
    return plan


@pytest.fixture
def ledger():
    with Store(":memory:", today=lambda: TODAY) as s:
        s.upsert_customers([Customer("C1", "Alpha Studio", email="ap@alpha.example"),
                            Customer("C2", "Beta Works", email="ap@beta.example")], at=NOW)
        s.upsert_invoices([Invoice("INV-1", "C1", dt.date(2026, 10, 1), dt.date(2026, 10, 22),
                                   D("1200"), "EUR", D("1200"), "open"),
                           Invoice("INV-2", "C2", dt.date(2026, 10, 1), dt.date(2026, 10, 22),
                                   D("500"), "EUR", D("500"), "open")], at=NOW)
        yield s


@pytest.mark.parametrize("source,kind", FORMATS)
@pytest.mark.parametrize("mixed", [False, True], ids=["credit-only", "same-file-invoice"])
def test_credits_share_the_existing_invoice_customer_identity(ledger, source, kind, mixed):
    import_credit(ledger, source, kind, mixed=mixed)
    credit = ledger.get_invoice("CN-1")
    assert credit.customer_id == ledger.get_invoice("INV-1").customer_id == "C1"
    assert credit.balance == D("-200") and credit.status == "open"
    assert {c.id for c in ledger.list_customers()} == {"C1", "C2"}, "no phantom customer for the credit"
    holds = ledger.reminder_holds(TODAY)
    assert "credit" in holds["INV-1"] and "INV-2" not in holds
    plan = draft_current(ledger)
    assert [d.customer_id for d in plan.drafts] == ["C2"]
    assert plan.thanks == ()


@pytest.mark.parametrize("source,kind", FORMATS[:3])
def test_credit_only_export_keeps_customer_contact_for_later_invoice(source, kind):
    with Store(":memory:", today=lambda: TODAY) as s:
        import_credit(s, source, kind)
        customers = s.list_customers()
        assert len(customers) == 1 and customers[0].name == "Alpha Studio"
        assert customers[0].email == "ap@alpha.example"
        assert s.get_invoice("CN-1").customer_id == customers[0].id
        s.upsert_invoices([Invoice("INV-LATER", customers[0].id, dt.date(2026, 10, 1), dt.date(2026, 10, 22),
                                   D("1200"), "EUR", D("1200"), "open")], at=NOW)
        assert "credit" in s.reminder_holds(TODAY)["INV-LATER"]
        assert draft_current(s).drafts == ()


@pytest.mark.parametrize("source,kind", FORMATS)
@pytest.mark.parametrize("status,remaining,expected", [("PAID", "0", "paid"), ("", "0", "paid"),
                                                        ("VOID", "200", "void")])
def test_credit_terminal_updates_close_existing_holds_without_due_date(ledger, source, kind, status, remaining, expected):
    import_credit(ledger, source, kind)
    import_credit(ledger, source, kind, remaining=remaining, status=status, marker="settled weekly export")
    closed = ledger.get_invoice("CN-1")
    assert (closed.customer_id, closed.amount, closed.balance, closed.status) == ("C1", D("-200"), D("0"), expected)
    assert "INV-1" not in ledger.reminder_holds(TODAY)
    assert ledger.get_invoice("INV-1").balance == D("1200"), "closure alone never invents invoice settlement"
    plan = draft_current(ledger)
    assert {d.customer_id for d in plan.drafts} == {"C1", "C2"}
    assert plan.thanks == ()


@pytest.mark.parametrize("source,kind", FORMATS)
def test_weekly_partial_credit_updates_keep_one_identity_and_remaining_hold(ledger, source, kind):
    for week, remaining in enumerate(("200", "80", "80", "0")):
        import_credit(ledger, source, kind, remaining=remaining, marker=f"week {week}")
        credits = [i for i in ledger.list_invoices() if i.number == "CN-1"]
        assert len(credits) == 1 and credits[0].customer_id == "C1"
        assert credits[0].amount == D("-200") and credits[0].balance == -D(remaining)
        assert ("INV-1" in ledger.reminder_holds(TODAY)) == (D(remaining) > 0)
    assert ledger.get_invoice("CN-1").status == "paid"


@pytest.mark.parametrize("source,kind", FORMATS)
@pytest.mark.parametrize("approved", [False, True], ids=["pending", "approved"])
def test_new_credit_invalidates_old_text_and_closure_requires_fresh_approval(ledger, source, kind, approved):
    old = next(d for d in draft_current(ledger).drafts if d.customer_id == "C1")
    if approved:
        ledger.approve_draft(old.id, old.hash, approved_by="fake reviewer", at=NOW)
    import_credit(ledger, source, kind)
    assert ledger.get_draft(old.id).status == "held"
    assert ledger.approval(old.id) is None
    with pytest.raises(ConflictError, match="reminder is paused"):
        ledger.check_draft(old.id, old.hash, at=NOW)
    with pytest.raises(ConflictError, match="reminder is paused"):
        ledger.reserve_send(old.id, old.hash, "eml", at=NOW)
    import_credit(ledger, source, kind, remaining="0", status="PAID", marker="closed")
    assert ledger.approval(old.id) is None
    assert ledger.get_draft(old.id).status == "held"
    with pytest.raises(ConflictError, match="reminder is paused"):
        ledger.approve_draft(old.id, old.hash, approved_by="fake reviewer", at=NOW)
    new = next(d for d in draft_current(ledger).drafts if d.customer_id == "C1")
    assert ledger.get_draft(new.id).status == "pending" and ledger.approval(new.id) is None
    with pytest.raises(NotApprovedError):
        ledger.reserve_send(new.id, new.hash, "eml", at=NOW)
    ledger.approve_draft(new.id, new.hash, approved_by="fake reviewer after rereading", at=NOW)
    ledger.check_draft(new.id, new.hash, at=NOW)


@pytest.mark.parametrize("source,kind", FORMATS)
def test_credit_changes_do_not_rewrite_sent_history_or_other_customer(ledger, source, kind):
    plan = draft_current(ledger)
    sent = next(d for d in plan.drafts if d.customer_id == "C1")
    other = next(d for d in plan.drafts if d.customer_id == "C2")
    for draft in (sent, other):
        ledger.approve_draft(draft.id, draft.hash, approved_by="fake reviewer", at=NOW)
    key = ledger.reserve_send(sent.id, sent.hash, "eml", at=NOW)
    ledger.confirm_send(key, at=NOW)  # isolated ledger bookkeeping only; no delivery function called
    before = tuple(ledger._conn.execute("SELECT * FROM sends").fetchone())
    old_sent = ledger.get_draft(sent.id)
    import_credit(ledger, source, kind)
    import_credit(ledger, source, kind, remaining="0", status="PAID", marker="closed")
    assert ledger.get_draft(sent.id) == old_sent
    assert tuple(ledger._conn.execute("SELECT * FROM sends").fetchone()) == before
    assert ledger.get_draft(other.id).status == "approved" and ledger.approval(other.id) is not None
    ledger.check_draft(other.id, other.hash, at=NOW)


@pytest.mark.parametrize("change", ["source", "customer", "currency", "kind", "invoice-number", "ambiguous-customer"])
def test_credit_identity_conflicts_are_atomic(ledger, change):
    source, kind, kwargs = "quickbooks", "Credit Memo", {}
    if change == "invoice-number":
        kwargs["number"] = "INV-1"
    elif change == "ambiguous-customer":
        ledger.upsert_customers([Customer("C3", "Alpha Studio", email="another@alpha.example")], at=NOW)
    else:
        import_credit(ledger, source, kind)
        kwargs.update(remaining="0", status="PAID", marker="conflicting closure")
        if change == "source":
            source, kind = "xero", "ACCRECCREDIT"
        elif change == "customer":
            kwargs.update(customer="C2", name="Beta Works")
        elif change == "currency":
            kwargs["currency"] = "USD"
        elif change == "kind":
            kind = "Payment"
    incoming = read_invoices(source, export(source, kind, **kwargs))
    assert incoming.ok, incoming.errors
    if change == "customer":
        original=ledger.get_invoice("CN-1")
        ledger.ingest(incoming,at=NOW)
        assert ledger.get_invoice("CN-1")==original and ledger.customer_identity_reviews()
        assert "INV-1" in ledger.reminder_holds(TODAY) and "INV-2" in ledger.reminder_holds(TODAY)
        return
    if change == "ambiguous-customer":
        # Explicit C1 disambiguates equal names; it must not be replaced by C3.
        ledger.ingest(incoming,at=NOW)
        assert ledger.get_invoice("CN-1").customer_id=="C1" and not ledger.customer_identity_reviews()
        return
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError) as caught:
        ledger.ingest(incoming, at=NOW)
    assert "credit identity conflict" in str(caught.value)
    assert "check the accounting export" in str(caught.value)
    assert "\n".join(ledger._conn.iterdump()) == before


def test_regular_invoice_cannot_overwrite_an_existing_credit(ledger):
    import_credit(ledger, "quickbooks", "Credit Memo")
    text = export("quickbooks", "Invoice", remaining="200", marker="regular invoice collision")
    # A regular invoice requires its due date; the existing credit does not supply it.
    text = text.replace("2026-10-02,,", "2026-10-02,2026-10-22,")
    incoming = read_invoices("quickbooks", text)
    assert incoming.ok, incoming.errors
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError, match="credit identity conflict"):
        ledger.ingest(incoming, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before


@pytest.mark.parametrize("source,kind", FORMATS)
def test_unknown_credit_status_does_not_silently_open_or_close(ledger, source, kind):
    import_credit(ledger, source, kind)
    result = read_invoices(source, export(source, kind, remaining="0", status="UNDER REVIEW", marker="unknown status"))
    assert not result.ok
    assert "credit update cannot be trusted" in result.errors[0].message
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError):
        ledger.ingest(result, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before


def test_numberless_terminal_payment_never_closes_an_unidentified_open_credit(ledger):
    import_credit(ledger, "quickbooks", "Payment", number="")
    credits = [i for i in ledger.list_invoices() if i.amount < 0]
    assert len(credits) == 1 and credits[0].balance == D("-200")
    result = read_invoices("quickbooks", export("quickbooks", "Payment", number="", remaining="0", status="PAID", marker="unnamed closed"))
    assert result.ok
    assert any("credit update needs a stable document number" in warning.message for warning in result.warnings)
    ledger.ingest(result, at=NOW)
    assert ledger.get_invoice(credits[0].number).balance == D("-200")
    assert "INV-1" in ledger.reminder_holds(TODAY)


def test_old_orphaned_credit_requires_identity_verification_of_original_customer(ledger):
    old_id = customer_key("Alpha Studio")
    original = Invoice("CN-1", old_id, dt.date(2026,10,2), dt.date(2026,10,2),D("-200"),"EUR",D("-200"),"open",
                       note="credit note from the export; a person decides which invoice it settles")
    ledger.upsert_invoices([original],at=NOW)
    import_credit(ledger,"xero","ACCRECCREDIT",marker="same accounting credit reimported")
    assert ledger.get_invoice("CN-1")==original and ledger.customer_identity_reviews()
    assert "INV-1" in ledger.reminder_holds(TODAY)
    q=ledger.customer_identity_reviews()[0]
    with pytest.raises(ConflictError,match="customer identity review"):
        ledger.confirm_customer_identity("CN-1",customer_id="C1",expected_snapshot=q["snapshot"],reason="Verified new owner",by="fake reviewer")
    assert ledger.get_invoice("CN-1")==original


@pytest.mark.parametrize("source,kind", FORMATS)
def test_paid_credit_with_nonzero_remaining_evidence_refuses(ledger, source, kind):
    import_credit(ledger, source, kind)
    result = read_invoices(source, export(source, kind, remaining="80", status="PAID", marker="contradiction"))
    assert not result.ok and "credit update cannot be trusted" in result.errors[0].message
    assert "nonzero exported balance" in result.errors[0].message
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError):
        ledger.ingest(result, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before
    assert "INV-1" in ledger.reminder_holds(TODAY)


@pytest.mark.parametrize("source,kind", FORMATS)
def test_paid_credit_without_remaining_column_value_can_close_by_status(ledger, source, kind):
    import_credit(ledger, source, kind)
    text = export(source, kind, status="PAID", marker="authoritative terminal status")
    rows = list(csv.reader(io.StringIO(text)))
    rows[1][HEADER.index("InvoiceAmountDue")] = ""
    out = io.StringIO(); writer = csv.writer(out, lineterminator="\n"); writer.writerows(rows)
    result = read_invoices(source, out.getvalue())
    assert result.ok, result.errors
    ledger.ingest(result, at=NOW)
    assert ledger.get_invoice("CN-1").status == "paid"
    assert ledger.get_invoice("CN-1").balance == 0 and "INV-1" not in ledger.reminder_holds(TODAY)


@pytest.mark.parametrize("source,kind", FORMATS)
def test_closed_credit_cannot_be_reopened_by_an_older_open_export(ledger, source, kind):
    import_credit(ledger, source, kind)
    import_credit(ledger, source, kind, remaining="0", status="PAID", marker="closed")
    result = read_invoices(source, export(source, kind, marker="later file containing old open figures"))
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError, match="credit identity conflict"):
        ledger.ingest(result, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before
    assert ledger.get_invoice("CN-1").status == "paid" and "INV-1" not in ledger.reminder_holds(TODAY)


def test_conflicting_duplicate_credit_rows_refuse_the_complete_import(ledger):
    first = export("quickbooks", "Credit Memo")
    second = export("quickbooks", "Credit Memo", remaining="50", marker="second conflicting row")
    combined = first + second.split("\n", 1)[1]
    result = read_invoices("quickbooks", combined)
    assert result.ok, result.errors
    before = "\n".join(ledger._conn.iterdump())
    with pytest.raises(ConflictError, match="credit identity conflict"):
        ledger.ingest(result, at=NOW)
    assert "\n".join(ledger._conn.iterdump()) == before


@pytest.mark.parametrize("source,kind", FORMATS)
def test_zero_gross_terminal_export_preserves_original_credit_amount(ledger, source, kind):
    import_credit(ledger, source, kind)
    import_credit(ledger, source, kind, gross="0", remaining="0", status="PAID", marker="all terminal values zero")
    credit = ledger.get_invoice("CN-1")
    assert (credit.amount, credit.balance, credit.status) == (D("-200"), D("0"), "paid")
    assert "INV-1" not in ledger.reminder_holds(TODAY)


def test_identical_duplicate_credit_rows_count_one_updated_credit(ledger):
    text = export("quickbooks", "Credit Memo")
    duplicate = text + text.split("\n", 1)[1]
    result = read_invoices("quickbooks", duplicate)
    assert result.ok, result.errors
    counts = ledger.ingest(result, at=NOW)
    assert counts["credit_notes"] == 1, "repeated observations are one credit update, not two"
    credits = [i for i in ledger.list_invoices() if i.number == "CN-1"]
    assert len(credits) == 1 and credits[0].balance == D("-200")
    assert "INV-1" in ledger.reminder_holds(TODAY)


def test_legacy_orphan_credit_pauses_chasing_until_reimport(ledger):
    old = next(d for d in draft_current(ledger).drafts if d.customer_id == "C1")
    ledger.approve_draft(old.id, old.hash, approved_by="fake reviewer", at=NOW)
    orphan = Invoice("CN-LEGACY", customer_key("Alpha Studio"), dt.date(2026, 10, 2), dt.date(2026, 10, 2),
                     D("-200"), "EUR", D("-200"), "open",
                     note="credit note from the export; a person decides which invoice it settles")
    ledger.upsert_invoices([orphan], at=NOW)
    before = tuple(ledger._conn.execute("SELECT * FROM invoices WHERE number='CN-LEGACY'").fetchone())
    assert ledger.get_invoice("CN-LEGACY").customer_id != "C1", "the old financial row remains an orphan until verified reimport"
    holds = ledger.reminder_holds(TODAY)
    assert "credit identity needs review" in holds["INV-1"]
    assert "INV-2" not in holds
    assert ledger.get_draft(old.id).status == "held" and ledger.approval(old.id) is None
    with pytest.raises(ConflictError, match="credit identity needs review"):
        ledger.check_draft(old.id, old.hash, at=NOW)
    assert tuple(ledger._conn.execute("SELECT * FROM invoices WHERE number='CN-LEGACY'").fetchone()) == before
    assert not any(d.customer_id == "C1" for d in draft_current(ledger).drafts)
    import_credit(ledger, "xero", "ACCRECCREDIT", number="CN-LEGACY", remaining="0", status="PAID",
                  marker="verified identity and closure reimport")
    assert ledger.get_invoice("CN-LEGACY").customer_id == orphan.customer_id
    assert ledger.get_invoice("CN-LEGACY").balance == D("-200")
    assert ledger.customer_identity_reviews() and "INV-1" in ledger.reminder_holds(TODAY)
    assert ledger.approval(old.id) is None, "unknown history cannot authorize collection"


def test_credit_imported_before_explicit_contacts_holds_canonical_customer_and_can_close():
    with Store(":memory:", today=lambda: TODAY) as s:
        import_credit(s, "xero", "ACCRECCREDIT", include_id=False)
        generated_id = customer_key("Alpha Studio")
        assert s.get_invoice("CN-1").customer_id == generated_id
        assert {c.id for c in s.list_customers()} == {generated_id}
        original_credit = s.get_invoice("CN-1")
        s.ingest(read_customers("customer_id,name,email\nC1,Alpha Studio,ap@alpha.example\n"), at=NOW)
        invoice_export = ("invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status\n"
                          "INV-1,C1,2026-10-01,2026-10-22,1200,EUR,1200,open\n")
        s.ingest(read_invoices("generic", invoice_export), at=NOW)
        assert "INV-1" in s.reminder_holds(TODAY), "recorded credit provenance must not hide its canonical contact"
        assert s.get_invoice("CN-1") == original_credit, "eligibility alone does not rewrite credit ownership"
        assert draft_current(s).drafts == ()
        import_credit(s, "xero", "ACCRECCREDIT", remaining="0", status="PAID", marker="first-week corrected identity and closure", include_id=False)
        credit = s.get_invoice("CN-1")
        assert (credit.customer_id, credit.balance, credit.status) == ("C1", D("0"), "paid")
        assert {c.id for c in s.list_customers()} == {generated_id, "C1"}, "old imported contact history remains"
        assert "INV-1" not in s.reminder_holds(TODAY)
        (fresh,) = draft_current(s).drafts
        assert fresh.customer_id == "C1" and fresh.status == "pending"
        assert s.approval(fresh.id) is None
        with pytest.raises(NotApprovedError):
            s.reserve_send(fresh.id, fresh.hash, "eml", at=NOW)
