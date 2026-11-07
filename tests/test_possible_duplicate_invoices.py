"""One debt imported twice under two spellings is held, not demanded twice (P2aj / P2ab)."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer, Invoice
from cashdesk.store import Store, ConflictError

DAY = dt.date(2026, 11, 12)


def inv(number, cust='acme', amount='1200', ref='', cur='EUR'):
    return Invoice(number, cust, dt.date(2026, 10, 1), dt.date(2026, 10, 29), D(amount), cur, D(amount), 'open', ref)


def desk(*invoices):
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('acme', 'Acme Ltd', email='ap@acme.example'),
                        Customer('cus_A', 'Acme Ltd', email='AP@acme.example'),
                        Customer('beta', 'Beta GmbH', email='ap@beta.example')])
    s.upsert_invoices(list(invoices))
    return s


@pytest.mark.parametrize('a,b', [('INV-0041', 'inv-0041'), ('1041', 'INV-1041'), ('INV 41', 'INV-0041'), ('2026/101', '2026-101')])
def test_renumbered_same_debt_is_held(a, b):
    holds = desk(inv(a), inv(b)).reminder_holds(DAY)
    assert 'possible_duplicate' in holds[a] and 'possible_duplicate' in holds[b]


def test_stripe_invoice_synced_into_xero_by_reference_is_held_across_customer_ids():
    holds = desk(inv('ACME-0301', cust='cus_A'), inv('INV-0801', ref='ACME-0301')).reminder_holds(DAY)
    assert 'possible_duplicate' in holds['ACME-0301'] and 'possible_duplicate' in holds['INV-0801']


@pytest.mark.parametrize('b', [inv('INV-0042'), inv('inv-0041', amount='1100'), inv('inv-0041', cust='beta'), inv('inv-0041', cur='GBP')])
def test_different_invoices_are_not_held(b):
    holds = desk(inv('INV-0041'), b).reminder_holds(DAY)
    assert not any('possible_duplicate' in v for v in holds.values())


def test_not_duplicate_releases_the_pair_with_a_reason():
    s = desk(inv('INV-0041'), inv('inv-0041'))
    with pytest.raises(ConflictError, match='needs a reason'):
        s.confirm_distinct_invoices('INV-0041', 'inv-0041', reason='', by='test')
    s.confirm_distinct_invoices('inv-0041', 'INV-0041', reason='two separate jobs, both invoiced', by='test')
    assert not any('possible_duplicate' in v for v in s.reminder_holds(DAY).values())


def test_not_duplicate_refuses_a_pair_that_is_not_held():
    s = desk(inv('INV-0041'), inv('INV-0042'))
    with pytest.raises(ConflictError, match='are not held as a possible duplicate pair'):
        s.confirm_distinct_invoices('INV-0041', 'INV-0042', reason='x', by='test')


def test_paid_twin_holds_nothing():
    paid = Invoice('inv-0041', 'acme', dt.date(2026, 10, 1), dt.date(2026, 10, 29), D('1200'), 'EUR', D('0'), 'paid')
    holds = desk(inv('INV-0041'), paid).reminder_holds(DAY)
    assert not any('possible_duplicate' in v for v in holds.values())
