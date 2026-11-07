"""A pending incoming line naming an invoice for its exact balance holds that invoice, briefly (P2ad)."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer
from cashdesk.importers import read_invoices, read_bank_csv
from cashdesk.store import Store

INV = ("invoice_no,customer_id,issue_date,due_date,amount,currency,balance\n"
       "INV-0042,beta,2026-09-01,2026-10-29,800.00,EUR,800.00\n"
       "INV-0043,beta,2026-09-01,2026-10-29,300.00,EUR,300.00\n")
HEAD = "id,date,amount,currency,counterparty,reference,status\n"


def desk(line, day=dt.date(2026, 11, 5)):
    s = Store(':memory:', today=lambda: day)
    s.upsert_customers([Customer('beta', 'Beta GmbH', email='ap@beta.example')])
    s.ingest(read_invoices('generic', INV))
    r = read_bank_csv(HEAD + line); assert r.ok, r.errors
    s.ingest(r, file_name='bank.csv')
    return s


def test_pending_line_for_exact_balance_holds_only_that_invoice_and_is_not_cash():
    s = desk("p1,2026-11-04,800.00,EUR,BETA GMBH,INV-0042,pending\n")
    h = s.reminder_holds(dt.date(2026, 11, 5))
    assert 'pending_receipt' in h.get('INV-0042', '') and 'INV-0043' not in h
    assert s.get_invoice('INV-0042').balance == D('800.00') and not s.list_matches()


def test_hold_expires_a_week_after_the_line():
    s = desk("p1,2026-11-04,800.00,EUR,BETA GMBH,INV-0042,pending\n")
    assert 'INV-0042' in s.reminder_holds(dt.date(2026, 11, 11))
    assert 'INV-0042' not in s.reminder_holds(dt.date(2026, 11, 12))


@pytest.mark.parametrize('line', [
    "p1,2026-11-04,500.00,EUR,BETA GMBH,INV-0042,pending\n",   # not the exact balance
    "p1,2026-11-04,800.00,GBP,BETA GMBH,INV-0042,pending\n",   # other currency
    "p1,2026-11-04,800.00,EUR,BETA GMBH,,pending\n",            # names no invoice
    "p1,2026-11-04,-800.00,EUR,BETA GMBH,INV-0042,pending\n",  # outgoing
    "p1,2026-11-04,800.00,EUR,BETA GMBH,INV-0042,declined\n",  # failed, not on its way
])
def test_other_pending_or_failed_lines_hold_nothing(line):
    s = desk(line)
    assert 'pending_receipt' not in s.reminder_holds(dt.date(2026, 11, 5)).get('INV-0042', '')
