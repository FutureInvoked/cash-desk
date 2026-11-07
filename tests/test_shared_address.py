"""Two customer records behind one mailbox: shared holds, one reminder per address (P2ab)."""
import datetime as dt
from decimal import Decimal as D
from cashdesk import Customer, Invoice
from cashdesk.chasing import plan_followups
import pytest
from cashdesk.store import Store

DAY = dt.date(2026, 11, 5)


def inv(no, cust):
    return Invoice(no, cust, dt.date(2026, 9, 1), dt.date(2026, 10, 29), D('100'), 'EUR', D('100'), 'open')


def desk(email_b='ap@acme.example'):
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('xero-acme', 'Acme Ltd', email='ap@acme.example'),
                        Customer('cus_A', 'Acme Ltd', email=email_b)])
    s.upsert_invoices([inv('INV-1', 'xero-acme'), inv('ST-9', 'cus_A')])
    return s


def drafts(s):
    p = plan_followups(s.list_invoices(), s.list_customers(), s.verified_matches(), s.chase_state(), DAY,
                       sender='Demo', holds=s.reminder_holds(DAY))
    s.create_drafts(p.all_drafts, as_of=DAY)
    return [d for d in s.list_drafts() if d.kind.startswith('stage')]


def test_hold_on_one_record_holds_the_other_with_the_same_address():
    s = desk()
    s.set_hold('xero-acme', 'asked us to wait for their audit')
    h = s.reminder_holds(DAY)
    assert 'ST-9' in h and 'shares the address ap@acme.example' in h['ST-9']


def test_only_one_reminder_per_address_can_be_approved():
    s = desk()
    ds = drafts(s)
    assert len(ds) == 2
    first, second = sorted(ds, key=lambda d: d.id)
    s.approve_draft(first.id, first.hash)
    with pytest.raises(Exception, match='same address'):
        s.approve_draft(second.id, second.hash)


def test_different_addresses_are_independent():
    s = desk(email_b='billing@acme-shop.example')
    ds = drafts(s)
    for d in ds:
        s.approve_draft(d.id, d.hash)
    assert all(s.approval(d.id) for d in ds)


def test_hold_reaches_a_record_whose_cell_lists_the_held_address_among_others():
    s = desk(email_b='finance@acme.example; ap@acme.example')
    s.set_hold('xero-acme', 'asked us to wait')
    assert 'shares the address ap@acme.example' in s.reminder_holds(DAY).get('ST-9', '')
