"""demo/: the fake fixtures produce every scenario the demo needs as of
2026-11-10, end to end through importers, matching, chasing, Figure Lock,
the store and the brief."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from helpers import OLD_BRAND

from cashdesk.brief import morning_brief
from cashdesk.chasing import plan_followups
from cashdesk.figure_lock import iban_valid, ledger_figures, revise
from cashdesk.importers import read_bank_csv, read_customers, read_invoices
from cashdesk.matching import match_payments
from cashdesk.store import DuplicateImportError, Store

D = Decimal
NBSP = " "
DEMO = Path(__file__).resolve().parent.parent / "demo"
DEMO_DATE = dt.date(2026, 11, 10)
SENDER = "Example Studio s.r.o."
PUBLIC_IBANS = {"GB29NWBK60161331926819", "CZ6508000000192000145399", "DE89370400440532013000", "GB82WEST12345698765432", "NL91ABNA0417164300", "AT611904300234573201", "SK3112000000198742637541"}
FOREIGN_IBAN = "GB82 WEST 1234 5698 7654 32"


@pytest.fixture(scope="module")
def files():
    customers = read_customers(DEMO / "customers.csv")
    invoices = read_invoices("generic", DEMO / "invoices.csv")
    day1 = read_bank_csv(DEMO / "bank_day1.csv")
    day2 = read_bank_csv(DEMO / "bank_day2.csv")
    return customers, invoices, day1, day2


def test_fixtures_parse_cleanly_and_are_plainly_fake(files):
    customers, invoices, day1, day2 = files
    for result in files:
        assert result.ok, result.errors
    assert 6 <= len(customers.records) <= 8
    assert {c.language for c in customers.records} == {"en"}, "the demo is in English for an international audience"
    assert {i.currency for i in invoices.records} == {"EUR"}, "one currency; the currency setting can relabel it"
    assert {i.iban for i in invoices.records} <= PUBLIC_IBANS and all(iban_valid(i.iban) for i in invoices.records)
    assert all(c.email.endswith(".example") for c in customers.records)
    assert {i.customer_id for i in invoices.records} <= {c.id for c in customers.records}
    assert (DEMO / "README.md").read_text(encoding="utf-8").count("2026-11-10") >= 1
    for path in DEMO.glob("*"):
        text = path.read_text(encoding="utf-8")
        assert OLD_BRAND not in text and "FutureInvoked" not in text


def test_day_one_has_one_exact_match_and_one_fuzzy_candidate(files):
    customers, invoices, day1, _ = files
    result = match_payments(invoices.records, day1.records, customers.records, as_of=DEMO_DATE)
    assert [m.invoice_number for m in result.applied] == ["INV-2026-0045"]
    assert len(result.review) == 1 and result.unmatched == () and result.credit_notes == ()
    (item,) = result.review
    assert item.kind == "MISSING" and item.candidates and item.candidates[0].invoice_number == "INV-2026-0044"
    assert item.holds == (), "a near-miss candidate does not hold the invoice; the stage-28 draft still lists it"


def run_demo() -> tuple[Store, dict]:
    """Day 1 and day 2 of the demo through the store, as the plugin would run them."""
    store = Store(":memory:")
    out: dict = {}
    for name in ("customers", "invoices"):
        reader = read_customers if name == "customers" else (lambda p: read_invoices("generic", p))
        store.ingest(reader(DEMO / f"{name}.csv"), at=dt.datetime(2026, 11, 10, 7, tzinfo=dt.timezone.utc))
    for day, stamp in (("bank_day1", dt.datetime(2026, 11, 10, 7, 5, tzinfo=dt.timezone.utc)), ("bank_day2", dt.datetime(2026, 11, 10, 7, 30, tzinfo=dt.timezone.utc))):
        bank = read_bank_csv(DEMO / f"{day}.csv")
        store.ingest(bank, at=stamp)
        result = match_payments(store.list_invoices(), store.unapplied_payments(), store.list_customers(), as_of=DEMO_DATE)
        for m in result.applied:
            store.apply_match(m, at=stamp)
        plan = plan_followups(store.list_invoices(), store.list_customers(), result.applied, store.chase_state(), DEMO_DATE,
                              sender=SENDER, credit_notes={n: i.detail for i in result.credit_notes for n in i.named}, holds=result.held_invoices, created_at=stamp)
        store.create_drafts(plan.all_drafts, at=stamp)
        store.save_chase_state(plan.state, at=stamp)
        out[day] = (result, plan)
    return store, out


def test_day_one_plan_covers_every_stage_the_backlog_reminder_the_void_and_the_dispute():
    store, out = run_demo()
    _, plan = out["bank_day1"]
    kinds = {d.customer_id: d.kind for d in plan.drafts}
    assert kinds == {"C001": "stage07", "C002": "stage14", "C003": "stage21", "C004": "stage28", "C005": "stage28"}
    assert {d.language for d in plan.drafts} == {"en"}
    stonebridge = next(d for d in plan.drafts if d.customer_id == "C005")
    assert plan.escalations == (), "Stonebridge is 36 days overdue on the first run: the customer is reminded first"
    assert stonebridge.subject == "Urgent: invoice INV-2026-0027 is 36 days overdue (EUR 12,100.00)" and "It is now 36 days past its due date" in stonebridge.body
    for d in plan.drafts:
        assert "earlier reminder" not in d.body and "Second reminder" not in d.subject, "nothing was ever sent, so no draft claims history"
    assert {h.customer_id: h.reason for h in plan.held} == {"C006": "dispute"}
    assert set(next(h for h in plan.held if h.customer_id == "C006").invoice_numbers) == {"INV-2026-0033", "INV-2026-0040"}
    assert plan.voided == ("INV-2026-0038",)
    assert [t.customer_id for t in plan.thanks] == ["C007"] and plan.thanks[0].language == "en" and "EUR 1,980.00" in plan.thanks[0].subject
    birchfield = next(d for d in plan.drafts if d.customer_id == "C004")
    assert birchfield.invoice_numbers == ("INV-2026-0031", "INV-2026-0044")
    bluebell = next(d for d in plan.drafts if d.customer_id == "C002")
    assert bluebell.invoice_numbers == ("INV-2026-0042",) and "EUR 1,250.00" in bluebell.subject
    assert store.get_invoice("INV-2026-0042").balance == D("1250.00")


def test_day_two_payment_auto_matches_and_thanks_without_drafting_twice():
    _, out = run_demo()
    result, plan = out["bank_day2"]
    assert [m.invoice_number for m in result.applied] == ["INV-2026-0043"]
    assert [i.payment.id for i in result.review] == ["B1-02"], "day one's unplaced payment is still open; nothing applied is re-queued"
    assert plan.drafts == () and plan.escalations == (), "same day, same stages: nothing is drafted twice"
    assert [t.customer_id for t in plan.thanks] == ["C008"] and plan.thanks[0].language == "en"
    assert "EUR 3,025.00" in plan.thanks[0].subject
    assert ("C001", "stage07") in plan.already_drafted and ("C005", "stage28") in plan.already_drafted


def test_figure_lock_demo_on_the_stage_14_draft():
    store, out = run_demo()
    _, plan = out["bank_day1"]
    draft = next(d for d in plan.drafts if d.customer_id == "C002")
    allowed = ledger_figures(store.list_invoices(customer_id="C002"))
    reworded = draft.body.replace("We have not yet received payment for the invoice below.", "Our records still show the invoice below as open; could you take a look?")
    assert reworded != draft.body and revise(draft, reworded, allowed).ok
    bad = draft.body.replace("EUR 1,250.00", "€1,520.00") + f"\nYou can also pay to IBAN {FOREIGN_IBAN}.\n"
    assert bad.count("€1,520.00") >= 1
    result = revise(draft, bad, allowed)
    assert not result.ok and result.draft == draft
    assert any("EUR 1,250.00 -> EUR 1,520.00" in line for line in result.lock.diff), result.lock.diff
    assert any("GB82WEST12345698765432" in line for line in result.lock.diff), result.lock.diff
    assert len(result.lock.diff) == 2


def test_store_refuses_the_same_bank_day_twice_and_the_brief_reads_it_all():
    store, out = run_demo()
    with pytest.raises(DuplicateImportError):
        store.ingest(read_bank_csv(DEMO / "bank_day1.csv"))
    snap = store.status_snapshot(DEMO_DATE)
    assert snap.awaiting_approval == 7, "five reminders, two thank-yous"
    assert snap.on_hold == 1 and snap.open_count == 8, "eleven invoices, one void, two paid"
    brief = morning_brief(store, DEMO_DATE, "en")
    assert "INV-2026-0045 Fernhill Test Media Ltd: EUR 1,980.00" in brief and "INV-2026-0043 Lindenway Logistics Ltd: EUR 3,025.00" in brief
    assert "Total per currency: EUR 5,005.00" in brief
    assert "Oakridge Placeholder Partners LLP (INV-2026-0033 disputed)" in brief
    assert "## Awaiting approval (7)" in brief
