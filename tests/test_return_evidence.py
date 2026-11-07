"""Return observations pause authority without reversing settled financial history."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer,Invoice,Payment,Store,plan_followups
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError
from tests.test_receipt_lifecycle import setup,bank,ingest,plan,DAY


@pytest.mark.parametrize('reference',['','INV-A1'])
@pytest.mark.parametrize('memo',['RETURNED R01','payment reversed','chargeback','unpaid cheque returned'])
def test_separate_return_revokes_existing_thanks_without_undo(reference,memo):
    s,drafts=setup(); history=s.list_matches(); before=s.get_invoice('INV-A1')
    ingest(s,bank(pid='R1',amount='-1200',reference=reference,memo=memo,date='2026-11-04'))
    assert s.approval(drafts['A'].id) is None
    with pytest.raises(ConflictError,match='returned payment review'):s.approve_draft(drafts['A'].id,drafts['A'].hash)
    with pytest.raises(ConflictError,match='returned payment review'):s.reserve_send(drafts['A'].id,drafts['A'].hash,'eml')
    assert s.list_matches()==history and s.get_invoice('INV-A1')==before
    assert not s.verified_matches() and not plan(s).thanks
    assert s.reserve_send(drafts['C'].id,drafts['C'].hash,'eml')
    assert s.return_reviews()[0]['receipts']==['P1']


@pytest.mark.parametrize('state',['Returned','Reverted','Reversed'])
@pytest.mark.parametrize('pid',['R1',''])
def test_nonbooked_return_without_original_id_is_evidence_not_new_cash(state,pid):
    s,drafts=setup(); history=s.list_matches()
    result=read_bank_csv(bank(pid=pid,status=state,reference='',amount='-1200',date='2026-11-04'))
    assert result.ok and not result.records
    s.ingest(result)
    assert s.approval(drafts['A'].id) is None and not s.verified_matches()
    assert s.return_reviews() and s.return_reviews()[0]['evidence']['raw']
    assert len(s.list_payments())==1 and s.list_matches()==history


def test_unknown_return_identity_holds_all_plausible_same_amount_receipts():
    s,drafts=setup()
    s.upsert_invoices([Invoice('INV-B2','B',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    ingest(s,bank(pid='P2',payer='Beta Demo',reference='INV-B2'))
    for m in s.match_result(DAY).applied:s.apply_match(m)
    ingest(s,bank(pid='R1',payer='',amount='-1200',reference='',memo='RETURNED R01',date='2026-11-04'))
    q=s.return_reviews()[0]
    assert set(q['receipts'])=={'P1','P2'} and {'INV-A1','INV-B2'}<=set(q['affected_invoices'])
    assert s.approval(drafts['A'].id) is None and s.approval(drafts['B'].id) is None
    assert not s.verified_matches()


def test_status_only_unknown_return_has_visible_conservative_scope():
    s,drafts=setup()
    s.ingest(read_bank_csv('id,date,amount,currency,status\nR1,,,,Returned\n'))
    assert s.return_reviews()[0]['scope']=='uncertain'
    assert all(s.approval(d.id) is None for d in drafts.values())
    assert not s.verified_matches()


@pytest.mark.parametrize('memo',['ordinary supplier transfer','refund requested by customer','refund INV-A1'])
def test_ordinary_outgoing_is_not_inferred_to_be_bounced_receipt(memo):
    s,drafts=setup()
    ingest(s,bank(pid='OUT1',amount='-1200',reference='',memo=memo,date='2026-11-04'))
    assert not s.return_reviews()
    assert s.approval(drafts['A'].id) and s.verified_matches()


def test_return_holds_other_debt_of_affected_customer_but_never_reopens_paid_invoice():
    s,drafts=setup();before=s.get_invoice('INV-A1')
    s.upsert_invoices([Invoice('INV-A2','A',dt.date(2026,10,1),dt.date(2026,10,20),D('100'),'EUR',D('100'))])
    ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01',date='2026-11-04'))
    assert 'INV-A2' in s.reminder_holds(DAY) and not [d for d in plan(s).drafts if d.customer_id=='A']
    assert s.get_invoice('INV-A1')==before


def test_return_keeps_sent_history_and_never_restores_old_approval():
    s,drafts=setup();d=drafts['A']
    token=s.reserve_send(d.id,d.hash,'eml');s.confirm_send(token)
    before=s.list_sends(); history=s.list_matches()
    ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01',date='2026-11-04'))
    assert s.get_draft(d.id).status=='sent' and s.list_sends()==before and s.list_matches()==history
    assert not plan(s).thanks


def test_verified_unrelated_resolution_is_snapshot_bound_audited_and_requires_fresh_text():
    s,drafts=setup()
    ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01',date='2026-11-04'))
    q=s.return_reviews()[0]
    with pytest.raises(ConflictError,match='returned payment review'):s.confirm_return_unrelated(q['id'],expected_snapshot='stale',reason='Statement verified unrelated',by='test')
    with pytest.raises(ConflictError,match='returned payment review'):s.confirm_return_unrelated(q['id'],expected_snapshot=q['snapshot'],reason='',by='test')
    before=s.list_matches()
    s.confirm_return_unrelated(q['id'],expected_snapshot=q['snapshot'],reason='Verified bank statement: separate supplier reversal, original receipt remains settled',by='test')
    assert not s.return_reviews() and s.list_matches()==before
    assert s.approval(drafts['A'].id) is None and plan(s).thanks


def test_return_evidence_survives_dismissal_reimport_and_reopen(tmp_path):
    path=tmp_path/'scratch.sqlite';s=Store(path,today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example')])
    s.upsert_invoices([Invoice('INV-A1','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01'))
    q=s.return_reviews()[0];s.dismiss_payment('R1','not cash');s.close()
    with Store(path,today=lambda:DAY) as s:
        assert s.return_reviews()[0]['id']==q['id'] and 'INV-A1' in s.reminder_holds(DAY)


def test_pure_planner_respects_return_hold_even_with_historical_allocations():
    s,_=setup();p=plan_followups(s.list_invoices(),s.list_customers(),s.list_matches(),s.chase_state().__class__(),as_of=DAY,sender='Demo',holds={'INV-A1':'returned_payment: returned payment review'})
    assert not p.thanks


@pytest.mark.parametrize('amount,currency,payer',[('-600','EUR','Alpha Demo'),('-1200','USD','Alpha Demo'),('-1200','EUR','Alpha'),('-1200','EUR','')])
def test_uncertain_return_amount_currency_or_truncated_payer_cannot_authorize_old_thanks(amount,currency,payer):
    s,drafts=setup()
    ingest(s,bank(pid='R1',amount=amount,currency=currency,payer=payer,reference='',memo='RETURNED R01',date='2026-11-04'))
    assert s.approval(drafts['A'].id) is None and s.return_reviews()


def test_return_resolution_snapshot_covers_new_invoices_and_other_reviewed_observations():
    s,_=setup();ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01'))
    q=s.return_reviews()[0]
    s.upsert_invoices([Invoice('INV-A2','A',dt.date(2026,10,1),dt.date(2026,10,20),D('99'),'EUR',D('99'))])
    with pytest.raises(ConflictError,match='returned payment review'):
        s.confirm_return_unrelated(q['id'],expected_snapshot=q['snapshot'],reason='Verified bank statement unrelated',by='test')
    assert 'INV-A2' in s.return_holds()


def test_return_keeps_prior_and_current_customer_protected():
    from dataclasses import replace
    s,drafts=setup();ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01'))
    s.upsert_invoices([replace(s.get_invoice('INV-A1'),customer_id='B')])
    q=s.return_reviews()[0]
    assert {'A','B'}<=set(q['customer_ids']) and s.approval(drafts['B'].id) is None


def test_old_return_payment_is_backfilled_on_open_without_data_deletion(tmp_path):
    path=tmp_path/'scratch.sqlite';s=Store(path,today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example')])
    s.upsert_invoices([Invoice('INV-A1','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    # Construct an older-schema financial row, without current insertion hooks.
    s._conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?)",('R1','2026-11-04','-1200','EUR','','Alpha Demo','RETURNED R01','bank','2026-11-04T00:00:00+00:00'))
    s.close()
    with Store(path,today=lambda:DAY) as s:
        assert len(s.list_payments())==1 and s.return_reviews() and 'INV-A1' in s.return_holds()


def test_nonbooked_return_resolution_survives_reopen_without_duplicate_question(tmp_path):
    path=tmp_path/'scratch.sqlite'
    with Store(path,today=lambda:DAY) as s:
        s.ingest(read_bank_csv(bank(pid='R1',status='Returned',reference='')))
        q=s.return_reviews()[0]
        s.confirm_return_unrelated(q['id'],expected_snapshot=q['snapshot'],reason='Verified original receipt not affected',by='test')
    with Store(path,today=lambda:DAY) as s:assert not s.return_reviews()


def test_return_scope_remembers_intermediate_customer_after_further_reassignment():
    from dataclasses import replace
    s,_=setup();ingest(s,bank(pid='R1',amount='-1200',reference='',memo='RETURNED R01'))
    for customer in ['B','C']:s.upsert_invoices([replace(s.get_invoice('INV-A1'),customer_id=customer)])
    assert {'A','B','C'}<=set(s.return_reviews()[0]['customer_ids'])


def test_return_id_on_different_known_account_cannot_be_lost_as_already_linked():
    s=Store(':memory:',today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example')])
    s.upsert_invoices([Invoice('INV-A1','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    ingest(s,bank(account='123'))
    for m in s.match_result(DAY).applied:s.apply_match(m)
    d=plan(s).thanks[0];s.approve_draft(d.id,d.hash)
    ingest(s,bank(account='456',status='Returned',reference='',date='2026-11-04'))
    assert s.return_reviews() or s.receipt_conflicts()
    assert s.approval(d.id) is None and not s.verified_matches()


def test_positive_booked_return_observation_cannot_be_placed_as_incoming_cash():
    s=Store(':memory:',today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example')])
    s.upsert_invoices([Invoice('INV-A1','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    ingest(s,bank(pid='R1',memo='chargeback reversal'))
    assert not s.match_result(DAY).applied
    assert not s.match_result(DAY).totals()['review_in']
    with pytest.raises(ConflictError,match='this observation is not verified incoming cash'):s.apply_by_person('R1','INV-A1')
    assert not s.list_matches() and s.get_invoice('INV-A1').balance==D('1200')
