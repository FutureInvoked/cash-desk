"""A receipt whose return is already known is not allocated until a person verifies it (P2y)."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer, Invoice, Match, Store
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError
from tests.test_receipt_lifecycle import bank, ingest, plan, DAY

HEAD = 'id,date,amount,currency,counterparty,reference,status,memo\n'
P1 = 'P1,2026-11-02,1200,EUR,Alpha Demo,INV-A1,Completed,\n'
R1 = 'R1,2026-11-04,-1200,EUR,Alpha Demo,INV-A1,Completed,RETURNED R01\n'


def fresh():
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('A', 'Alpha Demo', email='a@alpha.example'),
                        Customer('B', 'Beta Demo', email='b@beta.example')])
    s.upsert_invoices([Invoice('INV-A1', 'A', dt.date(2026, 10, 1), dt.date(2026, 10, 22), D('1200'), 'EUR', D('1200')),
                       Invoice('INV-B1', 'B', dt.date(2026, 10, 1), dt.date(2026, 10, 22), D('300'), 'EUR', D('300'))])
    return s


def statement(*rows):
    r = read_bank_csv(HEAD + ''.join(rows)); assert r.ok, r.errors
    return r


def returned_statement():
    s = fresh(); s.ingest(statement(P1, R1), file_name='bank.csv'); return s


def test_receipt_with_known_return_is_not_proposed_or_applied():
    s = returned_statement()
    result = s.match_result(DAY)
    assert not [m for m in result.applied if m.payment_id == 'P1']
    item = [i for i in result.review if i.payment.id == 'P1']
    assert item and item[0].kind == 'RETURNED_PAYMENT' and not item[0].candidates
    assert 'may have been returned' in item[0].detail
    assert 'EUR' not in result.totals()['review_in']
    inv = s.get_invoice('INV-A1')
    assert inv.balance == D('1200') and inv.status == 'open' and not s.list_matches()


def test_known_return_blocks_automatic_and_person_allocation():
    s = returned_statement()
    auto = Match('P1', 'INV-A1', D('1200'), 'EUR', 'reference+exact', 'high', 'exact')
    with pytest.raises(ConflictError, match='may have been returned'):
        s.apply_match(auto)
    with pytest.raises(ConflictError, match='may have been returned'):
        s.apply_by_person('P1', 'INV-A1', by='test')
    assert not s.list_matches() and s.get_invoice('INV-A1').balance == D('1200')


def test_return_in_a_later_file_before_matching_also_quarantines():
    s = fresh()
    s.ingest(statement(P1), file_name='week1.csv')
    s.ingest(statement(R1), file_name='week2.csv')
    assert not [m for m in s.match_result(DAY).applied if m.payment_id == 'P1']
    with pytest.raises(ConflictError, match='may have been returned'):
        s.apply_by_person('P1', 'INV-A1', by='test')


def test_known_return_holds_collection_and_acknowledgement():
    s = returned_statement()
    p = plan(s)
    assert not [d for d in p.all_drafts if d.customer_id == 'A']
    assert 'INV-A1' in s.reminder_holds(DAY)


def test_unrelated_genuine_receipts_still_flow():
    s = returned_statement()
    s.upsert_invoices([Invoice('INV-A2', 'A', dt.date(2026, 10, 1), dt.date(2026, 10, 22), D('100'), 'EUR', D('100'))])
    s.ingest(statement('P2,2026-11-03,300,EUR,Beta Demo,INV-B1,Completed,\n',
                       'P3,2026-11-03,100,EUR,Alpha Demo,INV-A2,Completed,\n'), file_name='more.csv')
    applied = {m.payment_id for m in s.match_result(DAY).applied}
    assert {'P2', 'P3'} <= applied and 'P1' not in applied
    for m in s.match_result(DAY).applied:
        s.apply_match(m)
    assert s.get_invoice('INV-B1').status == 'paid' and s.get_invoice('INV-A2').status == 'paid'
    assert s.get_invoice('INV-A1').balance == D('1200')


def test_uncertain_return_amount_quarantines_every_scope_receipt():
    s = fresh()
    s.ingest(statement(P1, 'P2,2026-11-03,300,EUR,Beta Demo,INV-B1,Completed,\n'), file_name='bank.csv')
    s.ingest(read_bank_csv('id,date,amount,currency,status\nR9,,,,Returned\n'))
    assert not s.match_result(DAY).applied
    with pytest.raises(ConflictError, match='may have been returned'):
        s.apply_by_person('P2', 'INV-B1', by='test')


def test_verified_unrelated_releases_the_receipt_for_allocation():
    s = returned_statement()
    q = s.return_reviews()[0]
    assert q['quarantined'] == ['P1']
    s.confirm_return_unrelated(q['id'], expected_snapshot=q['snapshot'],
                               reason='Statement shows R1 returned a different item; P1 remains credited', by='test')
    applied = s.match_result(DAY).applied
    assert [m.payment_id for m in applied] == ['P1']
    s.apply_match(applied[0])
    assert s.get_invoice('INV-A1').status == 'paid'


def test_verified_reversal_closes_both_rows_without_allocation_and_resumes_chasing():
    s = returned_statement()
    q = s.return_reviews()[0]
    with pytest.raises(ConflictError, match='returned payment review'):
        s.confirm_return_reverses(q['id'], 'P1', expected_snapshot='stale', reason='bank confirms R01 return of P1', by='test')
    with pytest.raises(ConflictError, match='returned payment review'):
        s.confirm_return_reverses(q['id'], 'P1', expected_snapshot=q['snapshot'], reason='', by='test')
    with pytest.raises(ConflictError, match='is not a plausible unallocated receipt'):
        s.confirm_return_reverses(q['id'], 'R1', expected_snapshot=q['snapshot'], reason='bank confirms', by='test')
    s.confirm_return_reverses(q['id'], 'P1', expected_snapshot=q['snapshot'],
                              reason='Bank statement: R1 is the R01 return of P1', by='test')
    assert not s.return_reviews() and not s.list_matches()
    assert s.get_invoice('INV-A1').balance == D('1200') and s.get_invoice('INV-A1').status == 'open'
    assert not [m for m in s.match_result(DAY).applied if m.payment_id == 'P1']
    assert not [i for i in s.match_result(DAY).review if i.payment.id == 'P1']
    assert [e for e in s.events() if e.kind == 'bank_return_reverses_unapplied']
    assert len(s.list_payments()) == 2  # raw rows retained
    p = plan(s)
    assert [d for d in p.drafts if d.customer_id == 'A']


def test_reversal_refuses_an_already_allocated_receipt():
    s = fresh()
    s.ingest(statement(P1), file_name='week1.csv')
    for m in s.match_result(DAY).applied:
        s.apply_match(m)
    history = s.list_matches()
    s.ingest(statement(R1), file_name='week2.csv')
    q = s.return_reviews()[0]
    assert q['quarantined'] == []
    with pytest.raises(ConflictError, match='needs an audited correction, not this command'):
        s.confirm_return_reverses(q['id'], 'P1', expected_snapshot=q['snapshot'], reason='bank confirms', by='test')
    assert s.list_matches() == history and s.get_invoice('INV-A1').status == 'paid'


def test_reversal_refuses_amount_or_currency_mismatch():
    s = fresh()
    s.ingest(statement('P1,2026-11-02,1200,EUR,Alpha Demo,INV-A1,Completed,\n',
                       'R1,2026-11-04,-500,EUR,Alpha Demo,INV-A1,Completed,RETURNED R01\n'), file_name='bank.csv')
    q = s.return_reviews()[0]
    assert q['quarantined'] == []  # a 500 return cannot be the whole 1200 receipt
    with pytest.raises(ConflictError, match='is not a plausible unallocated receipt'):
        s.confirm_return_reverses(q['id'], 'P1', expected_snapshot=q['snapshot'], reason='bank confirms', by='test')


def test_remainder_of_partly_allocated_receipt_is_held_but_not_reversible():
    s = fresh()
    s.ingest(statement(P1), file_name='week1.csv')
    s.upsert_invoices([Invoice('INV-A2', 'A', dt.date(2026, 10, 1), dt.date(2026, 10, 22), D('400'), 'EUR', D('400'))])
    s.apply_by_person('P1', 'INV-A2', by='test')
    history = s.list_matches()
    s.ingest(statement(R1), file_name='week2.csv')
    q = s.return_reviews()[0]
    assert q['quarantined'] == ['P1']
    with pytest.raises(ConflictError, match='may have been returned'):
        s.apply_by_person('P1', 'INV-A1', by='test')
    with pytest.raises(ConflictError, match='needs an audited correction, not this command'):
        s.confirm_return_reverses(q['id'], 'P1', expected_snapshot=q['snapshot'], reason='bank confirms', by='test')
    assert s.list_matches() == history and s.get_invoice('INV-A1').balance == D('1200')
