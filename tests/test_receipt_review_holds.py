"""Review safety covers the whole ledger, while display candidates stay bounded."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer, Invoice, Payment, Store, match_payments, plan_followups
from cashdesk.store import ConflictError
DAY=dt.date(2026,11,10)


def inv(number,cid,balance='100'):
    return Invoice(number,cid,dt.date(2026,10,1),dt.date(2026,10,20),D(balance),'EUR',D(balance))


def plan(s):
    return plan_followups(s.list_invoices(),s.list_customers(),s.verified_matches(),s.chase_state(),DAY,
                          sender='Demo Studio',holds=s.reminder_holds(DAY))


def setup():
    s=Store(':memory:',today=lambda:DAY)
    s.upsert_customers([Customer('A','Orchard Demo',email='a@orchard.example'),
                        Customer('B','Birch Demo',email='b@birch.example'),
                        Customer('C','Unrelated Workshop',email='c@unrelated.example')])
    s.upsert_invoices([inv('INV-A1','A'),inv('INV-B1','B'),inv('INV-B2','B','200'),inv('INV-C1','C','300')])
    drafts=plan(s).drafts;s.create_drafts(drafts)
    for d in drafts:s.approve_draft(d.id,d.hash)
    return s,{d.customer_id:d for d in drafts}


@pytest.mark.parametrize('reference',['INV-A1','', 'INV-B1'])
@pytest.mark.parametrize('amount',['40','100','150'])
def test_receipt_review_pauses_reminders_for_payer_debt(reference,amount):
    s,drafts=setup()
    # An exact, correctly named receipt is a separate automatic positive control.
    if reference=='INV-B1' and amount=='100': amount='90'
    s.add_payments([Payment('P1',DAY,D(amount),'EUR',reference,'Birch Demo')])
    result=s.match_result(DAY)
    assert not result.applied
    assert {'INV-B1','INV-B2'}<=set(result.held_invoices)
    item=result.review[0] if result.review else result.unmatched[0]
    assert {'INV-B1','INV-B2'}<=set(item.holds)
    assert 'receipt review pauses reminders' in item.detail and 'Apply or Dismiss' in item.detail
    assert s.approval(drafts['B'].id) is None
    with pytest.raises(ConflictError,match='receipt review pauses reminders'):
        s.reserve_send(drafts['B'].id,drafts['B'].hash,'eml')
    with pytest.raises(ConflictError):s.approve_draft(drafts['B'].id,drafts['B'].hash)
    assert all(d.customer_id!='B' for d in plan(s).drafts)
    assert s.reserve_send(drafts['C'].id,drafts['C'].hash,'eml')
    assert not s.list_matches() and s.get_invoice('INV-B1').balance==D('100')


@pytest.mark.parametrize('payer',['','Unknown Sender'])
def test_equal_amount_holds_include_candidates_beyond_three(payer):
    invoices=[inv(f'INV-{i}',f'C{i}') for i in range(8)]
    customers=[Customer(f'C{i}',f'Company {i}') for i in range(8)]
    r=match_payments(invoices,[Payment('P1',DAY,D('100'),'EUR','',payer)],customers)
    item=r.review[0]
    assert len(item.candidates)==3
    assert set(item.holds)==set(i.number for i in invoices)==set(r.held_invoices)


def test_ambiguous_payer_holds_all_plausible_customers_and_preserves_unrelated():
    customers=[Customer('A','Bluebell Design Studio'),Customer('B','Bluebell Design Services'),Customer('C','Unrelated Workshop')]
    r=match_payments([inv('INV-A1','A'),inv('INV-B1','B'),inv('INV-C1','C','300')],
                     [Payment('P1',DAY,D('40'),'EUR','INV-A1','Bluebell Design')],customers)
    assert not r.applied and set(r.held_invoices)=={'INV-A1','INV-B1'}


@pytest.mark.parametrize('action',['apply','dismiss'])
def test_resolving_receipt_releases_holds_without_restoring_old_approval(action):
    s,drafts=setup();s.add_payments([Payment('P1',DAY,D('100'),'EUR','INV-A1','Birch Demo')])
    assert 'INV-B1' in s.reminder_holds(DAY)
    if action=='apply': s.apply_by_person('P1','INV-B1',D('100'))
    else: s.dismiss_payment('P1','verified noninvoice receipt')
    assert 'INV-B2' not in s.reminder_holds(DAY)
    assert s.approval(drafts['B'].id) is None
    replacement=next(d for d in plan(s).drafts if d.customer_id=='B')
    s.create_draft(replacement)
    s.approve_draft(replacement.id,replacement.hash)
    assert s.reserve_send(replacement.id,replacement.hash,'eml')


def test_holds_survive_reopen_until_person_resolves(tmp_path):
    path=tmp_path/'ledger.sqlite'
    with Store(path,today=lambda:DAY) as s:
        s.upsert_customers([Customer('B','Birch Demo',email='b@birch.example')]);s.upsert_invoices([inv('INV-B1','B')])
        s.add_payments([Payment('P1',DAY,D('40'),'EUR','','Birch Demo')])
    with Store(path,today=lambda:DAY) as s:assert 'INV-B1' in s.reminder_holds(DAY)


def test_correct_exact_receipt_still_applies_and_does_not_hold_unrelated_debt():
    s,_=setup();s.add_payments([Payment('P1',DAY,D('100'),'EUR','INV-B1','Birch Demo')])
    r=s.match_result(DAY);assert len(r.applied)==1 and not r.held_invoices
    s.apply_match(r.applied[0]);assert s.get_invoice('INV-B1').balance==0


def test_known_payer_does_not_pause_unrelated_equal_amount_invoice():
    s,_=setup()
    s.add_payments([Payment('P1',DAY,D('100'),'EUR','','Birch Demo')])
    r=s.match_result(DAY)
    assert set(r.held_invoices)=={'INV-B1','INV-B2'}
    assert 'INV-A1' not in r.review[0].holds


@pytest.mark.parametrize('status,balance',[('paid','0'),('void','100')])
def test_reference_to_closed_invoice_still_protects_actual_payer(status,balance):
    from dataclasses import replace
    s,_=setup()
    s.upsert_invoices([replace(s.get_invoice('INV-A1'),status=status,balance=D(balance))])
    s.add_payments([Payment('P1',DAY,D('100'),'EUR','INV-A1','Birch Demo')])
    r=s.match_result(DAY)
    assert not r.applied
    assert {'INV-B1','INV-B2'} <= set(r.held_invoices)
    assert r.review[0].kind in ('VOID','ALREADY_PAID')
