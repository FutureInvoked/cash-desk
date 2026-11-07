"""A blank cell in a present balance column is not 'the full amount is owed' (P2aa)."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer
from cashdesk.chasing import plan_followups
from cashdesk.importers import read_invoices
from cashdesk.store import Store, ConflictError

DAY = dt.date(2026, 11, 5)
GEN = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance{extra}\n"
QBO = 'Date,Num,Customer,Due date,Amount,Open balance,Status\n'


def gen(rows, extra=""):
    return GEN.format(extra=extra) + "".join(rows)


def store():
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('acme', 'Acme Demo', email='ap@acme.example'),
                        Customer('beta', 'Beta Demo', email='ap@beta.example')])
    return s


def drafts(s):
    p = plan_followups(s.list_invoices(), s.list_customers(), s.verified_matches(), s.chase_state(), DAY,
                       sender='Demo Creditor', holds=s.reminder_holds(DAY))
    return {n for d in p.drafts for n in d.invoice_numbers}


def test_generic_blank_balance_without_status_is_a_row_error_not_a_full_debt():
    r = read_invoices('generic', gen(["INV-0041,acme,2026-09-01,2026-10-22,1200.00,EUR,\n",
                                      "INV-0042,beta,2026-09-01,2026-10-22,800.00,EUR,800.00\n"]))
    assert not r.ok
    assert 'balance cell is blank' in r.errors[0].message and 'INV-0041' in r.errors[0].message
    assert [i.number for i in r.records] == ['INV-0042']
    s = store()
    with pytest.raises(ConflictError):
        s.ingest(r)
    s.ingest(r, allow_errors=True)
    assert 'INV-0041' not in {i.number for i in s.list_invoices()}
    assert drafts(s) == {'INV-0042'}


def test_qbo_blank_open_balance_without_status_is_refused_but_paid_row_closes():
    r = read_invoices('quickbooks', QBO + '10/02/2026,1047,Acme Ltd,10/16/2026,"1,200.00",,Paid\n'
                                          '10/02/2026,1048,Beta GmbH,10/16/2026,"800.00",,\n',
                      default_currency='USD', date_order='mdy')
    assert [e.message for e in r.errors if 'balance cell is blank' in e.message]
    assert '1048' in r.errors[0].message
    assert [(i.number, i.status, i.balance) for i in r.records] == [('1047', 'paid', D('0.00'))]


@pytest.mark.parametrize('status', ['Open', 'Overdue'])
def test_blank_balance_with_an_open_status_is_still_unknown(status):
    r = read_invoices('generic', gen([f"INV-1,acme,2026-09-01,2026-10-22,100.00,EUR,,{status}\n"], ",status"))
    assert not r.ok and 'balance cell is blank' in r.errors[0].message


def test_blank_balance_on_existing_open_invoice_holds_it_instead_of_rechasing_full_amount():
    s = store()
    s.ingest(read_invoices('generic', gen(["INV-0041,acme,2026-09-01,2026-10-22,1200.00,EUR,1200.00\n"])))
    assert drafts(s) == {'INV-0041'}
    r = read_invoices('generic', gen(["INV-0041,acme,2026-09-01,2026-10-22,1200.00,EUR,\n"]))
    assert not r.ok
    s.ingest(r, allow_errors=True)
    assert 'INV-0041' not in drafts(s)


# ---- agreeing layouts keep working ------------------------------------------------

def test_no_balance_column_still_means_the_full_amount_is_open():
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency\n"
                                 "INV-1,acme,2026-09-01,2026-10-22,100.00,EUR\n")
    assert r.ok and r.records[0].balance == D('100.00')


@pytest.mark.parametrize('status', ['paid', 'void'])
def test_blank_balance_with_terminal_status_is_settled(status):
    r = read_invoices('generic', gen([f"INV-1,acme,2026-09-01,2026-10-22,100.00,EUR,,{status}\n"], ",status"))
    assert r.ok, r.errors
    closed = list(r.records) + list(r.invoice_closures)
    assert closed and all(getattr(i, 'balance', getattr(i, 'exported_balance', None)) == 0 for i in closed)


def test_blank_balance_with_amount_paid_uses_the_paid_figure():
    r = read_invoices('generic', gen(["INV-1,acme,2026-09-01,2026-10-22,100.00,EUR,,40.00\n"], ",amount paid"))
    assert r.ok, r.errors
    assert r.records[0].balance == D('60.00')


def test_zero_balance_is_paid_and_explicit_balance_is_open():
    r = read_invoices('generic', gen(["INV-1,acme,2026-09-01,2026-10-22,100.00,EUR,0\n",
                                      "INV-2,beta,2026-09-01,2026-10-22,100.00,EUR,100.00\n"]))
    assert r.ok
    got = {i.number: (i.status, i.balance) for i in r.records}
    assert got['INV-2'] == ('open', D('100.00'))
    assert got.get('INV-1', ('paid', D('0')))[0] == 'paid'


def test_blank_credit_note_balance_is_unchanged_behaviour():
    r = read_invoices('generic', gen(["CN-1,acme,2026-09-01,2026-10-22,-50.00,EUR,\n"]))
    assert r.ok, r.errors
    assert r.credit_notes and r.credit_notes[0].amount == D('50.00')


# ---- reviewer findings: other ways an unread balance became 'full amount owed' -----

@pytest.mark.parametrize('header', ['balance due', 'Amount Outstanding', 'outstanding amount', 'open amount',
                                    'remaining', 'unpaid amount', 'Balance Due (EUR)', 'Outstanding Balance'])
def test_common_balance_headers_are_read(header):
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency," + header + "\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,100.00,EUR,0.00\n"
                                 "INV-2,acme,2026-09-01,2026-10-01,100.00,EUR,40.00\n")
    assert r.ok, r.errors
    got = {i.number: i.balance for i in r.records}
    assert got['INV-2'] == D('40.00') and got.get('INV-1', D('0')) == 0


@pytest.mark.parametrize('header', ['Balance Owing', 'Invoice Balance', 'Unpaid', 'Amount Still Outstanding'])
def test_unread_balance_like_header_refuses_the_file(header):
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency," + header + "\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,100.00,EUR,0.00\n")
    assert not r.ok and not r.records
    assert 'is not read: Cash Desk would chase the full amount' in r.errors[0].message


def test_stripe_customer_starting_and_ending_balance_are_not_open_balances():
    text = ("id,Number,Customer Name,Customer Email,Date (UTC),Due Date (UTC),Total,Amount Due,Amount Paid,"
            "Amount Remaining,Currency,Status,Starting Balance,Ending Balance\n"
            "in_1,INV-1,Acme Demo,ap@acme.example,2026-09-01,2026-10-01,100.00,100.00,0.00,100.00,eur,open,0,0\n")
    r = read_invoices('stripe', text)
    assert r.ok, r.errors
    assert r.records[0].balance == D('100.00')


def test_paid_date_without_balance_or_status_is_refused_not_chased():
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency,paid date\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,100.00,EUR,2026-10-10\n"
                                 "INV-2,acme,2026-09-01,2026-10-01,50.00,EUR,\n")
    assert not r.ok and 'has a paid date but no open balance' in r.errors[0].message
    assert [i.number for i in r.records] == ['INV-2']


def test_paid_date_with_a_balance_still_imports():
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency,balance,paid date\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,100.00,EUR,60.00,2026-10-10\n")
    assert r.ok, r.errors
    assert r.records[0].balance == D('60.00')


def test_blank_amount_paid_cell_without_balance_is_refused():
    r = read_invoices('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency,amount paid\n"
                                 "INV-1,acme,2026-09-01,2026-10-01,100.00,EUR,\n"
                                 "INV-2,acme,2026-09-01,2026-10-01,100.00,EUR,0\n")
    assert not r.ok and 'amount paid cell is blank' in r.errors[0].message
    assert [(i.number, i.balance) for i in r.records] == [('INV-2', D('100.00'))]


@pytest.mark.parametrize('source,text', [
    ('generic', "invoice_no,customer_id,issue_date,due_date,amount,currency,status\nINV-1,acme,2026-09-01,2026-10-01,1000.00,EUR,partial\n"),
    ('quickbooks', "Date,Num,Customer,Due date,Amount,Status\n09/01/2026,1001,Acme Corp,10/01/2026,1000.00,Partially paid\n"),
])
def test_part_paid_status_without_a_balance_column_is_refused(source, text):
    r = read_invoices(source, text, default_currency='EUR', date_order='mdy' if source == 'quickbooks' else None)
    assert not r.ok and 'but the file has no balance column' in r.errors[0].message
