"""A receipt the invoice export may already count is not applied automatically (P2ae)."""
import datetime as dt
from decimal import Decimal as D
from cashdesk import Customer
from cashdesk.chasing import plan_followups
from cashdesk.importers import read_invoices, read_bank_csv
from cashdesk.store import Store

DAY = dt.date(2026, 11, 5)
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\n"
BANK = "id,date,amount,currency,counterparty,reference,status,memo\n"


def desk(balance='500.00', amount='1000.00'):
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('acme', 'Acme Ltd', email='ap@acme.example')])
    r = read_invoices('generic', INV + f"INV-0041,acme,2026-10-01,2026-10-29,{amount},EUR,{balance}\n"); assert r.ok, r.errors
    s.ingest(r)
    return s


def bank(s, amount, pid='P1'):
    r = read_bank_csv(BANK + f"{pid},2026-11-03,{amount},EUR,Acme Ltd,INV-0041,Completed,\n"); assert r.ok, r.errors
    s.ingest(r, file_name=f'{pid}.csv')


def plan(s):
    return plan_followups(s.list_invoices(), s.list_customers(), s.verified_matches(), s.chase_state(), DAY,
                          sender='Demo', holds=s.reminder_holds(DAY))


def test_receipt_equal_to_the_paid_part_goes_to_review_and_holds_the_invoice():
    s = desk(); bank(s, '500.00')
    res = s.match_result(DAY)
    assert not res.applied
    item = [i for i in res.review if i.kind == 'ALREADY_COUNTED']
    assert item and 'receipt may already be counted' in item[0].detail and 'INV-0041' in item[0].detail
    assert 'INV-0041' in res.held_invoices
    p = plan(s)
    assert not [d for d in p.all_drafts if d.customer_id == 'acme']


def test_person_can_apply_it_as_a_further_payment():
    s = desk(); bank(s, '500.00')
    s.apply_by_person('P1', 'INV-0041', by='test')
    inv = s.get_invoice('INV-0041')
    assert inv.balance == 0 and inv.status == 'paid'


def test_receipt_of_another_amount_still_follows_normal_matching():
    s = desk(); bank(s, '200.00')
    res = s.match_result(DAY)
    assert not [i for i in res.review if i.kind == 'ALREADY_COUNTED']


def test_unpaid_invoice_receipt_is_applied_as_before():
    s = desk(balance='1000.00'); bank(s, '1000.00')
    res = s.match_result(DAY)
    assert [m.payment_id for m in res.applied] == ['P1']


def test_review_in_total_still_counts_it_as_possible_incoming_cash():
    s = desk(); bank(s, '500.00')
    assert s.match_result(DAY).totals()['review_in'].get('EUR') == D('500.00')
