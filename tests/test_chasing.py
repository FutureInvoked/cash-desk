"""chasing.py: stages 7/14/21/28 by the oldest invoice, one draft per customer,
nothing drafted twice, thank-yous once, escalation past 30 days once the
stage-28 reminder exists, dispute and hold rules, refunds as credit-note
questions, reply classification."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from cashdesk.chasing import ESCALATE_AFTER, STAGES, ChaseState, FollowupPlan, Reply, classify_reply, plan_followups, stage_for
from cashdesk.model import Customer, Invoice, Match

D = Decimal
AS_OF = dt.date(2026, 11, 10)
SENDER = "Example Studio s.r.o."
IBAN_EUR = "DE89370400440532013000"
IBAN_CZK = "CZ6508000000192000145399"


def cust(cid, name, lang="en", email=None, contact=None, **kw):
    email = f"{cid.lower()}@example.test" if email is None else email
    contact = "Jana Vzorová" if contact is None else contact
    return Customer(cid, name, lang, email, contact, **kw)


def inv(no, cid, due, amount, cur, status="open", balance=None, ref=None):
    due_date = dt.date.fromisoformat(due)
    bal = D(amount) if balance is None else D(balance)
    if status == "paid":
        bal = D("0")
    return Invoice(no, cid, due_date - dt.timedelta(days=14), due_date, D(amount), cur, bal, status, ref or no.replace("INV-", "2026"), IBAN_EUR if cur == "EUR" else IBAN_CZK)


CUSTOMERS = [
    cust("C1", "Northgate Sample Studio Ltd"),
    cust("C2", "Modrá Dílna Demo s.r.o.", "cs"),
    cust("C3", "Zelený Vrch Ukázka a.s.", "cs"),
    cust("C4", "Birchfield Example Consulting GmbH"),
    cust("C5", "Kamenný Most Fiktivní s.r.o.", "cs"),
    cust("C6", "Oakridge Placeholder Partners LLP"),
    cust("C7", "Fernhill Test Media Ltd"),
    cust("C8", "Lípa Vzorová Logistika s.r.o.", "cs"),
    cust("C9", "No Mail Ltd", email=""),
    cust("C10", "No Name Ltd", contact=""),
    cust("C11", "Bez Jména s.r.o.", "cs", contact=""),
    cust("C12", "Two Currency GmbH"),
]
INVOICES = [
    inv("INV-C1-1", "C1", "2026-11-03", "980.00", "EUR"),          # 7 days -> stage07
    inv("INV-C1-2", "C1", "2026-11-07", "120.00", "EUR"),          # 3 days: listed in the same draft
    inv("INV-C1-3", "C1", "2026-11-20", "300.00", "EUR"),          # not yet due
    inv("INV-C1-V", "C1", "2026-10-01", "450.00", "EUR", "void"),  # never chased
    inv("INV-C2-1", "C2", "2026-10-27", "1250.00", "EUR"),         # 14 -> stage14
    inv("INV-C3-1", "C3", "2026-10-20", "48400.00", "CZK"),        # 21 -> stage21
    inv("INV-C4-1", "C4", "2026-10-13", "3600.00", "EUR"),         # 28 -> stage28
    inv("INV-C4-2", "C4", "2026-10-31", "720.00", "EUR"),          # 10
    inv("INV-C5-1", "C5", "2026-10-05", "121000.00", "CZK"),       # 36 -> escalation
    inv("INV-C6-1", "C6", "2026-10-16", "2400.00", "EUR", "disputed"),
    inv("INV-C6-2", "C6", "2026-10-24", "600.00", "EUR"),
    inv("INV-C7-1", "C7", "2026-11-12", "1980.00", "EUR", "paid"),
    inv("INV-C8-1", "C8", "2026-11-07", "30250.00", "CZK", "paid"),
    inv("INV-C9-1", "C9", "2026-10-31", "100.00", "EUR"),
    inv("INV-C10-1", "C10", "2026-10-31", "100.00", "EUR"),
    inv("INV-C11-1", "C11", "2026-10-31", "100.00", "EUR"),
    inv("INV-C12-1", "C12", "2026-10-11", "1000.00", "EUR"),       # 30 days: still stage28, not escalated
    inv("INV-C12-2", "C12", "2026-10-12", "25000.00", "CZK"),      # 29
]
APPLIED = [Match("B1-01", "INV-C7-1", D("1980.00"), "EUR", "reference+exact", "high"), Match("B2-01", "INV-C8-1", D("30250.00"), "CZK", "reference+exact", "high")]


def plan(**kw) -> FollowupPlan:
    kw.setdefault("sender", SENDER)
    kw.setdefault("created_at", dt.datetime(2026, 11, 10, 7))
    return plan_followups(kw.pop("invoices", INVOICES), kw.pop("customers", CUSTOMERS), kw.pop("applied", APPLIED), kw.pop("state", ChaseState()), kw.pop("as_of", AS_OF), **kw)


def by_customer(drafts):
    return {d.customer_id: d for d in drafts}


def test_stage_for():
    assert STAGES == ((7, "stage07"), (14, "stage14"), (21, "stage21"), (28, "stage28")) and ESCALATE_AFTER == 30
    assert [stage_for(d) for d in (0, 6, 7, 13, 14, 20, 21, 27, 28, 30, 60)] == [None, None, "stage07", "stage07", "stage14", "stage14", "stage21", "stage21", "stage28", "stage28", "stage28"]


def test_stages_by_the_oldest_invoice_one_draft_per_customer():
    p = plan()
    drafts = by_customer(p.drafts)
    assert {c: d.kind for c, d in drafts.items()} == {"C1": "stage07", "C2": "stage14", "C3": "stage21", "C4": "stage28", "C5": "stage28", "C10": "stage07", "C11": "stage07", "C12": "stage28"}
    assert drafts["C1"].invoice_numbers == ("INV-C1-1", "INV-C1-2"), "every overdue invoice of the customer, oldest first"
    assert drafts["C4"].invoice_numbers == ("INV-C4-1", "INV-C4-2")
    assert drafts["C2"].language == "cs" and "1 250,00 EUR" in drafts["C2"].subject
    assert "INV-C1-3" in p.not_due and "INV-C1-3" not in drafts["C1"].body
    assert all(d.status == "pending" and d.hash for d in p.drafts)


def test_two_currencies_are_totalled_apart():
    d = by_customer(plan().drafts)["C12"]
    assert "CZK 25,000.00 + EUR 1,000.00" in d.subject and "26,000" not in d.body


def test_void_is_never_chased():
    p = plan()
    assert p.voided == ("INV-C1-V",)
    assert "INV-C1-V" not in by_customer(p.drafts)["C1"].body


def test_dispute_holds_the_whole_customer():
    p = plan()
    assert "C6" not in by_customer(p.drafts)
    held = {h.customer_id: h for h in p.held}
    assert held["C6"].reason == "dispute" and set(held["C6"].invoice_numbers) == {"INV-C6-1", "INV-C6-2"} and "INV-C6-1" in held["C6"].detail


def test_a_backlog_invoice_past_30_days_gets_a_customer_reminder_first_then_the_escalation():
    """Kamenný Most's invoice is 36 days overdue on the first run: the customer is reminded (real age,
    no history claim), and the internal escalation note follows a week later if it is still open."""
    p = plan()
    reminder = by_customer(p.drafts)["C5"]
    assert reminder.kind == "stage28" and reminder.recipients == ("c5@example.test",) and "36 dní po splatnosti" in reminder.body
    assert "předchozí" not in reminder.body and "Navazujeme" not in reminder.body
    assert p.escalations == () and p.state.drafted["INV-C5-1"] == "stage28@2026-11-10"
    same_day = plan(state=p.state)
    assert same_day.escalations == () and ("C5", "stage28") in same_day.already_drafted
    six_days = plan(state=p.state, as_of=AS_OF + dt.timedelta(days=6))
    assert "C5" not in by_customer(six_days.drafts) and "C5" not in by_customer(six_days.escalations)
    week = plan(state=p.state, as_of=AS_OF + dt.timedelta(days=7))
    esc = by_customer(week.escalations)["C5"]
    assert esc.kind == "escalation" and esc.recipients == () and "43" in esc.body and "INV-C5-1: připomínka 28" in esc.body
    assert week.state.drafted["INV-C5-1"] == "escalation" and "C5" not in by_customer(week.drafts)
    assert "C12" not in {e.customer_id for e in p.escalations}, "30 days is stage 28; escalation starts after 30"


def test_an_escalation_follows_a_stage_28_reminder_drafted_by_day_30():
    ledger = [inv("INV-E", "C1", "2026-10-13", "500.00", "EUR")]
    first = plan(invoices=ledger, customers=CUSTOMERS[:1])
    assert [d.kind for d in first.drafts] == ["stage28"] and first.state.drafted["INV-E"] == "stage28"
    later = plan(invoices=ledger, customers=CUSTOMERS[:1], state=first.state, as_of=AS_OF + dt.timedelta(days=3))
    assert later.drafts == () and [e.customer_id for e in later.escalations] == ["C1"]


def test_weekly_runs_do_not_skip_a_stage_passed_between_runs():
    """Day 27 drafts stage 21; the next weekly run is day 34: stage 28 is drafted, not skipped for the escalation."""
    ledger = [inv("INV-W", "C1", "2026-10-14", "500.00", "EUR")]
    day27 = plan(invoices=ledger, customers=CUSTOMERS[:1])
    assert [d.kind for d in day27.drafts] == ["stage21"]
    day34 = plan(invoices=ledger, customers=CUSTOMERS[:1], state=day27.state, as_of=AS_OF + dt.timedelta(days=7))
    assert [d.kind for d in day34.drafts] == ["stage28"] and day34.escalations == ()
    assert "34 days past its due date" in day34.drafts[0].body and "34 days overdue" in day34.drafts[0].subject
    day41 = plan(invoices=ledger, customers=CUSTOMERS[:1], state=day34.state, as_of=AS_OF + dt.timedelta(days=14))
    assert day41.drafts == () and [e.customer_id for e in day41.escalations] == ["C1"]
    day20 = plan(invoices=ledger, customers=CUSTOMERS[:1], as_of=AS_OF - dt.timedelta(days=7))
    day27b = plan(invoices=ledger, customers=CUSTOMERS[:1], state=ChaseState({"INV-W": "stage07"}), as_of=AS_OF)
    assert [d.kind for d in day20.drafts] == ["stage14"] and [d.kind for d in day27b.drafts] == ["stage21"]


def test_after_an_escalation_a_new_invoice_of_the_same_customer_is_chased_on_its_own():
    ledger = [inv("INV-OLD", "C1", "2026-09-01", "800.00", "EUR"), inv("INV-NEW", "C1", "2026-11-03", "50.00", "EUR")]
    state = ChaseState({"INV-OLD": "escalation"})
    p = plan(invoices=ledger, customers=CUSTOMERS[:1], state=state)
    (d,) = p.drafts
    assert d.kind == "stage07" and d.invoice_numbers == ("INV-NEW",) and "INV-OLD" not in d.body and "EUR 50.00" in d.subject
    assert p.escalations == () and ("C1", "escalation") in p.already_drafted
    week = plan(invoices=ledger, customers=CUSTOMERS[:1], state=p.state, as_of=AS_OF + dt.timedelta(days=7))
    assert [x.kind for x in week.drafts] == ["stage14"] and week.drafts[0].invoice_numbers == ("INV-NEW",)


def test_drafts_claim_earlier_reminders_only_when_one_was_sent():
    ledger = [inv("INV-H", "C1", "2026-10-27", "500.00", "EUR")]
    first_contact = plan(invoices=ledger, customers=CUSTOMERS[:1]).drafts[0]
    assert first_contact.kind == "stage14" and first_contact.subject.startswith("Reminder: invoice INV-H is 14 days overdue")
    assert "earlier" not in first_contact.body and "Second" not in first_contact.subject
    one_sent = plan(invoices=ledger, customers=CUSTOMERS[:1], reminders_sent=["INV-H"]).drafts[0]
    assert one_sent.subject.startswith("Second reminder: ") and "This follows our earlier reminder. " in one_sent.body
    two_sent = plan(invoices=ledger, customers=CUSTOMERS[:1], reminders_sent={"INV-H": 2}).drafts[0]
    assert two_sent.subject.startswith("Further reminder: ") and "our earlier reminders." in two_sent.body
    other = plan(invoices=ledger, customers=CUSTOMERS[:1], reminders_sent={"INV-ELSE": 3}).drafts[0]
    assert "earlier" not in other.body, "a reminder about another invoice is not history for this one"


def test_an_open_credit_note_holds_the_customers_reminder():
    ledger = [inv("INV-B", "C1", "2026-10-22", "900.00", "EUR"), inv("CN-B", "C1", "2026-10-22", "-300.00", "EUR"), inv("INV-O", "C4", "2026-10-22", "100.00", "EUR")]
    p = plan(invoices=ledger, customers=[CUSTOMERS[0], CUSTOMERS[3]])
    assert "C1" not in by_customer(p.drafts) and "C4" in by_customer(p.drafts)
    item = next(h for h in p.held if h.customer_id == "C1")
    assert item.reason == "credit_note" and set(item.invoice_numbers) == {"INV-B", "CN-B"} and "CN-B" in item.detail
    applied = [inv("INV-B", "C1", "2026-10-22", "900.00", "EUR"), inv("CN-B", "C1", "2026-10-22", "-300.00", "EUR", status="void")]
    assert "C1" in by_customer(plan(invoices=applied, customers=CUSTOMERS[:1]).drafts), "a void credit note holds nothing"


def test_thank_you_on_applied_payment_in_the_customers_language():
    p = plan()
    thanks = by_customer(p.thanks)
    assert set(thanks) == {"C7", "C8"}
    assert thanks["C7"].kind == "thanks" and "EUR 1,980.00" in thanks["C7"].subject and thanks["C7"].invoice_numbers == ("INV-C7-1",)
    assert thanks["C8"].language == "cs" and "30 250,00 Kč" in thanks["C8"].subject
    assert p.state.was_thanked("INV-C7-1", "B1-01") and p.state.was_thanked("INV-C8-1", "B2-01")
    assert "INV-C7-1" not in p.state.drafted, "a thank-you is not a reminder stage"


def test_a_thank_you_for_a_part_payment_does_not_stop_later_stages():
    ledger = [inv("INV-P", "C1", "2026-11-03", "500.00", "EUR", balance="300.00")]
    applied = [Match("B9", "INV-P", D("200.00"), "EUR", "manual", "high")]
    first = plan(invoices=ledger, customers=CUSTOMERS[:1], applied=applied)
    assert [t.invoice_numbers for t in first.thanks] == [("INV-P",)] and {d.kind for d in first.drafts} == {"stage07"}
    later = plan(invoices=ledger, customers=CUSTOMERS[:1], applied=applied, state=first.state, as_of=AS_OF + dt.timedelta(days=7))
    assert {d.kind for d in later.drafts} == {"stage14"} and later.thanks == () and ("C1", "thanks") in later.already_drafted
    second_payment = [Match("B10", "INV-P", D("100.00"), "EUR", "manual", "high")]
    again = plan(invoices=ledger, customers=CUSTOMERS[:1], applied=second_payment, state=later.state, as_of=AS_OF + dt.timedelta(days=7))
    assert [t.invoice_numbers for t in again.thanks] == [("INV-P",)], "each payment is thanked once"
    much_later = plan(invoices=ledger, customers=CUSTOMERS[:1], applied=[], state=again.state, as_of=AS_OF + dt.timedelta(days=30))
    assert [d.kind for d in much_later.drafts] == ["stage28"] and much_later.escalations == (), "day 37: the passed stage 28 first"
    after = plan(invoices=ledger, customers=CUSTOMERS[:1], applied=[], state=much_later.state, as_of=AS_OF + dt.timedelta(days=37))
    assert [e.customer_id for e in after.escalations] == ["C1"]


def test_nothing_is_drafted_twice():
    first = plan()
    assert first.state.drafted["INV-C1-1"] == "stage07" and first.state.drafted["INV-C4-1"] == "stage28" and first.state.drafted["INV-C4-2"] == "stage07"
    second = plan(state=first.state)
    assert second.drafts == () and second.thanks == () and second.escalations == ()
    assert ("C1", "stage07") in second.already_drafted and ("C5", "stage28") in second.already_drafted and ("C7", "thanks") in second.already_drafted
    week = plan(state=second.state, as_of=AS_OF + dt.timedelta(days=7))
    kinds = {d.customer_id: d.kind for d in week.drafts}
    assert kinds == {"C1": "stage14", "C2": "stage21", "C3": "stage28", "C4": "stage14", "C10": "stage14", "C11": "stage14"}
    assert by_customer(week.drafts)["C4"].invoice_numbers == ("INV-C4-2",), "the escalated invoice drops out; the newer one keeps its schedule"
    assert {e.customer_id for e in week.escalations} == {"C4", "C5", "C12"}, "Birchfield and Two Currency crossed 30 days; Kamenný Most's backlog reminder is a week old"
    esc = {e.customer_id: e for e in week.escalations}["C4"]
    assert "INV-C4-1: reminder 28" in esc.body
    third = plan(state=week.state, as_of=AS_OF + dt.timedelta(days=7))
    assert third.drafts == () and third.escalations == ()


def test_state_round_trips_through_json():
    state = plan().state
    again = ChaseState.from_json(state.to_json())
    assert again.drafted == state.drafted
    assert ChaseState.from_json("").drafted == {} and ChaseState.from_json("{}").drafted == {}
    s = ChaseState()
    s.record("X", "stage14")
    s.record("X", "stage07")
    assert s.drafted["X"] == "stage14", "a lower stage never overwrites a higher one"
    with pytest.raises(ValueError):
        s.record("X", "thanks")
    s.record_thanks("X", "P1")
    assert s.was_thanked("X", "P1") and not s.was_thanked("X", "P2") and s.drafted["X"] == "stage14"
    assert ChaseState.from_json(s.to_json()).thanked == {"X:P1"}


def test_credit_note_question_suppresses_the_thank_you_and_holds_the_invoice():
    p = plan(credit_notes={"INV-C7-1": "held: refund B9 - credit-note question"})
    assert "C7" not in by_customer(p.thanks)
    held = [h for h in p.held if h.reason == "credit_note"]
    assert held and held[0].customer_id == "C7" and held[0].invoice_numbers == ("INV-C7-1",)
    assert not p.state.was_thanked("INV-C7-1", "B1-01")
    p2 = plan(credit_notes=["INV-C4-2"])
    assert by_customer(p2.drafts)["C4"].invoice_numbers == ("INV-C4-1",)


def test_payment_in_review_holds_only_that_invoice():
    p = plan(holds={"INV-C4-2": "held: may be paid by B1-02 (MISSING)"})
    assert by_customer(p.drafts)["C4"].invoice_numbers == ("INV-C4-1",)
    held = {h.invoice_numbers: h for h in p.held if h.reason == "payment_in_review"}
    assert ("INV-C4-2",) in held and "B1-02" in held[("INV-C4-2",)].detail
    p2 = plan(holds={"INV-C1-1": "x", "INV-C1-2": "x"})
    assert "C1" not in by_customer(p2.drafts)


def test_customer_on_hold_gets_nothing():
    held_customer = cust("C1", "Northgate Sample Studio Ltd", on_hold=True, hold_reason="asked us to pause")
    p = plan(customers=[held_customer] + CUSTOMERS[1:])
    assert "C1" not in by_customer(p.drafts)
    item = next(h for h in p.held if h.customer_id == "C1")
    assert item.reason == "on_hold" and item.detail == "asked us to pause"


def test_no_email_is_held_and_listed_no_contact_name_is_greeted_hello():
    p = plan()
    reasons = {h.customer_id: h.reason for h in p.held}
    assert reasons["C9"] == "no_email" and "C10" not in reasons
    assert "C9" not in by_customer(p.drafts)
    assert by_customer(p.drafts)["C10"].body.startswith("Hello,\n"), "no contact name: a plain greeting, not a hold"
    assert by_customer(p.drafts)["C11"].body.startswith("Dobrý den"), "Czech needs no first name"
    nameless_thanks = plan(customers=[cust("C7", "Fernhill Test Media Ltd", contact="")] + CUSTOMERS[:6] + CUSTOMERS[7:])
    assert by_customer(nameless_thanks.thanks)["C7"].body.startswith("Hello,\n")
    no_mail = plan(customers=[cust("C7", "Fernhill Test Media Ltd", email="")] + CUSTOMERS[:6] + CUSTOMERS[7:])
    assert "C7" not in by_customer(no_mail.thanks) and any(h.customer_id == "C7" and h.reason == "no_email" for h in no_mail.held)


def test_dispute_reply_holds_the_customer_and_a_late_reply_is_ignored():
    replies = [Reply("C1", dt.date(2026, 11, 9), "We do not agree with the hours on INV-C1-1 and will not pay it as it stands."),
               Reply("C2", dt.date(2026, 11, 12), "Reklamujeme fakturu, částka je nesprávná.")]
    p = plan(replies=replies)
    assert "C1" not in by_customer(p.drafts) and next(h for h in p.held if h.customer_id == "C1").reason == "dispute"
    assert "C2" in by_customer(p.drafts), "a reply dated after as_of is not yet known"
    assert p.replies[0][1] == "dispute"


def test_refund_request_in_a_reply_is_a_credit_note_question_not_a_dispute():
    p = plan(replies=[Reply("C7", dt.date(2026, 11, 9), "Please refund INV-C7-1, we paid it twice.")])
    assert "C7" not in by_customer(p.thanks)
    item = next(h for h in p.held if h.customer_id == "C7")
    assert item.reason == "credit_note" and item.invoice_numbers == ("INV-C7-1",)
    unnamed = plan(replies=[Reply("C4", dt.date(2026, 11, 9), "We were charged twice in October, please send a refund.")])
    assert "C4" not in by_customer(unnamed.drafts)
    assert set(next(h for h in unnamed.held if h.customer_id == "C4").invoice_numbers) == {"INV-C4-1", "INV-C4-2"}


def test_sender_is_required():
    with pytest.raises(ValueError):
        plan(sender="")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Automatic reply: I am out of office until 26 October", "other"),
        ("We will pay by Friday", "promise"),
        ("We will pay the rest but FAKE-1 is wrong", "dispute"),
        ("Could you resend the PDF?", "other"),
        ("Fakturu uhradíme zítra.", "promise"),
        ("Reklamace: nesouhlasíme", "dispute"),
        ("Automatická odpověď: mimo kancelář", "other"),
        ("Thanks.", "other"),
        ("Thanks - the transfer for GA-113 went out yesterday.", "paid"),
        ("This invoice was already paid last week.", "paid"),
        ("Faktura byla uhrazena včera.", "paid"),
        ("Tuto fakturu FAKE-1002 reklamujeme, částka je nesprávná.", "dispute"),
        ("We disagree with your last invoice, the hours are not what we agreed.", "dispute"),
        ("Platba odešla dnes ráno.", "paid"),
        ("Zaplatíme do konce týdne.", "promise"),
        ("I dispute the 10 Sep charge - wrong price - please refund the difference", "dispute"),
    ],
)
def test_classify_reply(text, expected):
    assert classify_reply(text, "en") == expected
    assert classify_reply(text, "cs") == expected
