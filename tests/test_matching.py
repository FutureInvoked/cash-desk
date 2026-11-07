"""matching.py: a payment is applied only on reference + exact amount in the
same currency; everything else is a review item with fuzzy candidates, a
credit-note question or unmatched. Fixtures port workflow #13's fake bank
export and ledger (B01-B13, FI-1001..FI-1021)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from cashdesk.matching import (
    NAME_MIN,
    Candidate,
    MatchResult,
    ReviewItem,
    invoices_named,
    is_refund,
    match_payments,
    normalise_name,
)
from cashdesk.model import Customer, Invoice, Payment

D = Decimal
AS_OF = dt.date(2026, 10, 21)


def inv(no, cust, issue, due, amount, balance, cur, status="open", vs=""):
    return Invoice(no, cust, dt.date.fromisoformat(issue), dt.date.fromisoformat(due), D(amount), cur, D(balance), status, vs)


def pay(pid, date, amount, cur, counterparty, reference, memo=""):
    return Payment(pid, dt.date.fromisoformat(date), D(amount), cur, reference, counterparty, memo)


CUSTOMERS = {
    "acme": Customer("acme", "Acme Test Studio"),
    "bluefin": Customer("bluefin", "Bluefin Demo Ltd"),
    "cedar": Customer("cedar", "Cedar Sample GmbH"),
    "dolphin": Customer("dolphin", "Dolphin Placeholder s.r.o.", "cs"),
    "elm": Customer("elm", "Elm Fiktivni a.s.", "cs"),
    "fjord": Customer("fjord", "Fjord Example Oy"),
    "grove": Customer("grove", "Grove Sample BV"),
    "harbor": Customer("harbor", "Harbor Demo Ltd"),
    "ivy": Customer("ivy", "Ivy Test s.r.o.", "cs"),
    "juniper": Customer("juniper", "Juniper Fake LLC"),
    "kestrel": Customer("kestrel", "Kestrel Placeholder Ltd"),
    "lumen": Customer("lumen", "Lumen Test Studio"),
    "moss": Customer("moss", "Moss Demo s.r.o.", "cs"),
    "nova": Customer("nova", "Nova Example Ltd"),
    "orchid": Customer("orchid", "Orchid Sample Ltd"),
}

LEDGER = [
    inv("FI-1001", "acme", "2026-08-11", "2026-09-10", "1200", "1200", "EUR", vs="20261001"),
    inv("FI-1002", "acme", "2026-09-10", "2026-10-10", "450", "450", "EUR", vs="20261002"),
    inv("FI-1003", "bluefin", "2026-08-31", "2026-09-30", "2300", "2300", "EUR", vs="20261003"),
    inv("FI-1004", "cedar", "2026-07-15", "2026-08-14", "3100", "3100", "EUR", "disputed", "20261004"),
    inv("FI-1005", "cedar", "2026-08-16", "2026-09-15", "800", "800", "EUR", vs="20261005"),
    inv("FI-1006", "dolphin", "2026-08-06", "2026-09-05", "24200", "24200", "CZK", vs="20261006"),
    inv("FI-1007", "dolphin", "2026-09-15", "2026-10-15", "12100", "12100", "CZK", vs="20261007"),
    inv("FI-1008", "elm", "2026-07-02", "2026-08-01", "60500", "60500", "CZK", vs="20261008"),
    inv("FI-1009", "elm", "2026-09-01", "2026-10-01", "500", "500", "EUR", vs="20261009"),
    inv("FI-1010", "fjord", "2026-09-05", "2026-10-05", "990", "990", "EUR", vs="20261010"),
    inv("FI-1011", "fjord", "2026-09-12", "2026-10-12", "990", "990", "EUR", vs="20261011"),
    inv("FI-1012", "grove", "2026-08-21", "2026-09-20", "1500", "1500", "EUR", vs="20261012"),
    inv("FI-1013", "harbor", "2026-08-26", "2026-09-25", "640", "640", "EUR", vs="20261013"),
    inv("FI-1014", "harbor", "2026-08-26", "2026-09-25", "360", "360", "EUR", vs="20261014"),
    inv("FI-1015", "ivy", "2026-09-01", "2026-10-01", "9680", "9680", "CZK", vs="20261015"),
    inv("FI-1016", "juniper", "2026-10-16", "2026-11-15", "2000", "2000", "EUR", vs="20261016"),
    inv("FI-1017", "kestrel", "2026-08-01", "2026-08-31", "700", "0", "EUR", "paid", "20261017"),
    inv("FI-1018", "lumen", "2026-09-01", "2026-10-01", "300", "300", "EUR", vs="20261018"),
    inv("FI-1019", "moss", "2026-09-04", "2026-10-04", "5000", "5000", "CZK", vs="20261019"),
    inv("FI-1020", "nova", "2026-08-02", "2026-09-01", "1750", "1750", "EUR", vs="20261020"),
    inv("FI-1021", "orchid", "2026-09-12", "2026-10-12", "220", "220", "EUR", vs="20261021"),
]

BANK = [
    pay("B01", "2026-10-14", "1200.00", "EUR", "Acme Test Studio", "Invoice FI-1001"),
    pay("B02", "2026-10-15", "24200.00", "CZK", "Dolphin Placeholder s.r.o.", "VS 20261006"),
    pay("B03", "2026-10-16", "1000.00", "EUR", "Bluefin Demo Ltd", "FI-1003 part payment"),
    pay("B04", "2026-10-16", "9800.00", "CZK", "Ivy Test s.r.o.", "FI-1015"),
    pay("B05", "2026-10-17", "990.00", "EUR", "Harbor Demo Ltd", "FI-1013 FI-1014"),  # not their exact total: reviewed
    pay("B06", "2026-10-18", "990.00", "EUR", "Fjord Exmple OY", "payment"),
    pay("B07", "2026-10-18", "450.00", "EUR", "ACME TEST", "thanks"),
    pay("B08", "2026-10-19", "500.00", "CZK", "Elm Fiktivni a.s.", "FI-1009"),
    pay("B09", "2026-10-19", "-1500.00", "EUR", "Grove Sample BV", "Refund FI-1012"),
    pay("B10", "2026-10-20", "700.00", "EUR", "Kestrel Placeholder Ltd", "FI-1017"),
    pay("B11", "2026-10-20", "333.00", "EUR", "[FAKE] Unknown Payer", "INV 77"),
    pay("B01", "2026-10-14", "1200.00", "EUR", "Acme Test Studio", "Invoice FI-1001"),
    pay("B13", "2026-10-20", "-12.50", "EUR", "[FAKE] Bank", "", "Poplatek za vedeni uctu"),
]


@pytest.fixture(scope="module")
def result() -> MatchResult:
    return match_payments(LEDGER, BANK, CUSTOMERS, as_of=AS_OF)


def kinds(result: MatchResult) -> dict[str, str]:
    out = {item.payment.id: item.kind for item in result.review}
    out.update({item.payment.id: item.kind for item in result.credit_notes})
    out.update({item.payment.id: item.kind for item in result.unmatched})
    return out


def test_only_reference_plus_exact_amount_is_applied(result):
    applied = {m.payment_id: m for m in result.applied}
    assert set(applied) == {"B01", "B02"}
    assert applied["B01"].invoice_number == "FI-1001" and applied["B01"].amount == D("1200.00") and applied["B01"].currency == "EUR"
    assert applied["B02"].invoice_number == "FI-1006", "variable symbol in the reference"
    assert all(m.method == "reference+exact" and m.confidence == "high" for m in result.applied)


def test_review_kinds_port_workflow_13(result):
    k = kinds(result)
    assert k["B03"] == "PARTIAL" and k["B04"] == "OVERPAID" and k["B05"] == "MULTI_INVOICE"
    assert k["B06"] == "AMBIGUOUS" and k["B07"] == "MISSING" and k["B08"] == "CURRENCY"
    assert k["B09"] == "CREDIT_NOTE" and k["B10"] == "ALREADY_PAID" and k["B11"] == "UNMATCHED"
    assert "B13" not in k and "B01" not in k and "B02" not in k


def test_fee_line_skipped_and_duplicate_counted_once(result):
    assert [p.id for p, _ in result.skipped] == ["B13"]
    assert [p.id for p in result.duplicates] == ["B01"]


def test_nothing_in_review_is_applied(result):
    named = {n for item in result.review for n in item.named}
    assert named >= {"FI-1003", "FI-1015", "FI-1013", "FI-1014", "FI-1009"}
    assert not named & {m.invoice_number for m in result.applied}


def test_fuzzy_candidates(result):
    items = {item.payment.id: item for item in result.review}
    b07 = items["B07"].candidates
    assert b07 and b07[0].invoice_number == "FI-1002" and b07[0].amount_score == 1.0 and b07[0].name_score >= NAME_MIN
    b06 = {c.invoice_number for c in items["B06"].candidates}
    assert {"FI-1010", "FI-1011"} <= b06
    assert all(isinstance(c, Candidate) and 0 <= c.score <= 1 for c in b06 and items["B06"].candidates)


def test_held_invoices_while_a_payment_is_in_review(result):
    held = result.held_invoices
    assert {"FI-1002", "FI-1003", "FI-1009", "FI-1010", "FI-1011", "FI-1012", "FI-1013", "FI-1014", "FI-1015"} <= set(held)
    assert "FI-1001" not in held and "FI-1017" not in held and "FI-1006" not in held
    assert "refund" in held["FI-1012"] and "credit-note" in held["FI-1012"]
    assert "B07" in held["FI-1002"]


def test_refund_is_a_credit_note_question_not_a_dispute(result):
    (item,) = result.credit_notes
    assert isinstance(item, ReviewItem) and item.payment.id == "B09" and item.named == ("FI-1012",)
    assert "credit note" in item.detail.lower() and "not a dispute" in item.detail.lower()


def test_detail_texts_carry_house_style_amounts(result):
    items = {item.payment.id: item for item in result.review}
    assert "EUR 1,000.00 received, EUR 2,300.00 open; EUR 1,300.00 would remain" in items["B03"].detail
    assert "CZK 120.00 extra" in items["B04"].detail
    assert "FI-1013, FI-1014" in items["B05"].detail and "EUR 990.00" in items["B05"].detail
    assert "CZK 500.00 received but FI-1009 is in EUR" in items["B08"].detail
    assert "2 open invoices for exactly this amount" in items["B06"].detail
    unmatched = {item.payment.id: item for item in result.unmatched}
    assert "INV 77" in unmatched["B11"].detail


def test_totals_keep_money_in_and_money_out_apart(result):
    totals = result.totals()
    assert totals["applied"] == {"CZK": D("24200.00"), "EUR": D("1200.00")}
    assert totals["review_in"] == {"CZK": D("10300.00"), "EUR": D("4463.00")}
    assert totals["out"] == {"EUR": D("1500.00")}


def test_reference_with_other_separators_and_case_still_matches():
    r = match_payments(LEDGER, [pay("X1", "2026-10-14", "1200.00", "EUR", "Acme Test Studio", "fi 1001")], CUSTOMERS, as_of=AS_OF)
    assert [m.invoice_number for m in r.applied] == ["FI-1001"]


def test_longer_number_is_not_read_as_a_prefix_match():
    r = match_payments(LEDGER, [pay("X2", "2026-10-14", "300.00", "EUR", "Lumen Test Studio", "FI-10181")], CUSTOMERS, as_of=AS_OF)
    assert r.applied == ()
    (item,) = r.review
    assert item.kind == "MISSING" and item.candidates[0].invoice_number == "FI-1018"


def test_payment_dated_after_as_of_is_queued_not_applied():
    r = match_payments(LEDGER, [pay("F1", "2026-10-30", "1200.00", "EUR", "Acme Test Studio", "FI-1001")], CUSTOMERS, as_of=AS_OF)
    assert r.applied == () and r.review[0].kind == "AFTER_AS_OF" and "FI-1001" in r.held_invoices


def test_void_invoice_payment_is_queued_not_applied():
    void = inv("FI-5", "cedar", "2026-08-01", "2026-09-10", "700", "700", "EUR", "void")
    r = match_payments(LEDGER + [void], [pay("V1", "2026-10-15", "700", "EUR", "Void Test Ltd", "FI-5")], CUSTOMERS, as_of=AS_OF)
    assert r.applied == () and r.review[0].kind == "VOID"


def test_exact_amount_and_exact_name_without_reference_is_review_not_applied():
    r = match_payments(LEDGER, [pay("N1", "2026-10-15", "800.00", "EUR", "CEDAR SAMPLE GMBH", "inv oct")], CUSTOMERS, as_of=AS_OF)
    assert r.applied == ()
    (item,) = r.review
    assert item.kind == "MISSING" and item.candidates[0].invoice_number == "FI-1005" and item.candidates[0].name_score == 1.0
    assert "FI-1005" in r.held_invoices


def test_payment_in_another_currency_is_never_applied():
    r = match_payments(LEDGER, [pay("C1", "2026-10-15", "800.00", "CZK", "Cedar Sample GmbH", "FI-1005")], CUSTOMERS, as_of=AS_OF)
    assert r.applied == () and r.review[0].kind == "CURRENCY"


def test_several_invoices_summing_to_the_payment_are_suggested():
    r = match_payments(LEDGER, [pay("S1", "2026-10-17", "1000.00", "EUR", "Harbor Demo Ltd", "payment")], CUSTOMERS, as_of=AS_OF)
    (item,) = r.review
    assert item.kind == "MULTI_INVOICE" and {c.invoice_number for c in item.candidates} == {"FI-1013", "FI-1014"}
    assert "FI-1013 + FI-1014" in item.detail


def test_second_payment_for_an_applied_invoice_in_the_same_run_is_already_paid():
    twice = [pay("P1", "2026-10-14", "450.00", "EUR", "Acme Test Studio", "FI-1002"), pay("P2", "2026-10-15", "450.00", "EUR", "Acme Test Studio", "FI-1002")]
    r = match_payments(LEDGER, twice, CUSTOMERS, as_of=AS_OF)
    assert [m.payment_id for m in r.applied] == ["P1"] and r.review[0].kind == "ALREADY_PAID"


def test_without_customers_names_fall_back_to_ids():
    r = match_payments(LEDGER, [pay("B07", "2026-10-18", "450.00", "EUR", "ACME TEST", "thanks")], as_of=AS_OF)
    assert r.review[0].candidates[0].invoice_number == "FI-1002"


def test_positive_payment_with_refund_words_is_never_applied():
    r = match_payments(LEDGER, [pay("R1", "2026-10-14", "1200.00", "EUR", "Acme Test Studio", "FI-1001 refund of overpayment")], CUSTOMERS, as_of=AS_OF)
    assert r.applied == () and r.credit_notes[0].kind == "CREDIT_NOTE"


@pytest.mark.parametrize("text, expected", [("Refund FI-1012", True), ("dobropis 2026", True), ("vrácení platby", True), ("VRATKA", True), ("credit note", True), ("payment", False), ("Poplatek za vedeni uctu", False), ("", False)])
def test_refund_words(text, expected):
    assert is_refund(text) is expected


@pytest.mark.parametrize("a, b", [("Nová Ukázka s.r.o.", "NOVA UKAZKA SRO"), ("Elm Fiktivni a.s.", "ELM FIKTIVNÍ, A. S."), ("Foxglove Fictional B.V.", "foxglove fictional bv"), ("Acme Test Studio Ltd.", "ACME TEST STUDIO LIMITED")])
def test_normalise_name_ignores_case_accents_punctuation_and_legal_suffixes(a, b):
    assert normalise_name(a) == normalise_name(b)


def test_invoices_named_matches_whole_tokens_only():
    assert invoices_named("Invoice FI-1001 and VS 20261006", LEDGER) == ["FI-1001", "FI-1006"]
    assert invoices_named("FI-10181", LEDGER) == []
    assert invoices_named("fi/1001", LEDGER) == ["FI-1001"]
    assert invoices_named("", LEDGER) == []
