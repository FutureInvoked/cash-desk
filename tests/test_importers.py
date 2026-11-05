"""importers.py: column mappings for four invoice sources and bank statements,
row-level errors instead of exceptions, and a content fingerprint."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from cashdesk.importers import SOURCES, ImportResult, customer_key, fingerprint, read_bank_csv, read_customers, read_invoices

D = Decimal

GENERIC = """invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference,iban
INV-2026-0042,C002,2026-10-13,2026-10-27,1250.00,EUR,1250.00,open,20260042,DE89 3704 0044 0532 0130 00
INV-2026-0036,C003,2026-09-20,2026-10-20,48400.00,CZK,48400.00,open,20260036,CZ65 0800 0000 1920 0014 5399
INV-2026-0038,C001,2026-09-01,2026-10-01,450.00,EUR,450.00,void,20260038,DE89 3704 0044 0532 0130 00
"""

XERO = """*InvoiceNumber,*ContactName,EmailAddress,ContactPerson,Language,*InvoiceDate,*DueDate,Total,AmountDue,Currency,Status,PaidDate,Reference
GA-101,Acme Test Studio,jana@acme.example,Jana Testova,en,2026-09-18,2026-10-18,1200.00,1200.00,EUR,AUTHORISED,,
GA-104,Dolphin Placeholder s.r.o.,petr@dolphin.example,Petr Ukazka,cs,2026-09-04,2026-10-04,24200.00,24200.00,CZK,AUTHORISED,,VS 20261104
GA-111,Ivy Sample BV,bram@ivy.example,Bram Voorbeeld,en,2026-08-01,2026-09-01,350.00,350.00,EUR,VOIDED,,
GA-113,Acme Test Studio,jana@acme.example,Jana Testova,en,2026-09-01,2026-10-01,500.00,0.00,EUR,PAID,2026-10-24,
GA-108,Gale Test Oy,liisa@gale.example,Liisa Koe,en,2026-09-01,2026-10-01,600.00,600.00,EUR,DISPUTED,,
GA-120,Draft Co,,,en,2026-10-30,2026-11-29,100.00,100.00,EUR,DRAFT,,
GA-101,Acme Test Studio,jana@acme.example,Jana Testova,en,2026-09-18,2026-10-18,1200.00,1200.00,EUR,AUTHORISED,,
"""

QUICKBOOKS = """Date,Transaction Type,Num,Customer,Due Date,Amount,Open Balance,Status
10/13/2026,Invoice,1042,Birchfield Example Consulting GmbH,10/27/2026,"3,600.00","3,600.00",Overdue
10/20/2026,Invoice,1043,Fernhill Test Media Ltd,11/03/2026,980.00,0.00,Paid
10/21/2026,Payment,,Fernhill Test Media Ltd,,-980.00,0.00,Closed
10/22/2026,Invoice,1044,Oakridge Placeholder Partners LLP,11/05/2026,600.00,600.00,Voided
"""

STRIPE = """id,Number,Customer,Customer Email,Customer Name,Currency,Total,Amount Due,Amount Paid,Amount Remaining,Status,Date (UTC),Due Date (UTC),Paid At (UTC)
in_1Fake001,ABCD1234-0001,cus_Fake001,ann@northgate.example,Northgate Sample Studio Ltd,eur,980.00,980.00,0.00,980.00,open,2026-10-20 09:12,2026-11-03 00:00,
in_1Fake002,ABCD1234-0002,cus_Fake002,petr@modra.example,Modrá Dílna Demo s.r.o.,eur,1250.00,1250.00,1250.00,0.00,paid,2026-10-13 10:00,2026-10-27 00:00,2026-10-26 15:30
in_1Fake003,ABCD1234-0003,cus_Fake001,ann@northgate.example,Northgate Sample Studio Ltd,eur,50.00,50.00,0.00,50.00,uncollectible,2026-08-01 10:00,2026-08-15 00:00,
in_1Fake004,,cus_Fake003,x@y.example,Draft Customer,eur,10.00,10.00,0.00,10.00,draft,2026-11-01 10:00,2026-11-15 00:00,
"""

BANK = """id,date,amount,currency,counterparty,reference,memo
B1-01,2026-11-06,1980.00,EUR,FERNHILL TEST MEDIA LTD,INV-2026-0045,
B1-02,2026-11-06,700.00,EUR,BIRCHFIELD CONSULTING,project fee,
B1-03,2026-11-06,-12.50,EUR,BANK,,account fee
"""

CZECH_BANK = "﻿Datum;Částka;Měna;Název protiúčtu;VS;Zpráva pro příjemce\r\n06.11.2026;30 250,00;CZK;LÍPA VZOROVÁ LOGISTIKA;20260043;faktura\r\n09.11.2026;-1 000,00;CZK;Dodavatel;;vratka\r\n"

CUSTOMERS = """customer_id,name,language,email,contact_name,on_hold,hold_reason
C001,Northgate Sample Studio Ltd,en,ann@northgate.example,Ann Example,no,
C002,Modrá Dílna Demo s.r.o.,cs,petr@modra.example,Petr Ukázka,,
C006,Oakridge Placeholder Partners LLP,en,lee@oakridge.example,Lee Placeholder,yes,dispute on INV-2026-0033
"""


def test_sources_listed():
    assert SOURCES == ("generic", "xero", "quickbooks", "stripe")
    with pytest.raises(ValueError):
        read_invoices("sage", GENERIC)


def test_generic_mapping():
    result = read_invoices("generic", GENERIC)
    assert isinstance(result, ImportResult) and result.kind == "invoices" and result.source == "generic"
    assert result.errors == () and result.rows == 3
    inv = {i.number: i for i in result.records}
    assert inv["INV-2026-0042"].customer_id == "C002"
    assert inv["INV-2026-0042"].amount == D("1250.00") and inv["INV-2026-0042"].balance == D("1250.00")
    assert inv["INV-2026-0042"].issue_date == dt.date(2026, 10, 13) and inv["INV-2026-0042"].due_date == dt.date(2026, 10, 27)
    assert inv["INV-2026-0042"].reference == "20260042"
    assert inv["INV-2026-0042"].iban == "DE89370400440532013000"
    assert inv["INV-2026-0036"].currency == "CZK"
    assert inv["INV-2026-0038"].status == "void"
    assert result.customers == ()


def test_xero_mapping_derives_customers_and_statuses():
    result = read_invoices("xero", XERO)
    inv = {i.number: i for i in result.records}
    assert set(inv) == {"GA-101", "GA-104", "GA-111", "GA-113", "GA-108"}
    assert inv["GA-101"].status == "open" and inv["GA-111"].status == "void" and inv["GA-113"].status == "paid"
    assert inv["GA-108"].status == "disputed"
    assert inv["GA-113"].paid_date == dt.date(2026, 10, 24) and inv["GA-113"].balance == D("0.00")
    assert inv["GA-104"].reference == "20261104" and inv["GA-104"].currency == "CZK"
    customers = {c.id: c for c in result.customers}
    acme, dolphin = customer_key("Acme Test Studio"), customer_key("Dolphin Placeholder s.r.o.")
    assert set(customers) == {acme, dolphin, customer_key("Ivy Sample BV"), customer_key("Gale Test Oy")}
    assert acme.startswith("acme-test-studio-") and dolphin.startswith("dolphin-placeholder-s-r-o-")
    assert customers[dolphin].language == "cs"
    assert customers[acme].email == "jana@acme.example" and customers[acme].contact_name == "Jana Testova"
    assert inv["GA-101"].customer_id == acme
    assert [e.message for e in result.skipped] == ["status DRAFT is not an issued invoice"]
    assert [e.code for e in result.skipped] == ["draft"] and result.skipped_counts == {"draft": 1}
    # the repeated GA-101 row agrees on every invoice-level field: one invoice per line item, not a duplicate
    assert result.errors == ()


def test_quickbooks_mapping_us_dates_and_thousands():
    result = read_invoices("quickbooks", QUICKBOOKS, default_currency="EUR")
    inv = {i.number: i for i in result.records}
    assert set(inv) == {"1042", "1043", "1044"}
    assert inv["1042"].issue_date == dt.date(2026, 10, 13) and inv["1042"].due_date == dt.date(2026, 10, 27)
    assert inv["1042"].amount == D("3600.00") and inv["1042"].balance == D("3600.00") and inv["1042"].status == "open"
    assert inv["1043"].status == "paid" and inv["1044"].status == "void"
    assert inv["1042"].currency == "EUR"
    assert [e.message for e in result.skipped] == ["transaction type Payment is not an invoice"]
    names = {c.id: c.name for c in result.customers}
    assert names[customer_key("Birchfield Example Consulting GmbH")] == "Birchfield Example Consulting GmbH"
    assert inv["1042"].customer_id == customer_key("Birchfield Example Consulting GmbH")
    assert result.credit_notes == () and result.errors == ()


def test_stripe_mapping():
    result = read_invoices("stripe", STRIPE)
    inv = {i.number: i for i in result.records}
    assert set(inv) == {"ABCD1234-0001", "ABCD1234-0002", "ABCD1234-0003"}
    assert inv["ABCD1234-0001"].currency == "EUR" and inv["ABCD1234-0001"].customer_id == "cus_Fake001"
    assert inv["ABCD1234-0001"].issue_date == dt.date(2026, 10, 20) and inv["ABCD1234-0001"].due_date == dt.date(2026, 11, 3)
    assert inv["ABCD1234-0002"].status == "paid" and inv["ABCD1234-0002"].paid_date == dt.date(2026, 10, 26)
    assert inv["ABCD1234-0003"].status == "void"
    assert [e.message for e in result.skipped] == ["status draft is not an issued invoice"]
    customers = {c.id: c for c in result.customers}
    assert customers["cus_Fake002"].name == "Modrá Dílna Demo s.r.o." and customers["cus_Fake002"].email == "petr@modra.example"


def test_malformed_rows_are_reported_not_raised():
    text = GENERIC + "INV-BAD-1,C001,2026-10-13,2026-10-27,abc,EUR,1,open,,\n" \
        + ",C001,2026-10-13,2026-10-27,10,EUR,10,open,,\n" \
        + "INV-BAD-3,C001,13/13/2026,2026-10-27,10,EUR,10,open,,\n" \
        + "INV-BAD-4,C001,2026-10-13,2026-10-27,10,EUR,10,escalated,,\n" \
        + "INV-OK-5,C001,2026-10-13,2026-10-27,10.5,,10.5,,,\n"
    result = read_invoices("generic", text, default_currency="CZK")
    numbers = [i.number for i in result.records]
    assert numbers == ["INV-2026-0042", "INV-2026-0036", "INV-2026-0038", "INV-OK-5"]
    assert [e.row for e in result.errors] == [5, 6, 7, 8]
    assert "amount" in result.errors[0].message and "abc" in result.errors[0].raw
    assert "number" in result.errors[1].message
    assert "date" in result.errors[2].message
    assert "status" in result.errors[3].message
    ok5 = result.records[-1]
    assert ok5.currency == "CZK" and ok5.status == "open" and ok5.amount == D("10.50")
    assert not result.ok


def test_missing_required_column_is_a_single_error():
    result = read_invoices("generic", "invoice_no,customer_id\nX,Y\n")
    assert result.records == () and len(result.errors) == 1 and "column" in result.errors[0].message


def test_fingerprint_ignores_line_endings_bom_and_trailing_space():
    a = fingerprint(GENERIC)
    assert a == fingerprint("﻿" + GENERIC.replace("\n", "\r\n"))
    assert a == fingerprint(GENERIC.replace("\n", "   \n"))
    assert a != fingerprint(GENERIC.replace("1250.00", "1520.00"))
    assert len(a) == 64 and read_invoices("generic", GENERIC).fingerprint == a


def test_reads_from_a_path(tmp_path: Path):
    p = tmp_path / "invoices.csv"
    p.write_text(GENERIC, encoding="utf-8")
    assert len(read_invoices("generic", p).records) == 3
    assert len(read_invoices("generic", str(p)).records) == 3
    assert read_invoices("generic", "does-not-exist.csv").errors[0].message.startswith("no such file")


def test_bank_csv_generic():
    result = read_bank_csv(BANK)
    assert result.kind == "bank" and result.errors == ()
    pay = {p.id: p for p in result.records}
    assert pay["B1-01"].amount == D("1980.00") and pay["B1-01"].currency == "EUR"
    assert pay["B1-01"].date == dt.date(2026, 11, 6) and pay["B1-01"].reference == "INV-2026-0045"
    assert pay["B1-01"].counterparty == "FERNHILL TEST MEDIA LTD"
    assert pay["B1-03"].amount == D("-12.50") and pay["B1-03"].memo == "account fee" and pay["B1-03"].is_outgoing


def test_bank_csv_czech_export_with_semicolons_bom_and_decimal_commas():
    result = read_bank_csv(CZECH_BANK)
    assert result.errors == (), result.errors
    first, second = result.records
    assert first.amount == D("30250.00") and first.currency == "CZK" and first.date == dt.date(2026, 11, 6)
    assert first.reference == "20260043" and first.counterparty == "LÍPA VZOROVÁ LOGISTIKA" and first.memo == "faktura"
    assert second.amount == D("-1000.00")
    assert first.id and second.id and first.id != second.id
    assert read_bank_csv(CZECH_BANK).records[0].id == first.id


def test_bank_csv_bad_rows_and_duplicate_ids():
    text = BANK + "B1-01,2026-11-06,1980.00,EUR,FERNHILL TEST MEDIA LTD,INV-2026-0045,\nB1-09,not-a-date,10,EUR,X,,\nB1-10,2026-11-06,ten,EUR,X,,\n"
    result = read_bank_csv(text)
    assert [p.id for p in result.records] == ["B1-01", "B1-02", "B1-03"]
    assert [e.row for e in result.errors] == [5, 6, 7]
    assert "duplicate" in result.errors[0].message


def test_bank_csv_us_dates_with_date_order_mdy():
    text = "id,date,amount,currency,counterparty,reference\nU1,11/06/2026,1250.00,USD,ACME,INV-1\nU2,11/13/2026,10.00,USD,ACME,x\n"
    result = read_bank_csv(text, date_order="mdy")
    assert result.ok and [p.date for p in result.records] == [dt.date(2026, 11, 6), dt.date(2026, 11, 13)]
    inferred = read_bank_csv(text)
    assert inferred.ok and inferred.date_order == "mdy", "11/13/2026 proves the file is month-first"
    assert [p.date for p in inferred.records] == [dt.date(2026, 11, 6), dt.date(2026, 11, 13)]
    assert [p.date for p in read_bank_csv(text, date_order="auto").records] == [dt.date(2026, 11, 6), dt.date(2026, 11, 13)]
    told = read_bank_csv(text, date_order="dmy")
    assert told.records[0].date == dt.date(2026, 6, 11) and len(told.errors) == 1, "an explicit order is obeyed"
    with pytest.raises(ValueError):
        read_bank_csv(text, date_order="ymd")


def test_bank_csv_dates_with_a_time_part():
    text = "Datum;Částka;Měna;Protiúčet název\n06.11.2026 14:32:11;1 250,00;CZK;X\n6. 11. 2026 09:00;10,00;CZK;Y\n"
    result = read_bank_csv(text)
    assert result.ok and [p.date for p in result.records] == [dt.date(2026, 11, 6), dt.date(2026, 11, 6)]


def test_customers_csv():
    result = read_customers(CUSTOMERS)
    assert result.kind == "customers" and result.errors == ()
    c = {x.id: x for x in result.records}
    assert c["C002"].language == "cs" and c["C002"].contact_name == "Petr Ukázka" and not c["C002"].on_hold
    assert c["C006"].on_hold and c["C006"].hold_reason == "dispute on INV-2026-0033"
    german = read_customers(CUSTOMERS + "C009,Kunde Beispiel GmbH,de,info@kunde.example,Herr Muster,,\n")
    assert german.ok and {x.id: x for x in german.records}["C009"].language == "en"
    assert [(w.row, w.code) for w in german.warnings] == [(5, "language")] and "'de'" in german.warnings[0].message
