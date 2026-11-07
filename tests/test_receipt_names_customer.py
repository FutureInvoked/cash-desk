"""An unmatched receipt that names a customer pauses that customer's reminders (P2af)."""
import datetime as dt
from cashdesk import Customer
from cashdesk.chasing import plan_followups
from cashdesk.importers import read_invoices, read_bank_csv
from cashdesk.store import Store

DAY = dt.date(2026, 11, 5)
INV = ("invoice_no,customer_id,issue_date,due_date,amount,currency,balance\n"
       "INV-0041,acme,2026-09-01,2026-10-29,1000.00,EUR,1000.00\n"
       "INV-0042,acme,2026-09-01,2026-10-29,500.00,EUR,500.00\n"
       "INV-0051,beta,2026-09-01,2026-10-29,700.00,EUR,700.00\n")


def desk(bank_text):
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('acme', 'Acme Ltd', email='ap@acme.example'),
                        Customer('beta', 'Beta Trading GmbH', email='ap@beta.example')])
    s.ingest(read_invoices('generic', INV))
    r = read_bank_csv(bank_text); assert r.ok, r.errors
    s.ingest(r, file_name='bank.csv')
    return s


def reminded(s):
    p = plan_followups(s.list_invoices(), s.list_customers(), s.verified_matches(), s.chase_state(), DAY,
                       sender='Demo', holds=s.reminder_holds(DAY))
    return {d.customer_id for d in p.drafts}


H = "id,date,amount,currency,counterparty,reference,status,memo\n"


def test_director_pays_exact_sum_with_company_name_in_memo():
    s = desk(H + "P1,2026-11-03,1500.00,EUR,J SMITH,,Completed,ACME LTD\n")
    assert not s.match_result(DAY).applied
    assert reminded(s) == {'beta'}
    held = s.match_result(DAY).held_invoices
    assert 'receipt' in held['INV-0041'] and 'names Acme Ltd' in held['INV-0041']


def test_no_payer_column_name_in_description_short_by_fees():
    s = desk("date,description,amount,currency\n2026-11-03,ACME LTD BGC,1485.00,EUR\n")
    assert reminded(s) == {'beta'}


def test_receipt_naming_nobody_holds_nothing():
    s = desk(H + "P1,2026-11-03,1485.00,EUR,J SMITH,,Completed,THANKS\n")
    assert reminded(s) == {'acme', 'beta'}


def test_short_or_partial_name_word_is_not_enough():
    s = desk(H + "P1,2026-11-03,1485.00,EUR,ACMEX HOLDINGS,,Completed,\n")
    assert 'acme' in reminded(s)


def test_outgoing_payment_naming_the_customer_holds_nothing():
    s = desk(H + "P1,2026-11-03,-50.00,EUR,ACME LTD,,Completed,refund fee\n")
    assert reminded(s) == {'acme', 'beta'}


def test_dismissed_receipt_releases_the_hold():
    s = desk(H + "P1,2026-11-03,1500.00,EUR,J SMITH,,Completed,ACME LTD\n")
    pid = s.unapplied_payments()[0].id
    s.dismiss_payment(pid, reason='personal loan repayment, not an invoice payment')
    assert 'acme' in reminded(s)
