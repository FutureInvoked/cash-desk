"""Xero PlannedDate pauses chasing; an invoice marked never sent is not reminded (P2al)."""
import datetime as dt
import pytest
from cashdesk import Customer
from cashdesk.importers import read_invoices
from cashdesk.store import Store

HEAD = ("ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,PlannedDate,Total,InvoiceAmountPaid,"
        "InvoiceAmountDue,Currency,Type,Sent,Status\n")


def row(no, planned='', sent='Sent', who='Acme Ltd,ap@acme.example'):
    return f"{who},{no},01/10/2026,29/10/2026,{planned},1200.00,0.00,1200.00,GBP,Sales invoice,{sent},AUTHORISED\n"


def holds_on(day, *rows, again=None):
    s = Store(':memory:', today=lambda: day)
    r = read_invoices('xero', HEAD + ''.join(rows), date_order='dmy'); assert r.ok, r.errors
    s.ingest(r, file_name='Invoices.csv')
    if again is not None:
        r2 = read_invoices('xero', HEAD + ''.join(again), date_order='dmy'); assert r2.ok
        s.ingest(r2, file_name='Invoices2.csv')
    return s.reminder_holds(day)


def test_planned_date_holds_until_two_days_after_it():
    assert 'planned_payment' in holds_on(dt.date(2026, 11, 5), row('INV-1', planned='20/11/2026')).get('INV-1', '')
    assert 'planned_payment' in holds_on(dt.date(2026, 11, 22), row('INV-1', planned='20/11/2026')).get('INV-1', '')
    assert 'planned_payment' not in holds_on(dt.date(2026, 11, 23), row('INV-1', planned='20/11/2026')).get('INV-1', '')


@pytest.mark.parametrize('sent', ['Not sent', 'NOT SENT', 'false', 'no'])
def test_explicitly_not_sent_invoice_is_held(sent):
    assert 'not_sent' in holds_on(dt.date(2026, 11, 5), row('INV-1', sent=sent)).get('INV-1', '')


@pytest.mark.parametrize('sent', ['Sent', 'true', ''])
def test_sent_or_blank_is_chased(sent):
    assert 'INV-1' not in holds_on(dt.date(2026, 11, 5), row('INV-1', sent=sent))


def test_next_export_without_the_flag_clears_it():
    h = holds_on(dt.date(2026, 11, 5), row('INV-1', sent='Not sent'), again=[row('INV-1', sent='Sent')])
    assert 'INV-1' not in h


def test_invalid_planned_date_is_a_warning_not_a_hold():
    r = read_invoices('xero', HEAD + row('INV-1', planned='soon'), date_order='dmy')
    assert r.ok and any('planned payment date' in w.message for w in r.warnings)
    assert not r.invoice_planned
