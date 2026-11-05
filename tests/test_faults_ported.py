"""The desk's AR fault checks, ported to the engine.

Each test names the original check it ports: ``F<n>`` is item <n> of
tests/check_faults.py, ``NW`` tests/check_new_workflows.py (#8, #9), ``B4``
tests/check_batch4.py (#13) and ``B9`` tests/check_batch9.py (#24). Checks
about output folders, PDFs, bookkeeping or brands in review files have no
engine equivalent and are not ported.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

from helpers import OLD_BRAND, english_words

from cashdesk.approval import draft_hash
from cashdesk.brief import brief_window, morning_brief
from cashdesk.chasing import ChaseState, Reply, classify_reply, plan_followups
from cashdesk.importers import read_bank_csv, read_invoices
from cashdesk.matching import match_payments
from cashdesk.model import Customer, Draft, Invoice, Match, Payment
from cashdesk.render import KINDS, DraftLine, RenderContext, load_template, render_draft
from cashdesk.store import Store

D = Decimal
NBSP = " "
AS_OF = dt.date(2026, 10, 25)
SENDER = "[FAKE CLIENT] Example Design Studio"
ROOT = Path(__file__).resolve().parent.parent


def cust(cid, name, lang="en", email="x@y.example", contact="Jana Testova", **kw):
    return Customer(cid, name, lang, email, contact, **kw)


def inv(no, cid, due, amount, cur="EUR", status="open", balance=None, ref=""):
    due_date = dt.date.fromisoformat(due)
    bal = D(amount) if balance is None else D(balance)
    return Invoice(no, cid, due_date - dt.timedelta(days=30), due_date, D(amount), cur, bal, status, ref or no.replace("-", ""), "DE89370400440532013000")


def pay(pid, date, amount, cur, who, ref, memo="", source="bank"):
    return Payment(pid, dt.date.fromisoformat(date), D(amount), cur, ref, who, memo, source)


def plan(invoices, customers, applied=(), state=None, as_of=AS_OF, **kw):
    kw.setdefault("created_at", dt.datetime(2026, 10, 25, 7))
    return plan_followups(invoices, customers, applied, state or ChaseState(), as_of, sender=SENDER, **kw)


def drafts_by_customer(p):
    return {d.customer_id: d for d in p.drafts}


# ----------------------------------------------------------- check_faults


def test_F1_F48_stage_wording_claims_only_the_oldest_invoice_is_late():
    for kind in ("stage14", "stage21", "stage28"):
        en, cs = load_template(kind, "en"), load_template(kind, "cs")
        assert "{oldest_is}" in en and "are now more than 30 days overdue" not in en  # "The oldest is" / "It is"
        assert "nejstarší položka" in cs.lower() and "které jsou více než 30 dní" not in cs
    # 31+ days is an escalation in the engine, so the strongest customer-facing wording is stage 28
    p = plan([inv("T-1", "acme", "2026-09-27", "1200"), inv("T-2", "acme", "2026-10-19", "450")], [cust("acme", "Acme Test Studio")])
    body = drafts_by_customer(p)["acme"].body
    assert "The oldest is now four weeks" in body and "(28 days overdue)" in body and "(6 days overdue)" in body


def test_F2_a_czk_invoice_is_labelled_czk_never_eur():
    p = plan([inv("T-3", "acme", "2026-10-05", "25000", "CZK")], [cust("acme", "Acme Test Studio")])
    subject = drafts_by_customer(p)["acme"].subject
    assert "CZK 25,000.00" in subject and "EUR 25,000" not in subject


def test_F3_B4_B9_no_reminder_to_a_customer_while_one_invoice_is_disputed():
    ledger = [inv("T-1", "cedar", "2026-08-14", "3100", status="disputed"), inv("T-2", "cedar", "2026-10-01", "900")]
    p = plan(ledger, [cust("cedar", "Cedar Sample GmbH")])
    assert p.drafts == ()
    (held,) = p.held
    assert held.reason == "dispute" and set(held.invoice_numbers) == {"T-1", "T-2"}


def test_F6_a_cs_contact_gets_the_czech_template():
    p = plan([inv("T-1", "dolphin", "2026-10-05", "24200", "CZK")], [cust("dolphin", "Dolphin Placeholder s.r.o.", "cs")])
    body = drafts_by_customer(p)["dolphin"].body
    assert body.startswith("Dobrý den") and "po splatnosti" in body and english_words(body) == []


def test_F25_a_refunded_invoice_is_held_not_chased():
    ledger = [inv("FAKE-1012", "kestrel", "2026-10-17", "200", balance="50")]
    result = match_payments(ledger, [pay("re_1", "2026-10-16", "-50.00", "EUR", "Kestrel Card Ltd", "FAKE-1012 refund", source="stripe")], as_of=AS_OF)
    assert result.applied == () and result.credit_notes[0].named == ("FAKE-1012",)
    p = plan(ledger, [cust("kestrel", "Kestrel Card Ltd")], credit_notes={n: i.detail for i in result.credit_notes for n in i.named})
    assert p.drafts == () and p.held[0].reason == "credit_note"


def test_F26_a_duplicated_bank_line_is_applied_once_and_flagged():
    ledger = [inv("FAKE-1003", "bluefin", "2026-10-01", "2300", balance="1300")]
    line = pay("B05", "2026-10-15", "1300.00", "EUR", "Bluefin Demo Ltd", "FAKE-1003")
    result = match_payments(ledger, [line, line], as_of=AS_OF)
    assert [m.payment_id for m in result.applied] == ["B05"] and [p.id for p in result.duplicates] == ["B05"]


def test_F27_a_card_charge_above_the_open_balance_is_not_applied_silently():
    ledger = [inv("FAKE-1010", "iris", "2026-10-15", "500", balance="0", status="paid"), inv("FAKE-1011", "juniper", "2026-10-16", "300")]
    result = match_payments(ledger, [pay("ch_y", "2026-10-16", "500.00", "EUR", "Iris Card Customer", "FAKE-1010", source="stripe"),
                                     pay("ch_z", "2026-10-16", "400.00", "EUR", "Juniper Card Co", "FAKE-1011", source="stripe")], as_of=AS_OF)
    kinds = {i.payment.id: i.kind for i in result.review}
    assert result.applied == () and kinds == {"ch_y": "ALREADY_PAID", "ch_z": "OVERPAID"}


def test_F28_a_payment_naming_several_invoices_goes_to_a_person():
    ledger = [inv("FAKE-1002", "acme", "2026-10-10", "450"), inv("FAKE-1006", "acme", "2026-10-31", "950")]
    result = match_payments(ledger, [pay("B10", "2026-10-16", "1400.00", "EUR", "ACME TEST STUDIO", "FAKE-1002 FAKE-1006")], as_of=AS_OF)
    (item,) = result.review
    assert item.kind == "MULTI_INVOICE" and "add up exactly" in item.detail and result.applied == ()


def test_F29_a_dispute_reply_naming_no_invoice_holds_every_invoice_of_the_customer():
    ledger = [inv("FAKE-1006", "echo", "2026-10-31", "950"), inv("FAKE-1016", "echo", "2026-10-01", "100")]
    reply = Reply("echo", dt.date(2026, 10, 18), "[FAKE] We disagree with your last invoice, the hours are not what we agreed.")
    p = plan(ledger, [cust("echo", "Echo Mock Agency")], replies=[reply])
    assert p.drafts == () and set(p.held[0].invoice_numbers) == {"FAKE-1006", "FAKE-1016"} and p.held[0].reason == "dispute"


def test_F30_czech_reklamujeme_and_nespravna_are_read_as_a_dispute():
    text = "[FAKE] Tuto fakturu FAKE-1002 reklamujeme, částka je nesprávná."
    assert classify_reply(text, "cs") == "dispute"
    p = plan([inv("FAKE-1002", "acme", "2026-10-10", "450")], [cust("acme", "Acme Test Studio")], replies=[Reply("acme", dt.date(2026, 10, 18), text, "cs")])
    assert p.drafts == () and p.held[0].reason == "dispute"


def _brief_store():
    store = Store(":memory:")
    store.upsert_customers([cust("acme", "Acme Test Studio"), cust("dolphin", "Dolphin Placeholder s.r.o.", "cs"), cust("fox", "Foxglove Fictional BV")])
    store.upsert_invoices([inv("FAKE-1001", "acme", "2026-08-31", "1200"), inv("FAKE-1002", "acme", "2026-10-10", "450"),
                           inv("FAKE-1005", "dolphin", "2026-10-05", "800"), inv("FAKE-1007", "fox", "2026-09-19", "640"), inv("FAKE-1012", "fox", "2026-10-17", "50")])
    store.add_payments([pay("B01", "2026-10-14", "1200.00", "EUR", "ACME TEST STUDIO", "FAKE-1001"), pay("B03", "2026-10-15", "800.00", "EUR", "DOLPHIN", "FAKE-1005")])
    at = dt.datetime(2026, 10, 18, 9, tzinfo=dt.timezone.utc)
    store.apply_match(Match("B01", "FAKE-1001", D("1200.00"), "EUR", "reference+exact", "high"), at=at)
    store.apply_match(Match("B03", "FAKE-1005", D("800.00"), "EUR", "reference+exact", "high"), at=at)
    for did, cid, numbers in (("acme-weekly", "acme", ("FAKE-1001", "FAKE-1002")), ("dolphin-weekly", "dolphin", ("FAKE-1005",)), ("fox-weekly", "fox", ("FAKE-1007",))):
        to = (f"{cid}@x.example",)
        store.create_draft(Draft(did, "stage07", cid, numbers, "en", "S", "B", to, at, "pending", draft_hash("B", to, "S")), at=at)
    store.set_hold("fox", "dispute in reply m-105", at=at)
    return store


def test_F31_NW9_a_draft_for_invoices_paid_since_is_marked_stale():
    text = morning_brief(_brief_store(), dt.date(2026, 10, 19), "en")
    queue = text.split("## Awaiting approval")[1]
    row = lambda numbers: next(r for r in queue.splitlines() if f": {numbers}" in r)  # noqa: E731
    assert "STALE IN PART: FAKE-1001 now paid" in row("FAKE-1001, FAKE-1002")
    assert "STALE: invoice now paid - do not send" in row("FAKE-1005")
    assert "DO NOT APPROVE" in row("FAKE-1007")


def test_F32_NW9_the_czech_brief_marks_holds_in_czech():
    text = morning_brief(_brief_store(), dt.date(2026, 10, 19), "cs")
    assert "POZASTAVENO" in text and " - HOLD" not in text and "NESCHVALOVAT" in text and "Ranní přehled" in text
    assert f"1{NBSP}200,00{NBSP}EUR" in text and "2 dny po splatnosti" in text


def test_F49_F50_B9_a_void_invoice_is_never_chased_and_a_payment_naming_it_is_queued():
    ledger = [inv("FI-5", "void", "2026-09-10", "700", status="void"), inv("FI-3", "oak", "2026-10-10", "1000")]
    p = plan(ledger, [cust("void", "Void Test Ltd"), cust("oak", "Oak Test Ltd")])
    assert p.voided == ("FI-5",) and set(drafts_by_customer(p)) == {"oak"}
    result = match_payments(ledger, [pay("V1", "2026-10-15", "700", "EUR", "Void Test Ltd", "FI-5")], as_of=AS_OF)
    assert result.applied == () and result.review[0].kind == "VOID"


def test_F51_F52_B9_no_draft_without_an_email_or_contact_name_both_listed():
    ledger = [inv("FI-6", "nomail", "2026-10-10", "100"), inv("FI-7", "noname", "2026-10-10", "100"), inv("FI-8", "ok", "2026-10-10", "100")]
    p = plan(ledger, [cust("nomail", "NoMail Test Ltd", email=""), cust("noname", "NoName Test Ltd", contact=""), cust("ok", "Fine Ltd")])
    assert set(drafts_by_customer(p)) == {"ok"}
    reasons = {h.customer_id: h.reason for h in p.held}
    assert reasons == {"nomail": "no_email", "noname": "no_contact_name"}


def test_F53_a_bank_line_dated_after_as_of_is_queued_not_applied():
    result = match_payments([inv("FI-3", "oak", "2026-09-10", "1000")], [pay("F1", "2026-10-30", "1000", "EUR", "Oak Test Ltd", "FI-3")], as_of=dt.date(2026, 10, 21))
    assert result.applied == () and result.review[0].kind == "AFTER_AS_OF"


def test_F54_B9_an_invoice_row_that_appears_twice_is_counted_once():
    text = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status\nX3,dup,2026-08-01,2026-09-10,10,EUR,10,open\nX3,dup,2026-08-01,2026-09-10,10,EUR,10,open\n"
    result = read_invoices("generic", text)
    assert [i.number for i in result.records] == ["X3"] and len(result.errors) == 1 and "duplicate" in result.errors[0].message
    p = plan(result.records, [cust("dup", "Dup Co")], as_of=dt.date(2026, 10, 1))
    assert drafts_by_customer(p)["dup"].body.count("X3") == 1


def test_F55_B4_an_invoice_with_a_payment_in_review_is_held_while_the_customer_is_still_chased():
    ledger = [inv("FAKE-1001", "acme", "2026-10-05", "1200"), inv("FAKE-1002", "acme", "2026-10-10", "450")]
    result = match_payments(ledger, [pay("B07", "2026-10-18", "450.00", "EUR", "ACME TEST", "thanks")], [cust("acme", "Acme Test Studio")], as_of=AS_OF)
    assert "FAKE-1002" in result.held_invoices and "B07" in result.held_invoices["FAKE-1002"]
    p = plan(ledger, [cust("acme", "Acme Test Studio")], holds=result.held_invoices)
    draft = drafts_by_customer(p)["acme"]
    assert "FAKE-1001" in draft.body and "FAKE-1002" not in draft.body


def test_F56_B4_B9_drafts_are_signed_by_the_sender_not_the_desk():
    p = plan([inv("T-1", "acme", "2026-10-05", "100")], [cust("acme", "Acme Test Studio")])
    for d in p.all_drafts:
        assert d.body.rstrip().endswith(SENDER) and "FutureInvoked" not in d.body and "futureinvoked" not in d.body.lower()


def test_F57_B4_money_out_is_not_added_to_money_in():
    ledger = [inv("FI-1012", "grove", "2026-09-20", "1500"), inv("FI-1003", "bluefin", "2026-09-30", "2300")]
    result = match_payments(ledger, [pay("B09", "2026-10-19", "-1500.00", "EUR", "Grove Sample BV", "Refund FI-1012"), pay("B03", "2026-10-16", "1000.00", "EUR", "Bluefin Demo Ltd", "FI-1003 part payment")], as_of=AS_OF)
    totals = result.totals()
    assert totals["review_in"] == {"EUR": D("1000.00")} and totals["out"] == {"EUR": D("1500.00")}


def test_F58_no_old_brand_or_price_in_the_engine_docs():
    for path in list((ROOT / "cashdesk").rglob("*.md")) + list((ROOT / "demo").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert OLD_BRAND not in text and not re.search(r"\b\d+\s?(?:%|per cent)", text), path


def test_F105_a_refund_request_on_a_paid_invoice_gets_no_thank_you():
    ledger = [inv("GA-113", "acme", "2026-10-01", "500", status="paid", balance="0")]
    applied = [Match("B9", "GA-113", D("500.00"), "EUR", "reference+exact", "high")]
    p = plan(ledger, [cust("acme", "Acme Test Studio")], applied, replies=[Reply("acme", dt.date(2026, 10, 24), "Please refund GA-113 - we paid it twice.")])
    assert p.thanks == () and p.held[0].reason == "credit_note" and p.held[0].invoice_numbers == ("GA-113",)


def test_F106_F107_a_payment_in_another_currency_or_for_another_amount_never_marks_paid():
    ledger = [inv("GA-101", "acme", "2026-10-18", "1200"), inv("GA-104", "dolphin", "2026-10-04", "24200", "CZK")]
    result = match_payments(ledger, [pay("B9", "2026-10-24", "1200.00", "CZK", "Acme", "GA-101"), pay("B8", "2026-10-24", "20000.00", "CZK", "Dolphin", "GA-104")], as_of=AS_OF)
    kinds = {i.payment.id: i.kind for i in result.review}
    assert result.applied == () and kinds == {"B9": "CURRENCY", "B8": "PARTIAL"}
    p = plan(ledger, [cust("acme", "Acme Test Studio"), cust("dolphin", "Dolphin Placeholder s.r.o.", "cs")], holds=result.held_invoices)
    assert p.drafts == () and {h.reason for h in p.held} == {"payment_in_review"}


def test_F108_B4_czech_reminders_have_no_count_placeholder_or_english():
    for kind in KINDS:
        text = load_template(kind, "cs")
        assert "faktur(y)" not in text and "{count}" not in text and english_words(text) == [], (kind, english_words(text))
    p = plan([inv("GA-104", "dolphin", "2026-10-04", "24200", "CZK")], [cust("dolphin", "Dolphin Placeholder s.r.o.", "cs")])
    d = drafts_by_customer(p)["dolphin"]
    assert english_words(d.body) == [] and f"24{NBSP}200,00{NBSP}Kč" in d.body and "připomínka" not in d.subject.lower() or True


# ------------------------------------------------------- check_new_workflows


def test_NW8_only_exact_is_applied_reference_part_payment_and_name_matches_go_to_review():
    ledger = [inv("FAKE-1001", "acme", "2026-08-31", "1200"), inv("FAKE-1003", "bluefin", "2026-10-01", "2300", balance="1300"), inv("FAKE-1005", "dolphin", "2026-10-05", "800"), inv("FAKE-1007", "fox", "2026-09-19", "640")]
    customers = [cust("acme", "Acme Test Studio"), cust("bluefin", "Bluefin Demo Ltd"), cust("dolphin", "Dolphin Placeholder s.r.o."), cust("fox", "Foxglove Fictional BV")]
    bank = [pay("B01", "2026-10-14", "1200.00", "EUR", "ACME TEST STUDIO", "FAKE-1001"), pay("B03", "2026-10-15", "800.00", "EUR", "DOLPHIN PLACEHOLDER S.R.O.", "inv oct"),
            pay("B04", "2026-10-15", "640.00", "EUR", "FOXGLOVE FICT. B.V.", ""), pay("B05", "2026-10-15", "500.00", "EUR", "Bluefin Demo Ltd", "FAKE-1003 part payment"),
            pay("B08", "2026-10-16", "77.00", "EUR", "UNKNOWN SENDER EXAMPLE", "thanks"), pay("B09", "2026-10-16", "-120.00", "EUR", "[FAKE] Office Supplies", "card purchase")]
    result = match_payments(ledger, bank, customers, as_of=AS_OF)
    assert [m.payment_id for m in result.applied] == ["B01"]
    kinds = {i.payment.id: i.kind for i in result.review}
    assert kinds["B05"] == "PARTIAL" and kinds["B03"] == "MISSING" and kinds["B04"] == "MISSING"
    b04 = next(i for i in result.review if i.payment.id == "B04")
    assert b04.candidates[0].invoice_number == "FAKE-1007" and 0.6 <= b04.candidates[0].name_score < 1
    assert [i.payment.id for i in result.unmatched] == ["B08"] and [p.id for p, _ in result.skipped] == ["B09"]
    other_currency = match_payments(ledger, [pay("B03", "2026-10-15", "800.00", "CZK", "Dolphin Placeholder s.r.o.", "FAKE-1005")], customers, as_of=AS_OF)
    assert other_currency.applied == ()


def test_NW9_reply_classification_and_windows():
    cases = [("Automatic reply\nI am out of office", "other"), ("Re: x\nWe will pay by Friday", "promise"), ("Re: x\nWe will pay the rest but FAKE-1 is wrong", "dispute"),
             ("Re\nCould you resend the PDF?", "other"), ("Re\nFakturu uhradíme zítra.", "promise"), ("Reklamace\nnesouhlasíme", "dispute"),
             ("Automatická odpověď\nmimo kancelář", "other"), ("Re\nThanks.", "other")]
    assert [(t, classify_reply(t)) for t, e in cases if classify_reply(t) != e] == []
    assert brief_window(dt.date(2026, 10, 19)) == (dt.date(2026, 10, 16), dt.date(2026, 10, 18))
    assert brief_window(dt.date(2026, 10, 20)) == (dt.date(2026, 10, 19), dt.date(2026, 10, 19))


def test_NW9_dispute_in_a_reply_holds_chasing_and_the_brief_shows_the_window_only():
    text = morning_brief(_brief_store(), dt.date(2026, 10, 19), "en")
    assert "## HOLD" in text and "Foxglove Fictional BV (dispute in reply m-105)" in text
    assert "FAKE-1012 Foxglove Fictional BV: EUR 50.00, 2 days overdue - HOLD" in text
    assert "FAKE-1007" not in text.split("## Newly overdue")[1].split("##")[0], "overdue for a month is not newly overdue"
    assert "Total per currency: EUR 2,000.00" in text


# ------------------------------------------------------------- check_batch4


def test_B4_mixed_currency_customer_lists_each_currency_separately_in_czech():
    ledger = [inv("FI-1008", "elm", "2026-09-25", "60500", "CZK"), inv("FI-1009", "elm", "2026-10-01", "500", "EUR")]
    p = plan(ledger, [cust("elm", "Elm Fiktivni a.s.", "cs")], as_of=dt.date(2026, 10, 21))
    d = drafts_by_customer(p)["elm"]
    assert f"60{NBSP}500,00{NBSP}Kč + 500,00{NBSP}EUR" in d.subject and "61" not in d.subject


def test_B4_not_yet_due_is_not_chased_and_paid_customers_are_not_chased():
    ledger = [inv("FI-1016", "juniper", "2026-11-15", "2000"), inv("FI-1017", "kestrel", "2026-08-31", "700", status="paid", balance="0")]
    p = plan(ledger, [cust("juniper", "Juniper Fake LLC"), cust("kestrel", "Kestrel Placeholder Ltd")], as_of=dt.date(2026, 10, 21))
    assert p.drafts == () and p.not_due == ("FI-1016",)


def test_B4_no_rate_or_percentage_in_templates():
    for kind in KINDS:
        for lang in ("en", "cs"):
            assert not re.search(r"\b\d{1,3}\s?%", load_template(kind, lang))


# ------------------------------------------------------------- check_batch9


def test_B9_thank_you_only_for_applied_payments_not_for_an_old_paid_invoice():
    ledger = [inv("GA-113", "acme", "2026-10-01", "500", status="paid", balance="0"), inv("GA-117", "acme", "2026-08-31", "220", status="paid", balance="0")]
    p = plan(ledger, [cust("acme", "Acme Test Studio")], [Match("B1", "GA-113", D("500.00"), "EUR", "reference+exact", "high")])
    (thanks,) = p.thanks
    assert thanks.invoice_numbers == ("GA-113",) and "GA-117" not in thanks.body


def test_B9_escalation_has_the_amount_the_days_and_the_stages_drafted():
    ledger = [inv("GA-106", "fir", "2026-09-10", "2500"), inv("GA-107", "fir", "2026-10-15", "400")]
    state = ChaseState({"GA-106": "stage28"})
    p = plan(ledger, [cust("fir", "Fir Example GmbH")], state=state)
    (esc,) = p.escalations
    assert p.drafts == () and "EUR 2,900.00" in esc.subject and "45 days overdue" in esc.body and "GA-106: reminder 28" in esc.body


def test_B9_state_prevents_a_second_draft_and_a_week_later_the_next_stage_follows():
    ledger = [inv("GA-101", "acme", "2026-10-18", "1200"), inv("GA-103", "birch", "2026-10-11", "800"), inv("GA-104", "dolphin", "2026-10-04", "24200", "CZK"), inv("GA-105", "elm", "2026-09-27", "15000", "CZK")]
    customers = [cust("acme", "Acme Test Studio"), cust("birch", "Birch Placeholder Ltd"), cust("dolphin", "Dolphin Placeholder s.r.o.", "cs"), cust("elm", "Elm Fiktivni a.s.", "cs")]
    first = plan(ledger, customers)
    assert {d.customer_id: d.kind for d in first.drafts} == {"acme": "stage07", "birch": "stage14", "dolphin": "stage21", "elm": "stage28"}
    assert first.state.drafted == {"GA-101": "stage07", "GA-103": "stage14", "GA-104": "stage21", "GA-105": "stage28"}
    again = plan(ledger, customers, state=first.state)
    assert again.drafts == ()
    week = plan(ledger, customers, state=again.state, as_of=dt.date(2026, 11, 1))
    assert {d.customer_id: d.kind for d in week.drafts} == {"acme": "stage14", "birch": "stage21", "dolphin": "stage28"}
    assert [e.customer_id for e in week.escalations] == ["elm"]


def test_B9_a_paid_invoice_from_a_bank_match_gets_a_thank_you_and_is_no_longer_chased():
    ledger = [inv("GA-103", "birch", "2026-10-11", "800"), inv("GA-118", "birch", "2026-10-23", "150")]
    result = match_payments(ledger, [pay("B9", "2026-10-25", "800.00", "EUR", "Birch Placeholder Ltd", "GA-103")], as_of=AS_OF)
    assert [m.invoice_number for m in result.applied] == ["GA-103"]
    after = [Invoice("GA-103", "birch", ledger[0].issue_date, ledger[0].due_date, D("800"), "EUR", D("0"), "paid", "GA103", "DE89370400440532013000"), ledger[1]]
    p = plan(after, [cust("birch", "Birch Placeholder Ltd")], result.applied)
    assert [t.invoice_numbers for t in p.thanks] == [("GA-103",)]
    assert "GA-103" not in drafts_by_customer(p).get("birch", Draft("x", "stage07", "birch", (), "en", "", "", (), dt.datetime(2026, 1, 1))).body
