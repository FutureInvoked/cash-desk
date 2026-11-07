"""Xero AmountCredited is part of what has been settled (P2ah)."""
from decimal import Decimal as D
from cashdesk.importers import read_invoices

HEAD = ("ContactName,EmailAddress,InvoiceNumber,Reference,InvoiceDate,DueDate,Total,TaxTotal,"
        "InvoiceAmountPaid,InvoiceAmountDue,AmountCredited,Currency,Type,Status,Description,LineAmount\n")


def xero(*rows):
    return read_invoices('xero', HEAD + ''.join(rows), date_order='dmy')


def test_partly_credited_invoice_imports_with_its_real_amount_due():
    r = xero("Oakridge Partners LLP,lee@oak.example,INV-0043,,14/10/2026,28/10/2026,600.00,100.00,200.00,300.00,100.00,GBP,Sales invoice,Awaiting Payment,Consulting,500.00\n")
    assert r.ok, r.errors
    assert r.records[0].balance == D('300.00')


def test_judge_layout_with_line_items_and_credit_imports_both_invoices():
    r = xero("Bluebell Design Studio Ltd,mia@bluebell.example,INV-0042,PO 77,13/10/2026,27/10/2026,1500.00,250.00,0.00,1500.00,0.00,GBP,Sales invoice,Awaiting Payment,Design work,1000.00\n",
             "Bluebell Design Studio Ltd,mia@bluebell.example,INV-0042,PO 77,13/10/2026,27/10/2026,1500.00,250.00,0.00,1500.00,0.00,GBP,Sales invoice,Awaiting Payment,Hosting,250.00\n",
             "Oakridge Partners LLP,lee@oak.example,INV-0043,,14/10/2026,28/10/2026,600.00,100.00,200.00,300.00,100.00,GBP,Sales invoice,Awaiting Payment,Consulting,500.00\n")
    assert r.ok, r.errors
    assert {i.number: i.balance for i in r.records} == {'INV-0042': D('1500.00'), 'INV-0043': D('300.00')}


def test_fully_credited_invoice_is_settled():
    r = xero("Oakridge Partners LLP,lee@oak.example,INV-0044,,14/10/2026,28/10/2026,600.00,100.00,0.00,0.00,600.00,GBP,Sales invoice,Paid,Consulting,500.00\n")
    assert r.ok, r.errors
    closed = list(r.records) + list(r.invoice_closures)
    assert closed and closed[0].status == 'paid'


def test_credited_figures_that_still_disagree_are_refused():
    r = xero("Oakridge Partners LLP,lee@oak.example,INV-0043,,14/10/2026,28/10/2026,600.00,100.00,200.00,400.00,100.00,GBP,Sales invoice,Awaiting Payment,Consulting,500.00\n")
    assert not r.ok and 'does not equal remaining' in r.errors[0].message


def test_negative_amount_credited_is_refused():
    r = xero("Oakridge Partners LLP,lee@oak.example,INV-0043,,14/10/2026,28/10/2026,600.00,100.00,200.00,500.00,-100.00,GBP,Sales invoice,Awaiting Payment,Consulting,500.00\n")
    assert not r.ok and 'amount credited is negative' in r.errors[0].message


def test_credited_without_amount_due_derives_the_balance():
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency,amount paid,amount credited\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,600.00,EUR,200.00,100.00\n")
    assert r.ok, r.errors
    assert r.records[0].balance == D('300.00')


def test_zero_credited_changes_nothing():
    r = xero("Bluebell Design Studio Ltd,mia@bluebell.example,INV-0042,,13/10/2026,27/10/2026,1500.00,250.00,500.00,1000.00,0.00,GBP,Sales invoice,Awaiting Payment,Design,1250.00\n")
    assert r.ok, r.errors
    assert r.records[0].balance == D('1000.00')


def test_credited_without_paid_or_amount_due_reduces_the_balance():
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency,amount credited\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,600.00,EUR,100.00\n")
    assert r.ok, r.errors
    assert r.records[0].balance == D('500.00')


def test_xero_without_amount_due_column_uses_total_minus_paid_minus_credited():
    head = "ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountPaid,AmountCredited,Currency,Type,Status\n"
    r = read_invoices('xero', head + "Oak LLP,lee@oak.example,INV-7,14/10/2026,28/10/2026,600.00,200.00,100.00,GBP,Sales invoice,Awaiting Payment\n", date_order='dmy')
    assert r.ok, r.errors
    assert r.records[0].balance == D('300.00')


def test_credit_note_with_negative_figures_including_credited_imports():
    r = xero("Oak LLP,lee@oak.example,CN-9,,14/10/2026,28/10/2026,-100.00,0.00,0.00,-60.00,-40.00,GBP,Sales credit note,Awaiting Payment,Refund,-100.00\n")
    assert r.ok, r.errors
    assert r.credit_notes and r.credit_notes[0].amount == D('60.00')


def test_line_items_that_disagree_on_credited_are_refused():
    r = xero("Oak LLP,lee@oak.example,INV-8,,14/10/2026,28/10/2026,600.00,100.00,200.00,300.00,100.00,GBP,Sales invoice,Awaiting Payment,A,300.00\n",
             "Oak LLP,lee@oak.example,INV-8,,14/10/2026,28/10/2026,600.00,100.00,200.00,300.00,0.00,GBP,Sales invoice,Awaiting Payment,B,200.00\n")
    assert not r.ok
