"""Reported overpayments are review evidence, not permission to chase gross debt."""
import csv
import io
import json
import sqlite3
import datetime as dt
from dataclasses import replace
from decimal import Decimal as D
import pytest
from cashdesk import Customer, Invoice, Payment, Store, plan_followups
from cashdesk.importers import read_invoices
from cashdesk.store import ConflictError
from tests.test_invoice_closures import export, DAY


def row(source='generic',**kw):
    paid=kw.pop('paid_amount',None)
    text=export(source,amount=kw.pop('amount','100'),**kw)
    if paid is not None:
        item=next(csv.DictReader(io.StringIO(text)));item['Amount Paid']=paid
        out=io.StringIO();w=csv.DictWriter(out,fieldnames=list(item),lineterminator='\n');w.writeheader();w.writerow(item);text=out.getvalue()
    return read_invoices(source,text)


def plan(s):
    return plan_followups(s.list_invoices(),s.list_customers(),s.verified_matches(),s.chase_state(),DAY,sender='Example Studio Ltd',holds=s.reminder_holds(DAY))


def setup(source='generic',path=':memory:'):
    s=Store(path,today=lambda:DAY)
    s.upsert_customers([Customer('C1','Alpha Demo',email='ap@alpha.example'),Customer('C2','Beta Demo',email='ap@beta.example')])
    for kw in [dict(balance='0',status='paid'),dict(number='INV-101',balance='100'),dict(number='INV-200',customer='C2',name='Beta Demo',balance='100')]:
        result=row(source,**kw);assert result.ok,result.errors;s.ingest(result)
    p=plan(s);s.create_drafts(p.all_drafts);s.save_chase_state(p.state)
    for d in p.drafts:s.approve_draft(d.id,d.hash)
    return s,{d.customer_id:d for d in p.drafts}


@pytest.mark.parametrize('source',['generic','xero','quickbooks','stripe'])
def test_exported_surplus_is_retained_and_customer_collection_authority_revoked(source):
    s,d=setup(source)
    r=row(source,balance='-10',status='paid',paid_amount='110');assert r.ok,r.errors
    counts=s.ingest(r,file_name='weekly.csv')
    assert counts['surplus_questions']==1
    q=s.surplus_reviews()[0]
    assert q['number']=='INV-100' and q['observations'][0]['balance']=='-10.00'
    assert q['customer_ids']==['C1'] and q['affected_invoices']==['INV-100','INV-101']
    assert q['observations'][0]['raw'] and q['observations'][0]['source']==source
    assert s.approval(d['C1'].id) is None
    assert 'customer surplus review' in s.reminder_holds(DAY)['INV-101']
    assert not [x for x in plan(s).drafts if x.customer_id=='C1']
    with pytest.raises(ConflictError,match='customer surplus review'):s.reserve_send(d['C1'].id,d['C1'].hash,'eml')
    assert s.reserve_send(d['C2'].id,d['C2'].hash,'eml')
    assert s.get_invoice('INV-101').balance==D('100') and not s.list_matches()


@pytest.mark.parametrize('source',['generic','xero','quickbooks','stripe'])
def test_paid_overpayment_without_due_date_preserves_credit_observation(source):
    s,d=setup(source)
    r=row(source,balance='-10',status='paid',due='',paid_amount='110')
    assert r.ok,r.errors
    s.ingest(r)
    assert s.surplus_reviews() and s.approval(d['C1'].id) is None
    assert s.surplus_reviews()[0]['observations'][0]['balance']=='-10.00'


def test_initial_terminal_surplus_does_not_invent_invoice_or_due_date():
    s=Store(':memory:',today=lambda:DAY);s.upsert_customers([Customer('C1','Alpha Demo',email='a@alpha.example')])
    r=row(balance='-10',status='paid',due='',paid_amount='110');assert r.ok
    s.ingest(r)
    assert not s.list_invoices() and s.surplus_reviews()[0]['number']=='INV-100'
    s.ingest(row(number='INV-101',balance='100'))
    assert 'INV-101' in s.reminder_holds(DAY)


@pytest.mark.parametrize('closure',[dict(balance='0',status='paid'),dict(balance='0',status='paid',due=''),dict(balance='0',status='void',due='')])
def test_zero_or_void_closure_cannot_erase_pending_surplus(closure):
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    s.ingest(row(note='later closing export',**closure))
    assert len(s.surplus_reviews())==1 and s.approval(d['C1'].id) is None
    assert 'INV-101' in s.reminder_holds(DAY)


def test_repeated_exports_are_observations_never_summed_as_extra_cash():
    s,d=setup()
    for note in ['weekly one','weekly two']:
        s.ingest(row(balance='-10',status='paid',paid_amount='110',note=note))
    questions=s.surplus_reviews();assert len(questions)==1
    assert len(questions[0]['observations'])==2
    assert {x['balance'] for x in questions[0]['observations']}=={'-10.00'}
    assert not s.list_payments() and s.get_invoice('INV-101').balance==D('100')


def test_surplus_hold_survives_reopen_and_prevents_new_drafts(tmp_path):
    path=tmp_path/'ledger.sqlite';s,d=setup(path=path)
    s.ingest(row(balance='-10',status='paid',paid_amount='110'));s.close()
    with Store(path,today=lambda:DAY) as s:
        assert s.surplus_reviews() and not [x for x in plan(s).drafts if x.customer_id=='C1']
        assert s.approval(d['C1'].id) is None


def test_customer_credit_verification_is_snapshot_bound_and_moves_no_money():
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    q=s.surplus_reviews()[0];before=(s.list_invoices(),s.list_matches(),s.list_sends())
    with pytest.raises(ConflictError,match='customer surplus review'):s.confirm_surplus('INV-100',expected_snapshot='stale',by='FAKE REVIEW')
    s.confirm_surplus('INV-100',expected_snapshot=q['snapshot'],by='FAKE REVIEW')
    assert not s.surplus_reviews() and not s.surplus_customer_holds()
    assert (s.list_invoices(),s.list_matches(),s.list_sends())==before
    assert s.approval(d['C1'].id) is None
    with pytest.raises(ConflictError):s.reserve_send(d['C1'].id,d['C1'].hash,'eml')
    assert [x for x in plan(s).drafts if x.customer_id=='C1']
    assert s._conn.execute('SELECT COUNT(*) FROM invoice_surpluses').fetchone()[0]==1


def test_new_observation_or_customer_change_invalidates_review_snapshot():
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    q=s.surplus_reviews()[0];s.upsert_customers([Customer('C1','Alpha Demo',email='new@alpha.example')])
    with pytest.raises(ConflictError,match='customer surplus review'):s.confirm_surplus('INV-100',expected_snapshot=q['snapshot'])


def test_regular_settlement_reconciliation_does_not_clear_surplus_review():
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    inv=s.get_invoice('INV-100');s.reconcile_settlement(inv.number,'100',expected_balance=inv.balance,currency='EUR',expected_snapshot=s.settlement_snapshot(inv.number))
    assert s.surplus_reviews() and 'INV-101' in s.reminder_holds(DAY)


def test_local_allocations_are_retained_without_double_counting_or_false_thanks():
    s,d=setup();s.ingest(row(number='INV-300',balance='60',paid_amount='40'))
    s.add_payments([Payment('P1',dt.date(2026,11,3),D('60'),'EUR','INV-300','Alpha Demo')])
    for m in s.match_result(DAY).applied:s.apply_match(m)
    history=s.list_matches();assert history
    s.ingest(row(number='INV-300',balance='-10',status='paid',paid_amount='110'))
    assert s.list_matches()==history and s.surplus_reviews()
    assert not s.verified_matches() and not plan(s).thanks
    assert s.get_invoice('INV-101').balance==D('100')


def test_legacy_basis_surplus_migrates_as_question_without_financial_rewrite(tmp_path):
    s,d=setup();s._conn.execute("UPDATE invoice_settlements SET external_paid='110',export_settled='110' WHERE number='INV-100'")
    sql='\n'.join(line for line in s._conn.iterdump() if 'invoice_surplus' not in line)
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as c:c.executescript(sql)
    before=s.list_invoices();s.close()
    with Store(path,today=lambda:DAY) as migrated:
        assert migrated.list_invoices()==before and migrated.surplus_reviews()
        assert migrated.approval(d['C1'].id) is None
        q=migrated.surplus_reviews()[0]
        assert 'legacy' in q['observations'][0]['kind']
        migrated.confirm_surplus(q['number'],expected_snapshot=q['snapshot'],by='FAKE REVIEW')
    with Store(path,today=lambda:DAY) as reopened:assert not reopened.surplus_reviews()


def test_direct_signed_sale_balance_also_creates_review():
    s,d=setup();s.upsert_invoices([replace(s.get_invoice('INV-100'),balance=D('-25'))])
    assert s.surplus_reviews() and s.approval(d['C1'].id) is None


def test_normal_credit_note_and_void_do_not_create_sale_surplus():
    s,d=setup();s.ingest(row(number='VOID-1',balance='0',status='void'))
    s.ingest(row(number='CREDIT-1',amount='-50',balance='-50',kind='Credit Note'))
    assert not s.surplus_reviews()


def test_surplus_preserves_source_currency_and_original_customer_on_identity_drift():
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    s.upsert_invoices([replace(s.get_invoice('INV-100'),customer_id='C2',currency='USD')])
    assert set(s.surplus_customer_holds())=={'C1','C2'}
    o=s.surplus_reviews()[0]['observations'][0];assert o['currency']=='EUR' and o['customer_id']=='C1'


def test_unknown_legacy_external_basis_is_not_invented_as_confirmed_credit(tmp_path):
    s,d=setup();s._conn.execute("UPDATE invoice_settlements SET external_paid=NULL,export_settled=NULL WHERE number='INV-100'")
    sql='\n'.join(line for line in s._conn.iterdump() if 'invoice_surplus' not in line)
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as c:c.executescript(sql)
    with Store(path,today=lambda:DAY) as migrated:assert not migrated.surplus_reviews()


def test_negative_observation_reappearing_after_verification_requires_fresh_review():
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    q=s.surplus_reviews()[0];s.confirm_surplus(q['number'],expected_snapshot=q['snapshot'],by='FAKE REVIEW')
    s.ingest(row(balance='-10',status='paid',paid_amount='110',note='later negative observation'))
    assert len(s.surplus_reviews())==1
    history=list(s._conn.execute('SELECT * FROM invoice_surpluses'))
    assert len(history)==2 and sum(x['resolved_at'] is not None for x in history)==1


def test_legacy_negative_audit_event_is_recovered_when_current_basis_no_longer_shows_it(tmp_path):
    s,d=setup();s.log_event('import_kept_payment','INV-100',{'export_balance':'-10.00','kept_balance':'0.00','external_paid':'110.00','applied':'0.00','currency':'EUR'})
    sql='\n'.join(line for line in s._conn.iterdump() if 'invoice_surplus' not in line)
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as c:c.executescript(sql)
    with Store(path,today=lambda:DAY) as migrated:
        q=migrated.surplus_reviews()[0]
        assert q['observations'][0]['kind']=='legacy_event'
        assert 'historical_customer_not_independently_verified' in q['observations'][0]['raw']
        assert migrated.approval(d['C1'].id) is None


def test_verified_external_plus_existing_local_excess_is_a_question_not_lost_money():
    s,d=setup();s.ingest(row(number='INV-300',balance='50',paid_amount='50'))
    s.add_payments([Payment('P1',dt.date(2026,11,3),D('50'),'EUR','INV-300','Alpha Demo')])
    # 50 equals the export's paid part, so it is not applied automatically (P2ae); a person applies it.
    assert not s.match_result(DAY).applied
    s.apply_by_person('P1','INV-300',by='test')
    inv=s.get_invoice('INV-300');matches=s.list_matches()
    s.reconcile_settlement(inv.number,'75',expected_balance=inv.balance,currency='EUR',expected_snapshot=s.settlement_snapshot(inv.number))
    q=s.surplus_reviews()[0]
    assert q['observations'][0]['balance']=='-25.00' and s.list_matches()==matches
    assert 'INV-101' in s.reminder_holds(DAY)


def test_gross_correction_below_allocations_retains_potential_surplus():
    s,d=setup();s.ingest(row(number='INV-300',balance='100'))
    s.add_payments([Payment('P1',dt.date(2026,11,3),D('100'),'EUR','INV-300','Alpha Demo')])
    for m in s.match_result(DAY).applied:s.apply_match(m)
    matches=s.list_matches();s.ingest(row(number='INV-300',amount='80',balance='80',note='gross corrected'))
    assert s.surplus_reviews()[0]['observations'][0]['balance']=='-20.00'
    assert s.list_matches()==matches and s.get_invoice('INV-101').balance==D('100')


def test_surplus_verification_snapshot_cannot_be_empty():
    s,d=setup();s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    with pytest.raises(ConflictError,match='customer surplus review'):s.confirm_surplus('INV-100',expected_snapshot='')


def test_prior_sent_history_is_never_reopened_or_rewritten_by_surplus_review():
    s,d=setup();key=s.reserve_send(d['C1'].id,d['C1'].hash,'eml');s.confirm_send(key)
    sent=s.list_sends();draft=s.get_draft(d['C1'].id)
    s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    assert s.list_sends()==sent and s.get_draft(draft.id)==draft
    q=s.surplus_reviews()[0];s.confirm_surplus(q['number'],expected_snapshot=q['snapshot'],by='FAKE REVIEW')
    assert s.list_sends()==sent and s.get_draft(draft.id)==draft


def test_late_authoritative_contact_does_not_escape_name_derived_surplus_owner():
    s=Store(':memory:',today=lambda:DAY)
    s.ingest(row('xero',balance='-10',status='paid',paid_amount='110'))
    original=s.surplus_reviews()[0]['observations'][0]['customer_id']
    s.upsert_customers([Customer(original,'Archived Alias',email='old@alpha.example'),Customer('C1','Alpha Demo',email='ap@alpha.example')])
    s.ingest(row('xero',number='INV-101',balance='100'))
    assert 'C1' in s.surplus_customer_holds()
    assert not plan(s).drafts


def test_other_currency_surplus_stays_explicit_without_automatic_fx_setoff():
    s,d=setup();s.ingest(row(number='USD-100',currency='USD',balance='-10',status='paid',paid_amount='110'))
    q=s.surplus_reviews()[0];assert q['observations'][0]['currency']=='USD'
    assert s.get_invoice('INV-101').balance==D('100')
    assert not [x for x in plan(s).drafts if x.customer_id=='C1']
    assert s.approval(d['C2'].id)


@pytest.mark.parametrize('source',['generic','xero','quickbooks','stripe'])
def test_open_reported_overpayment_without_due_date_still_retains_credit(source):
    s,d=setup(source);r=row(source,balance='-10',status='open',due='',paid_amount='110')
    assert r.ok,r.errors
    s.ingest(r)
    assert s.surplus_reviews() and s.approval(d['C1'].id) is None


def test_legacy_negative_operative_balance_requires_corrected_export_before_confirmation(tmp_path):
    s,d=setup();s._conn.execute("UPDATE invoices SET balance='-10' WHERE number='INV-100'")
    sql='\n'.join(line for line in s._conn.iterdump() if 'invoice_surplus' not in line)
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as c:c.executescript(sql)
    with Store(path,today=lambda:DAY) as migrated:
        q=migrated.surplus_reviews()[0]
        with pytest.raises(ConflictError,match='customer surplus review'):
            migrated.confirm_surplus(q['number'],expected_snapshot=q['snapshot'])


def test_explicit_unrelated_customer_id_is_not_merged_by_equal_display_name():
    s,d=setup();s.upsert_customers([Customer('C2','Alpha Demo',email='other@alpha.example')])
    # Contact changes correctly revoke the old text; isolate the surplus hold
    # assertion with a fresh, independently approved draft for the new contact.
    for old,why in s.stale_drafts():s.reject_draft(old.id,why)
    fresh=next(x for x in plan(s).drafts if x.customer_id=='C2')
    s.create_draft(fresh);s.approve_draft(fresh.id,fresh.hash);d['C2']=fresh
    s.ingest(row(balance='-10',status='paid',paid_amount='110'))
    assert set(s.surplus_customer_holds())=={'C1'}
    assert s.approval(d['C2'].id)


def test_first_surplus_and_customer_change_in_same_export_protect_previous_owner():
    s,d=setup();s.ingest(row(customer='C2',name='Beta Demo',balance='-10',status='paid',paid_amount='110'))
    assert set(s.surplus_customer_holds())=={'C1','C2'}
    q=s.surplus_reviews()[0]
    assert q['observations'][0]['prior_customer_id']=='C1'
    assert s.approval(d['C1'].id) is None and s.approval(d['C2'].id) is None
