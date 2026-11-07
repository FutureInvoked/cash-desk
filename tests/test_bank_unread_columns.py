"""P2z: bank direction or lifecycle kept in descriptive columns never becomes cash.

An unsigned Amount whose direction is only in Type/Transaction Type, and
Sparkasse lines whose 'Umsatz vorgemerkt' sits in Info or Buchungsstatus, used
to be applied as customer receipts with a thank-you. All data is fake; every
refusal has a genuine credit control beside it.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from cashdesk import Customer, Invoice, Store, plan_followups
from cashdesk.importers import read_bank_csv

DAY = dt.date(2026, 11, 12)


def ledger():
    s = Store(":memory:", today=lambda: DAY)
    s.upsert_customers([Customer("C1", "Acme Ltd", email="ap@acme.example")])
    s.upsert_invoices([Invoice("INV-0041", "C1", dt.date(2026, 10, 1), dt.date(2026, 10, 31),
                               D("1200.00"), "GBP", D("1200.00"), "open", "INV-0041")])
    return s


def run(text):
    """Import, match and apply like the plugin does, then plan follow-ups."""
    s = ledger()
    r = read_bank_csv(text)
    if r.records or r.bank_observations:
        s.ingest(r, file_name="bank.csv")
    applied = list(s.match_result(DAY).applied)
    for m in applied:
        s.apply_match(m)
    p = plan_followups(s.list_invoices(), s.list_customers(), s.verified_matches(), s.chase_state(), DAY,
                       sender="Demo Studio", holds=s.reminder_holds(DAY))
    inv = {i.number: i for i in s.list_invoices()}["INV-0041"]
    return r, applied, p, inv


UNSIGNED_TYPE = "Date,{col},Counterparty,Reference,Amount,Currency\n2026-11-04,{val},ACME LTD,INV-0041,1200.00,GBP\n"


@pytest.mark.parametrize("col", ["Type", "Transaction Type", "TXN_TYPE", "Entry Type"])
@pytest.mark.parametrize("val", ["DEBIT", "debit", "DR", "DBIT", "Soll", "Withdrawal", "OUT"])
def test_unsigned_amount_with_debit_in_type_column_is_money_out_not_a_receipt(col, val):
    r, applied, p, inv = run(UNSIGNED_TYPE.format(col=col, val=val))
    assert r.ok and r.records[0].amount == D("-1200.00")
    assert applied == [] and not p.thanks
    assert inv.status == "open" and inv.balance == D("1200.00")


@pytest.mark.parametrize("col", ["Type", "Transaction Type"])
@pytest.mark.parametrize("val", ["CREDIT", "cr", "CRDT", "Haben"])
def test_unsigned_amount_with_credit_in_type_column_still_applies(col, val):
    r, applied, p, inv = run(UNSIGNED_TYPE.format(col=col, val=val))
    assert r.ok and r.records[0].amount == D("1200.00")
    assert len(applied) == 1 and inv.balance == D("0.00") and p.thanks


@pytest.mark.parametrize("val", ["Direct Debit", "SEPA-Lastschrift", "ATM withdrawal", "Outgoing transfer", "Debit card", "Transfer Out",
                                 "ACH_DEBIT", "DIRECTDEBIT", "Card Payment", "FEE", "Kartenzahlung", "POS", "CHECK", "Dauerauftrag", "D", "S"])
def test_unread_debit_wording_beside_unsigned_amount_refuses_the_file(val):
    r, applied, p, inv = run(UNSIGNED_TYPE.format(col="Type", val=val))
    assert not r.ok and r.records == ()
    assert "bank direction evidence is unread" in r.errors[0].message
    assert applied == [] and not p.thanks and inv.balance == D("1200.00")


@pytest.mark.parametrize("val,amount,expected", [
    ("FASTER PAYMENT", "1200.00", "1200.00"),    # Starling: descriptive Type, signed amount
    ("Faster payment", "1200.00", "1200.00"),    # Monzo
    ("TRANSFER", "1200.00", "1200.00"),          # Revolut
    ("REFUND", "1200.00", "1200.00"),            # merchant refund in a signed file
    ("Direct Debit", "-1200.00", "-1200.00"),    # signed outgoing with debit wording agrees
    ("DEBIT", "-1200.00", "-1200.00"),           # exact debit on a signed negative
])
def test_descriptive_type_values_keep_signed_files_working(val, amount, expected):
    r = read_bank_csv(UNSIGNED_TYPE.format(col="Type", val=val).replace("1200.00,GBP", amount + ",GBP"))
    assert r.ok, r.errors
    assert r.records[0].amount == D(expected)


def test_credit_type_on_negative_amount_and_type_vs_direction_disagreement_conflict():
    r = read_bank_csv(UNSIGNED_TYPE.format(col="Type", val="CREDIT").replace("1200.00,GBP", "-1200.00,GBP"))
    assert not r.ok and "bank amount evidence conflicts" in r.errors[0].message
    r = read_bank_csv("Date,Type,CdtDbtInd,Counterparty,Amount,Currency\n2026-11-04,DEBIT,CRDT,ACME LTD,1200.00,GBP\n")
    assert not r.ok and "the Type column disagrees with the signed amount" in r.errors[0].message


SPARKASSE = "Buchungstag;Beguenstigter/Zahlungspflichtiger;Verwendungszweck;Betrag;Waehrung;{col}\n04.11.2026;ACME LTD;INV-0041;1.200,00;GBP;{val}\n"


@pytest.mark.parametrize("col,val", [("Info", "Umsatz vorgemerkt"), ("Status", "Umsatz vorgemerkt"), ("Status", "vorgemerkt"),
                                     ("Buchungsstatus", "vorgemerkt"), ("Buchungsstatus", "Umsatz vorgemerkt"),
                                     ("Umsatzstatus", "noch nicht gebucht"), ("Info", "umsatz  storniert"), ("Status", "PDNG")])
def test_sparkasse_pending_lines_in_info_or_buchungsstatus_are_not_cash(col, val):
    r, applied, p, inv = run(SPARKASSE.format(col=col, val=val))
    assert r.records == () and r.skipped and r.skipped[0].code == "not_booked"
    assert applied == [] and not p.thanks and inv.balance == D("1200.00")


@pytest.mark.parametrize("col,val", [("Info", "Umsatz gebucht"), ("Buchungsstatus", "gebucht"), ("Status", "BOOK"),
                                     ("Info", "Kartenzahlung Filiale 12"), ("Info", "")])
def test_booked_sparkasse_lines_and_unrelated_info_text_still_apply(col, val):
    r, applied, p, inv = run(SPARKASSE.format(col=col, val=val))
    assert r.ok, r.errors
    assert r.records[0].amount == D("1200.00") and len(applied) == 1 and inv.balance == D("0.00")


@pytest.mark.parametrize("val", ["Umsatz vorgemerkt seit 04.11.", "Pending review", "Vormerkung", "Reservierung", "authorisation hold"])
def test_unread_lifecycle_wording_in_info_refuses_the_file(val):
    r, applied, p, inv = run(SPARKASSE.format(col="Info", val=val))
    assert not r.ok and r.records == ()
    assert "names a lifecycle Cash Desk cannot read" in r.errors[0].message
    assert applied == [] and inv.balance == D("1200.00")


def test_info_disagreeing_with_status_column_conflicts():
    text = "Buchungstag;Betrag;Waehrung;Status;Info\n04.11.2026;1.200,00;GBP;gebucht;Umsatz vorgemerkt\n"
    r = read_bank_csv(text)
    assert not r.ok and "bank status evidence conflicts" in r.errors[0].message


def test_original_cp1252_sparkasse_pending_only_export_is_skipped(tmp_path):
    """The bookkeeper's g_sparkasse_pending_only file, byte for byte in cp1252."""
    text = ('"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";"Beguenstigter/Zahlungspflichtiger";'
            '"Kontonummer/IBAN";"Betrag";"Waehrung";"Info"\n'
            '"DE89370400440532013000";"04.11.26";"04.11.26";"GUTSCHR. UEBERWEISUNG";"INV-0041";"ACME LTD";'
            '"GB29NWBK60161331926819";"1.200,00";"GBP";"Umsatz vorgemerkt"\n')
    path = tmp_path / "sparkasse_pending.csv"
    path.write_bytes(text.encode("cp1252"))
    r, applied, p, inv = run(str(path))
    assert r.records == () and applied == [] and not p.thanks and inv.balance == D("1200.00")


def test_camt_style_type_wording_beside_cdtdbtind_keeps_the_receipt():
    text = ("Booking Date,Amount,Currency,CdtDbtInd,Transaction Type,Name,Reference\n"
            "2026-11-02,50.00,GBP,DBIT,SEPA Direct Debit,Gas Co,GAS-11\n"
            "2026-11-03,1200.00,GBP,CRDT,SEPA Credit Transfer,ACME LTD,INV-0041\n")
    r, applied, p, inv = run(text)
    assert r.ok, r.errors
    assert [x.amount for x in r.records] == [D("-50.00"), D("1200.00")]
    assert len(applied) == 1 and inv.balance == D("0.00")


def test_signed_statement_keeps_descriptive_types_including_a_direct_debit_refund():
    text = ("Date,Counter Party,Reference,Type,Amount (GBP),Balance (GBP)\n"
            "01/11/2026,Office Co,RENT,DIRECT DEBIT,-80.00,20.00\n"
            "02/11/2026,Gas Co,REFUND,DIRECT DEBIT,12.50,32.50\n"
            "03/11/2026,ACME LTD,INV-0041,FASTER PAYMENT,1200.00,1232.50\n")
    r = read_bank_csv(text, date_order="dmy")
    assert r.ok, r.errors
    assert [x.amount for x in r.records] == [D("-80.00"), D("12.50"), D("1200.00")]


def test_type_word_contradicting_a_signed_amount_is_a_conflict_not_a_flip():
    text = "Date,Type,Name,Amount,Currency\n2026-11-01,CREDIT,Office Co,-80.00,GBP\n2026-11-02,DEBIT,ACME LTD,1200.00,GBP\n"
    r = read_bank_csv(text)
    assert r.records == () or all(x.amount < 0 for x in r.records)
    assert any("the Type column disagrees with the signed amount" in e.message for e in r.errors)


@pytest.mark.parametrize("info", ["Returned goods", "Scheduled transfer from customer", "Card processing fee", "Cancellation fee"])
def test_free_text_info_columns_outside_sparkasse_are_not_refused(info):
    text = f"Date,Amount,Currency,Name,Info\n2026-11-02,-40.00,GBP,Shop,Returned goods\n2026-11-03,1200.00,GBP,ACME LTD,{info}\n"
    r = read_bank_csv(text)
    assert r.ok, r.errors
    assert [x.amount for x in r.records] == [D("-40.00"), D("1200.00")]


def test_sparkasse_info_status_file_refuses_unknown_info_values():
    text = ("Buchungstag;Betrag;Waehrung;Info\n04.11.2026;1.200,00;GBP;Umsatz gebucht\n"
            "05.11.2026;300,00;GBP;Umsatz in Klärung\n")
    r = read_bank_csv(text)
    assert not r.ok and r.records == ()
    assert "names a lifecycle Cash Desk cannot read" in r.errors[0].message
