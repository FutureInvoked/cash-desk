"""importers.py against the exports people really download: Xero line-item
exports, Stripe's invoice export, QuickBooks Online reports with title and
total rows, and bank statements with preambles, debit/credit columns,
direction indicators and status columns. All data is fake (.example e-mails,
public example IBANs)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from cashdesk.importers import CreditNote, customer_key, read_bank_csv, read_customers, read_invoices

D = Decimal

# ------------------------------------------------------------------ Xero

XERO_HEADER = ("*ContactName,EmailAddress,POAddressLine1,POCity,POPostalCode,POCountry,*InvoiceNumber,Reference,*InvoiceDate,*DueDate,"
               "Total,TaxTotal,InvoiceAmountPaid,InvoiceAmountDue,InventoryItemCode,*Description,*Quantity,*UnitAmount,Discount,LineAmount,"
               "*AccountCode,*TaxType,TaxAmount,TrackingName1,TrackingOption1,Currency,BrandingTheme,Type,Sent,Status\n")


def xero_row(contact, number, issue, due, total, paid, due_amt, desc, line, *, status="AUTHORISED", type_="ACCREC", ref="", cur="GBP"):
    email = contact.split()[0].lower() + "@accounts.example"
    return (f"{contact},{email},1 Example Street,London,EC1A 1AA,United Kingdom,{number},{ref},{issue},{due},"
            f"{total},0.00,{paid},{due_amt},,{desc},1,{line},,{line},200,No VAT,0.00,,,{cur},Standard,{type_},Sent,{status}\n")


XERO_EXPORT = XERO_HEADER + "".join([
    xero_row("Harbour Example Ltd", "INV-0041", "15/10/2026", "29/10/2026", "1200.00", "0.00", "1200.00", "Design work", "1000.00", ref="PO-7781"),
    xero_row("Harbour Example Ltd", "INV-0041", "15/10/2026", "29/10/2026", "1200.00", "0.00", "1200.00", "Hosting", "200.00", ref="PO-7781"),
    xero_row("Quarry Sample Ltd", "INV-0043", "24/09/2026", "08/10/2026", "1000.00", "400.00", "600.00", "Consulting", "1000.00"),
    xero_row("Quarry Sample Ltd", "INV-0044", "01/09/2026", "15/09/2026", "250.00", "250.00", "0.00", "Support", "250.00", status="PAID"),
    xero_row("Quarry Sample Ltd", "INV-0045", "02/09/2026", "16/09/2026", "90.00", "0.00", "0.00", "Support", "90.00", status="VOIDED"),
    xero_row("Harbour Example Ltd", "INV-0046", "03/11/2026", "17/11/2026", "75.00", "0.00", "75.00", "Draft", "75.00", status="DRAFT"),
    xero_row("Harbour Example Ltd", "INV-0047", "03/11/2026", "17/11/2026", "75.00", "0.00", "75.00", "Mistake", "75.00", status="DELETED"),
    xero_row("Harbour Example Ltd", "CN-0007", "20/10/2026", "", "300.00", "0.00", "300.00", "Refund of hosting", "300.00", type_="ACCRECCREDIT"),
    xero_row("Quarry Sample Ltd", "CN-0008", "21/10/2026", "", "50.00", "50.00", "0.00", "Applied credit", "50.00", type_="ACCRECCREDIT", status="PAID"),
])


def test_xero_line_item_export_imports_without_allow_errors():
    result = read_invoices("xero", XERO_EXPORT)
    assert result.ok, result.errors
    inv = {i.number: i for i in result.records}
    assert set(inv) == {"INV-0041", "INV-0043", "INV-0044", "INV-0045"}, "two line items of INV-0041 are one invoice"
    assert inv["INV-0041"].amount == D("1200.00") and inv["INV-0041"].balance == D("1200.00") and inv["INV-0041"].reference == "PO-7781"
    assert inv["INV-0043"].amount == D("1000.00") and inv["INV-0043"].balance == D("600.00"), "balance is InvoiceAmountDue"
    assert inv["INV-0043"].issue_date == dt.date(2026, 9, 24) and inv["INV-0043"].due_date == dt.date(2026, 10, 8)
    assert inv["INV-0044"].status == "paid" and not inv["INV-0044"].is_open
    assert inv["INV-0045"].status == "void" and not inv["INV-0045"].is_open
    assert result.skipped_counts == {"draft": 1, "deleted": 1, "settled_credit": 1}
    assert result.date_order == "dmy" and result.rows == 9
    (cn,) = result.credit_notes
    assert isinstance(cn, CreditNote) and cn.kind == "credit_note" and cn.number == "CN-0007"
    assert cn.amount == D("300.00") and cn.currency == "GBP" and cn.customer_id == customer_key("Harbour Example Ltd")
    assert cn.date == dt.date(2026, 10, 20) and "CN-0007" not in inv and cn.detail.startswith("credit note for GBP 300.00")


def test_xero_balance_is_total_minus_amount_paid_without_an_amount_due_column():
    text = ("ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountPaid,Currency,Type,Status\n"
            "Quarry Sample Ltd,ap@quarry.example,INV-0050,01/10/2026,15/10/2026,1000.00,350.00,EUR,ACCREC,AUTHORISED\n")
    (inv,) = read_invoices("xero", text).records
    assert inv.amount == D("1000.00") and inv.balance == D("650.00")


def test_xero_line_items_that_disagree_refuse_that_invoice():
    text = XERO_HEADER + xero_row("Harbour Example Ltd", "INV-0041", "15/10/2026", "29/10/2026", "1200.00", "0.00", "1200.00", "Design", "1000.00") \
        + xero_row("Harbour Example Ltd", "INV-0041", "15/10/2026", "29/10/2026", "1250.00", "0.00", "1250.00", "Hosting", "250.00") \
        + xero_row("Quarry Sample Ltd", "INV-0043", "24/09/2026", "08/10/2026", "1000.00", "400.00", "600.00", "Consulting", "1000.00")
    result = read_invoices("xero", text)
    assert [i.number for i in result.records] == ["INV-0043"]
    (error,) = result.errors
    assert error.row == 2 and "INV-0041" in error.message and "disagree" in error.message and "total" in error.message


def test_xero_sales_credit_note_wording_and_bills():
    text = ("ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountPaid,InvoiceAmountDue,Currency,Type,Status\n"
            "Quarry Sample Ltd,ap@quarry.example,INV-0050,01/10/2026,15/10/2026,900.00,0.00,900.00,GBP,Sales invoice,AUTHORISED\n"
            "Quarry Sample Ltd,ap@quarry.example,CN-0009,02/10/2026,16/10/2026,300.00,0.00,300.00,GBP,Sales credit note,AUTHORISED\n"
            "Paper Supplier Ltd,bills@paper.example,BILL-1,03/10/2026,17/10/2026,80.00,0.00,80.00,GBP,ACCPAY,AUTHORISED\n")
    result = read_invoices("xero", text)
    assert result.ok and [i.number for i in result.records] == ["INV-0050"]
    assert [c.number for c in result.credit_notes] == ["CN-0009"]
    assert result.skipped_counts == {"bill": 1}


def test_xero_us_organisation_dates_are_read_month_first():
    text = ("ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Status\n"
            "Harbour Example Ltd,ap@harbour.example,INV-0060,10/27/2026,11/10/2026,500.00,500.00,USD,AUTHORISED\n"
            "Quarry Sample Ltd,ap@quarry.example,INV-0061,11/01/2026,12/01/2026,700.00,700.00,USD,AUTHORISED\n")
    result = read_invoices("xero", text)
    assert result.ok and result.date_order == "mdy"
    inv = {i.number: i for i in result.records}
    assert inv["INV-0060"].due_date == dt.date(2026, 11, 10) and inv["INV-0061"].issue_date == dt.date(2026, 11, 1)


# ---------------------------------------------------------------- Stripe

STRIPE_EXPORT = (
    "id,Number,Customer,Customer Name,Customer Email,Amount Due,Amount Paid,Amount Remaining,Subtotal,Total,Currency,"
    "Date (UTC),Due Date (UTC),Finalized At (UTC),Paid At (UTC),Status,Paid,Billing,Description\n"
    "in_1PfakeA001,7F3A21C0-0001,cus_Pfake001,Harbour Example Ltd,ap@harbour.example,500.00,0.00,500.00,500.00,500.00,eur,"
    "2026-10-01 09:00:00,2026-10-29 09:00:00,2026-10-01 09:05:00,,open,false,send_invoice,Retainer\n"
    "in_1PfakeA002,7F3A21C0-0002,cus_Pfake002,Quarry Sample Ltd,ap@quarry.example,99.00,99.00,0.00,99.00,99.00,eur,"
    "2026-10-03 09:00:00,,2026-10-03 09:05:00,2026-10-03 09:06:00,paid,true,charge_automatically,Subscription\n"
    "in_1PfakeA003,7F3A21C0-0003,cus_Pfake003,Lantern Test GmbH,ap@lantern.example,1000.00,400.00,600.00,1000.00,1000.00,eur,"
    "2026-09-20 09:00:00,2026-10-08 09:00:00,2026-09-20 09:05:00,,open,false,send_invoice,Project\n"
    "in_1PfakeA004,7F3A21C0-0004,cus_Pfake002,Quarry Sample Ltd,ap@quarry.example,99.00,0.00,99.00,99.00,99.00,eur,"
    "2026-11-03 09:00:00,,2026-11-03 09:05:00,,open,false,charge_automatically,Subscription retry\n"
    "in_1PfakeA005,7F3A21C0-0005,cus_Pfake003,Lantern Test GmbH,ap@lantern.example,40.00,0.00,40.00,40.00,40.00,eur,"
    "2026-08-01 09:00:00,2026-08-15 09:00:00,2026-08-01 09:05:00,,uncollectible,false,send_invoice,Old\n"
    "in_1PfakeA006,7F3A21C0-0006,cus_Pfake003,Lantern Test GmbH,ap@lantern.example,15.00,0.00,15.00,15.00,15.00,eur,"
    "2026-08-02 09:00:00,2026-08-16 09:00:00,2026-08-02 09:05:00,,void,false,send_invoice,Cancelled\n"
    "in_1PfakeA007,,cus_Pfake001,Harbour Example Ltd,ap@harbour.example,20.00,0.00,20.00,20.00,20.00,eur,"
    "2026-11-04 09:00:00,2026-12-04 09:00:00,,,draft,false,send_invoice,Next month\n"
)


def test_stripe_export_reads_amount_remaining_and_skips_what_cannot_be_chased():
    result = read_invoices("stripe", STRIPE_EXPORT)
    assert result.ok, result.errors
    inv = {i.number: i for i in result.records}
    assert set(inv) == {"7F3A21C0-0001", "7F3A21C0-0003", "7F3A21C0-0005", "7F3A21C0-0006"}
    assert inv["7F3A21C0-0003"].amount == D("1000.00") and inv["7F3A21C0-0003"].balance == D("600.00"), "Amount Remaining, not Amount Due"
    assert inv["7F3A21C0-0001"].currency == "EUR" and inv["7F3A21C0-0001"].customer_id == "cus_Pfake001"
    assert inv["7F3A21C0-0005"].status == "void" and inv["7F3A21C0-0006"].status == "void"
    assert result.skipped_counts == {"no_due_date": 2, "draft": 1}
    assert all("charged automatically" in e.message for e in result.skipped if e.code == "no_due_date")


# ------------------------------------------------------------ QuickBooks

QBO_AGING = """Example Studio LLC
A/R Aging Detail
As of November 5, 2026

,Date,Transaction Type,Num,Customer full name,Due Date,Past Due,Amount,Open Balance
,Current,,,,,,,
,10/30/2026,Invoice,1051,Harbour Example Ltd,11/29/2026,-24,"1,500.00","1,500.00"
,Total for Current,,,,,,"1,500.00","1,500.00"
,1 - 30 days past due,,,,,,,
,10/08/2026,Invoice,1047,Lantern Test GmbH,10/22/2026,14,"2,400.00","2,400.00"
,10/02/2026,Credit Memo,1049,Lantern Test GmbH,10/02/2026,34,-400.00,-400.00
,10/20/2026,Payment,,Lantern Test GmbH,10/20/2026,16,"-1,000.00","-1,000.00"
,10/21/2026,Journal Entry,JE-12,Harbour Example Ltd,10/21/2026,15,25.00,25.00
,Total for 1 - 30 days past due,,,,,,"1,025.00","1,025.00"
,TOTAL,,,,,,"2,525.00","2,525.00"



"Thursday, Nov 05, 2026 09:41:12 AM GMT-08:00"
"""


def test_quickbooks_aging_report_with_titles_groups_and_totals():
    result = read_invoices("quickbooks", QBO_AGING, default_currency="USD")
    assert result.ok, result.errors
    inv = {i.number: i for i in result.records}
    assert set(inv) == {"1051", "1047"}
    assert inv["1047"].amount == D("2400.00") and inv["1047"].due_date == dt.date(2026, 10, 22) and inv["1047"].currency == "USD"
    assert inv["1047"].customer_id == customer_key("Lantern Test GmbH")
    kinds = {(c.kind, c.number, c.amount, c.row) for c in result.credit_notes}
    assert kinds == {("credit_note", "1049", D("400.00"), 11), ("unapplied_payment", "", D("1000.00"), 12)}, "never dropped"
    assert [(e.row, e.code) for e in result.skipped] == [(13, "not_invoice")] and "not chased" in result.skipped[0].message
    assert result.rows == 5, "group, subtotal, TOTAL and footer lines are not rows"


def test_quickbooks_invoice_list_without_a_type_column():
    text = ("Example Studio LLC\nInvoice List\nAll Dates\n\n"
            "Date,Num,Customer,Due date,Amount,Open balance,Status\n"
            "10/13/2026,1042,Birchfield Example LLC,10/27/2026,\"3,600.00\",\"3,600.00\",Overdue\n"
            "10/20/2026,1043,Fernhill Example Inc,11/19/2026,980.00,0.00,Paid\n"
            "TOTAL,,,,\"4,580.00\",\"3,600.00\",\n")
    result = read_invoices("quickbooks", text)
    assert result.ok and [(i.number, i.balance, i.status) for i in result.records] == [("1042", D("3600.00"), "open"), ("1043", D("0.00"), "paid")]
    assert result.errors == () and result.skipped == ()


# --------------------------------------------------------------- generic

def test_generic_credit_notes_drafts_and_missing_due_dates():
    text = ("invoice_no,customer_id,type,issue_date,due_date,amount,currency,balance,status\n"
            "FV-2026-101,C1,faktura,2026-10-01,2026-10-15,1210.00,CZK,1210.00,open\n"
            "DB-2026-007,C1,dobropis,2026-10-05,2026-10-19,242.00,CZK,242.00,open\n"
            "FV-2026-102,C2,,2026-10-02,2026-10-16,-50.00,CZK,-50.00,open\n"
            "FV-2026-103,C2,,2026-10-03,,500.00,CZK,0.00,paid\n"
            "FV-2026-104,C2,,2026-11-01,2026-11-15,300.00,CZK,300.00,draft\n")
    result = read_invoices("generic", text)
    assert result.ok and [i.number for i in result.records] == ["FV-2026-101"]
    assert [(c.number, c.amount) for c in result.credit_notes] == [("DB-2026-007", D("242.00")), ("FV-2026-102", D("50.00"))]
    assert result.skipped_counts == {"no_due_date": 1, "draft": 1}


def test_open_invoice_without_due_date_and_due_before_issue_are_row_errors():
    text = ("invoice_no,customer_id,issue_date,due_date,amount\n"
            "A1,C1,2026-10-01,,100.00\n"
            "A2,C1,2026-10-15,2026-10-01,100.00\n"
            "A3,C1,2026-10-01,2026-10-01,100.00\n")
    result = read_invoices("generic", text)
    assert [i.number for i in result.records] == ["A3"]
    assert [e.row for e in result.errors] == [2, 3]
    assert "due date is missing" in result.errors[0].message
    assert "2026-10-01 is before issue date 2026-10-15" in result.errors[1].message


def test_ambiguous_slashed_dates_refuse_the_file_unless_an_order_is_given():
    text = "invoice_no,customer_id,issue_date,due_date,amount\nA1,C1,10/01/2026,11/05/2026,100.00\nA2,C1,03/02/2026,04/03/2026,50.00\n"
    refused = read_invoices("generic", text)
    assert refused.records == () and len(refused.errors) == 1 and "date_order" in refused.errors[0].message
    assert read_invoices("generic", text, date_order="auto").records == ()
    us = read_invoices("generic", text, date_order="mdy")
    assert us.ok and us.records[0].issue_date == dt.date(2026, 10, 1) and us.records[0].due_date == dt.date(2026, 11, 5)
    eu = read_invoices("generic", text, date_order="dmy")
    assert eu.ok and eu.records[0].issue_date == dt.date(2026, 1, 10) and eu.records[0].due_date == dt.date(2026, 5, 11)
    mixed = read_invoices("generic", text + "A3,C1,25/10/2026,10/30/2026,1.00\n")
    assert mixed.records == () and "mix" in mixed.errors[0].message
    with pytest.raises(ValueError):
        read_invoices("generic", text, date_order="ymd")


def test_invoice_amounts_with_three_decimals():
    text = "invoice_no,customer_id,issue_date,due_date,amount\nA1,C1,2026-10-01,2026-10-15,1250.500\nA2,C1,2026-10-01,2026-10-15,1250.505\n"
    result = read_invoices("generic", text)
    assert [(i.number, i.amount) for i in result.records] == [("A1", D("1250.50"))], "1250.500 is not 1,250,500.00"
    assert [e.row for e in result.errors] == [3] and "1250.505" in result.errors[0].message


# ---------------------------------------------------------- customer ids

def test_customer_ids_from_names_are_unicode_safe_and_never_merge_names():
    text = ("ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Status,Language\n"
            "北京测试有限公司,cn@beijing.example,INV-1,2026-10-01,2026-10-15,100.00,100.00,EUR,AUTHORISED,\n"
            "Ελληνική Δοκιμή Α.Ε.,gr@athens.example,INV-2,2026-10-01,2026-10-15,200.00,200.00,EUR,AUTHORISED,\n"
            "Müller GmbH,info@mueller.example,INV-3,2026-10-01,2026-10-15,300.00,300.00,EUR,AUTHORISED,de\n"
            "Muller GmbH,info@muller.example,INV-4,2026-10-01,2026-10-15,400.00,400.00,EUR,AUTHORISED,\n"
            "MULLER GMBH,info@muller.example,INV-5,2026-10-01,2026-10-15,10.00,10.00,EUR,AUTHORISED,\n")
    result = read_invoices("xero", text)
    assert result.ok, result.errors
    ids = [i.customer_id for i in result.records]
    assert len(set(ids[:4])) == 4, "four different names, four customers"
    assert ids[4] == ids[3], "the same name in capitals is the same customer"
    assert all(ids) and ids[0].startswith("customer-") and ids[2].startswith("muller-gmbh-") and ids[3].startswith("muller-gmbh-")
    assert customer_key("Müller GmbH") == ids[2] and customer_key("Müller  GmbH ") == ids[2], "stable across imports"
    names = {c.id: c for c in result.customers}
    assert names[ids[0]].name == "北京测试有限公司" and names[ids[2]].language == "en"
    assert [(w.row, w.code) for w in result.warnings] == [(4, "language")]


def test_explicit_customer_ids_are_kept():
    text = "invoice_no,customer_id,issue_date,due_date,amount\nA1,Müller GmbH,2026-10-01,2026-10-15,100.00\n"
    assert read_invoices("generic", text).records[0].customer_id == "Müller GmbH"
    assert read_invoices("stripe", STRIPE_EXPORT).records[0].customer_id == "cus_Pfake001"


def test_customers_file_with_a_preamble_and_an_unsupported_language():
    text = "Customer export 2026-11-05\n\ncustomer_id,name,language,email,contact_name\nk1,Kunde Beispiel GmbH,DE,info@kunde.example,Herr Muster\nk2,Quarry Sample Ltd,EN,ap@quarry.example,Ann\n"
    result = read_customers(text)
    assert result.ok and [c.language for c in result.records] == ["en", "en"]
    assert [(w.row, w.code) for w in result.warnings] == [(4, "language")]


# ------------------------------------------------------------------ banks

INVOICE = "invoice_no,customer_id,issue_date,due_date,amount\nINV-2026-0042,acme,2026-10-01,2026-10-22,1200.00\n"


def test_bank_statement_with_account_preamble():
    text = ("﻿Account;DE89 3704 0044 0532 0130 00\nPeriod;01.11.2026 - 05.11.2026\nOpening balance;4.200,00\n\n"
            "Booking date;Value date;Counterparty name;Payment reference;Amount;Currency\n"
            "03.11.2026;03.11.2026;HARBOUR EXAMPLE LTD;INV-2026-0042;1.200,00;EUR\n"
            "04.11.2026;04.11.2026;Example Bank;Account fee;12,50-;EUR\n")
    result = read_bank_csv(text)
    assert result.ok, result.errors
    assert [(p.date, p.amount, p.reference) for p in result.records] == [
        (dt.date(2026, 11, 3), D("1200.00"), "INV-2026-0042"), (dt.date(2026, 11, 4), D("-12.50"), "Account fee")]
    assert read_bank_csv(text.replace("1.200,00", "abc")).errors[0].row == 6, "rows keep their physical line numbers"


def test_bank_separate_debit_and_credit_columns():
    lloyds = ("Transaction Date,Transaction Type,Sort Code,Account Number,Transaction Description,Debit Amount,Credit Amount,Balance\n"
              "03/11/2026,FPI,'12-34-56,12345678,HARBOUR EXAMPLE LTD INV-2026-0042,,1200.00,5400.00\n"
              "28/10/2026,DD,'12-34-56,12345678,EXAMPLE ENERGY,85.20,,4200.00\n")
    result = read_bank_csv(lloyds, default_currency="GBP")
    assert result.ok and [(p.amount, p.currency) for p in result.records] == [(D("1200.00"), "GBP"), (D("-85.20"), "GBP")]
    nationwide = ("\"Account Name:\",\"FlexAccount ****1234\"\n\"Account Balance:\",\"£5,400.00\"\n\n"
                  "\"Date\",\"Transaction type\",\"Description\",\"Paid out\",\"Paid in\",\"Balance\"\n"
                  "\"03 Nov 2026\",\"Bank credit\",\"HARBOUR EXAMPLE LTD\",\"\",\"£1,200.00\",\"£5,400.00\"\n"
                  "\"04 Nov 2026\",\"Payment to\",\"EXAMPLE ENERGY\",\"£85.20\",\"\",\"£5,314.80\"\n")
    result = read_bank_csv(nationwide)
    assert result.ok and [(p.amount, p.currency) for p in result.records] == [(D("1200.00"), "GBP"), (D("-85.20"), "GBP")]
    both = read_bank_csv("Date,Description,Money out,Money in\n2026-11-03,X,5.00,6.00\n")
    assert len(both.errors) == 1 and "both" in both.errors[0].message


@pytest.mark.parametrize(
    "header, credit, debit",
    [
        ("CdtDbtInd", "CRDT", "DBIT"),
        ("DR/CR", "CR", "DR"),
        ("Debit/Credit", "Credit", "Debit"),
    ],
)
def test_bank_direction_indicator_signs_unsigned_amounts(header, credit, debit):
    text = (f"Booking date,Amount,Currency,Counterparty,Reference,{header}\n"
            f"2026-11-03,1200.00,EUR,HARBOUR EXAMPLE LTD,INV-2026-0042,{credit}\n"
            f"2026-11-04,1200.00,EUR,HARBOUR EXAMPLE LTD,Return INV-2026-0042,{debit}\n")
    result = read_bank_csv(text)
    assert result.ok and [p.amount for p in result.records] == [D("1200.00"), D("-1200.00")], "only the credit is money received"


def test_bank_german_soll_haben_export():
    text = ("Buchungstag;Valuta;Auftraggeber/Zahlungsempfänger;Empfänger/Zahlungspflichtiger;IBAN;BIC;Vorgang/Verwendungszweck;Währung;Umsatz;Soll/Haben\n"
            "03.11.2026;03.11.2026;Harbour Example Ltd;Example Studio GmbH;GB29NWBK60161331926819;NWBKGB2L;INV-2026-0042;EUR;1.200,00;H\n"
            "04.11.2026;04.11.2026;Example Studio GmbH;Strom Beispiel AG;DE89370400440532013000;COBADEFFXXX;Abschlag November;EUR;85,20;S\n")
    result = read_bank_csv(text)
    assert result.ok, result.errors
    assert [p.amount for p in result.records] == [D("1200.00"), D("-85.20")]


def test_bank_dutch_af_bij_export_with_currency_in_the_header():
    text = ('"Datum";"Naam / Omschrijving";"Rekening";"Tegenrekening";"Code";"Af Bij";"Bedrag (EUR)";"Mutatiesoort";"Mededelingen"\n'
            '"20261103";"Harbour Example Ltd";"NL91ABNA0417164300";"GB29NWBK60161331926819";"OV";"Bij";"1200,00";"Overschrijving";"INV-2026-0042"\n'
            '"20261104";"Energie Voorbeeld BV";"NL91ABNA0417164300";"NL02ABNA0123456789";"IC";"Af";"85,20";"Incasso";"Termijn november"\n')
    result = read_bank_csv(text, default_currency="CZK")
    assert result.ok, result.errors
    assert [(p.date, p.amount, p.currency, p.counterparty) for p in result.records] == [
        (dt.date(2026, 11, 3), D("1200.00"), "EUR", "Harbour Example Ltd"), (dt.date(2026, 11, 4), D("-85.20"), "EUR", "Energie Voorbeeld BV")]


def test_bank_status_column_counts_only_completed_lines():
    revolut = ("Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
               "TRANSFER,Current,2026-11-03 10:12:01,2026-11-03 10:12:05,Payment from Harbour Example Ltd,1200.00,0.00,EUR,COMPLETED,5400.00\n"
               "TRANSFER,Current,2026-11-04 08:00:00,,Payment from Quarry Sample Ltd,600.00,0.00,EUR,PENDING,\n"
               "TRANSFER,Current,2026-11-04 09:00:00,2026-11-04 09:30:00,Payment from Lantern Test GmbH,99.00,0.00,EUR,REVERTED,5400.00\n")
    result = read_bank_csv(revolut)
    assert result.ok and [p.amount for p in result.records] == [D("1200.00")]
    assert result.skipped_counts == {"not_booked": 2} and [e.row for e in result.skipped] == [3, 4]


@pytest.mark.parametrize(
    "text, column",
    [
        ("Date,Amount,Status\n2026-11-03,1200.00,Completed\n2026-11-04,50.00,In review\n", "status"),
        ("Date,Amount,CdtDbtInd\n2026-11-03,1200.00,CRDT\n2026-11-04,50.00,RVSL\n", "cdtdbtind"),
        ("Date,Amount,CdtDbtInd\n2026-11-03,1200.00,\n", "cdtdbtind"),
    ],
)
def test_bank_direction_or_status_values_not_understood_refuse_the_file(text, column):
    result = read_bank_csv(text)
    assert result.records == () and len(result.errors) == 1
    assert repr(column) in result.errors[0].message and "refused" in result.errors[0].message


def test_bank_credit_marked_line_with_a_negative_amount_is_an_error():
    result = read_bank_csv("Date,Amount,DR/CR\n2026-11-03,-5.00,CR\n")
    assert len(result.errors) == 1 and "negative" in result.errors[0].message


def test_bank_dates_decided_from_the_whole_file():
    us = "Date,Description,Amount,Running Bal.\n11/03/2026,HARBOUR EXAMPLE LTD INV-2026-0042,1200.00,5400.00\n10/28/2026,EXAMPLE ENERGY,-85.20,4200.00\n"
    result = read_bank_csv(us)
    assert result.ok and result.date_order == "mdy" and result.records[0].date == dt.date(2026, 11, 3)
    wise = "TransferWise ID,Date,Amount,Currency,Description,Payment Reference,Payer Name\nTRANSFER-1,03-11-2026,1200.00,EUR,Received money,INV-2026-0042,HARBOUR EXAMPLE LTD\n"
    assert read_bank_csv(wise).records == () and "date_order" in read_bank_csv(wise).errors[0].message
    told = read_bank_csv(wise, date_order="dmy")
    assert told.ok and told.records[0].date == dt.date(2026, 11, 3) and told.records[0].id == "TRANSFER-1"
