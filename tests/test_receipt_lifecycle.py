"""Account identity, conflicting observations and adverse status never invent cash."""
import csv,io,datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer,Invoice,Payment,Store,plan_followups
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError,DuplicateImportError
DAY=dt.date(2026,11,10)


def bank(pid='P1',amount='1200',payer='Alpha Demo',reference='INV-A1',date='2026-11-03',currency='EUR',status='Completed',account='',memo='',bank_name='Demo Bank'):
    f=io.StringIO();w=csv.writer(f,lineterminator='\n')
    if account:w.writerow(['Account Number',account]);w.writerow(['Bank',bank_name])
    w.writerow(['id','date','amount','currency','counterparty','reference','status','memo'])
    w.writerow([pid,date,amount,currency,payer,reference,status,memo]);return f.getvalue()


def ingest(s,text,file='statement.csv'):
    r=read_bank_csv(text);assert r.ok,r.errors
    return s.ingest(r,file_name=file)


def plan(s):
    p=plan_followups(s.list_invoices(),s.list_customers(),s.verified_matches(),s.chase_state(),DAY,sender='Demo Studio',holds=s.reminder_holds(DAY))
    s.create_drafts(p.all_drafts);s.save_chase_state(p.state);return p


def setup(applied=True):
    s=Store(':memory:',today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example'),Customer('B','Beta Demo',email='b@beta.example'),Customer('C','Gamma Workshop',email='c@gamma.example')])
    s.upsert_invoices([Invoice('INV-'+x+'1',x,dt.date(2026,10,1),dt.date(2026,10,20),D(v),'EUR',D(v)) for x,v in [('A','1200'),('B','300'),('C','700')]])
    ingest(s,bank())
    if applied:
        for m in s.match_result(DAY).applied:s.apply_match(m)
    p=plan(s)
    for d in p.all_drafts:s.approve_draft(d.id,d.hash)
    return s,{d.customer_id:d for d in p.all_drafts}


def resolve(s,decision):
    q=s.receipt_conflicts()[0];s.resolve_receipt_update(q['original'],q['observation'],decision,expected_snapshot=q['snapshot'],by='isolated test')


@pytest.mark.parametrize('change',[dict(amount='600'),dict(date='2026-11-04'),dict(currency='USD'),dict(payer='Beta Demo'),dict(reference='INV-B1'),dict(memo='different invoice evidence')])
def test_receipt_update_conflict_retains_changes_and_revokes_allocated_claims(change):
    s,drafts=setup();history=s.list_matches();before=s.get_payment('P1')
    ingest(s,bank(**change),file='second-account.csv')
    assert s.get_payment('P1')==before and s.list_matches()==history
    assert len(s.list_payments())==2 and s.receipt_conflicts()
    assert not s.verified_matches() and not s.unapplied_payments()
    assert s.approval(drafts['A'].id) is None
    with pytest.raises(ConflictError,match='receipt update conflict'):s.reserve_send(drafts['A'].id,drafts['A'].hash,'eml')
    assert not plan(s).thanks
    assert s.reserve_send(drafts['C'].id,drafts['C'].hash,'eml')


def test_different_unscoped_receipt_is_not_silently_lost_and_actual_payer_is_held():
    s,drafts=setup()
    counts=ingest(s,bank(amount='300',payer='Beta Demo',reference='INV-B1',date='2026-11-04'))
    assert counts['receipt_update_questions']==1 and counts['payments']==1
    assert 'INV-B1' in s.reminder_holds(DAY) and s.approval(drafts['B'].id) is None
    assert not [d for d in plan(s).drafts if d.customer_id=='B']
    assert s.get_invoice('INV-B1').balance==D('300')


@pytest.mark.parametrize('state',['Reverted','Reversed','Returned','Refunded','Pending','Failed','Void'])
def test_adverse_or_unsettled_update_keeps_history_and_suppresses_thanks(state):
    s,drafts=setup();history=s.list_matches()
    r=read_bank_csv(bank(status=state));assert r.ok and not r.records and r.skipped
    s.ingest(r)
    assert s.receipt_conflicts() and s.list_matches()==history and not s.verified_matches()
    assert not plan(s).thanks
    with pytest.raises(ConflictError):s.approve_draft(drafts['A'].id,drafts['A'].hash)


def test_status_only_reversal_can_quarantine_without_a_new_payment_amount():
    s,_=setup()
    r=read_bank_csv('id,date,amount,currency,status\nP1,,,,Reverted\n')
    assert r.ok and not r.records
    s.ingest(r);assert s.receipt_conflicts() and not s.verified_matches()
    assert len(s.list_payments())==1


@pytest.mark.parametrize('account_a,account_b',[('123','456'),('DE89370400440532013000','CZ6508000000192000145399')])
def test_account_scoped_ids_accept_genuine_different_accounts(account_a,account_b):
    with Store(':memory:') as s:
        ingest(s,bank(account=account_a))
        ingest(s,bank(account=account_b,amount='300',payer='Beta Demo',reference='INV-B1'))
        assert len(s.list_payments())==2 and len({p.id for p in s.list_payments()})==2
        assert not s.receipt_conflicts()


def test_same_account_and_id_reimport_is_idempotent_across_filenames():
    with Store(':memory:') as s:
        first=bank(account='123');ingest(s,first,'week.csv')
        second=bank(account='123',status='Booked',payer='ALPHA  DEMO')
        counts=ingest(s,second,'month.csv')
        assert counts['payments']==0 and len(s.list_payments())==1 and not s.receipt_conflicts()
        with pytest.raises(DuplicateImportError):ingest(s,first,'different-name.csv')


def test_legacy_unknown_account_is_not_counted_again_when_scope_appears():
    s,_=setup()
    ingest(s,bank(account='123'))
    assert s.receipt_conflicts() and not s.verified_matches()


def test_legacy_previously_discarded_import_can_be_reobserved_without_deleting_history():
    s,_=setup()
    lost=read_bank_csv(bank(amount='300',payer='Beta Demo',reference='INV-B1'))
    s.record_import(lost)  # emulate old release that recorded but discarded the changed ID
    assert s.has_import(lost.fingerprint)
    counts=s.ingest(lost)
    assert counts['payments']==1 and counts['receipt_update_questions']==1
    with pytest.raises(DuplicateImportError):s.ingest(lost)


def test_verified_distinct_releases_genuine_unscoped_cash_without_reversing_history():
    s,_=setup();history=s.list_matches()
    ingest(s,bank(amount='300',payer='Beta Demo',reference='INV-B1'))
    resolve(s,'distinct')
    assert not s.receipt_conflicts() and s.list_matches()==history
    result=s.match_result(DAY);assert len(result.applied)==1 and result.applied[0].invoice_number=='INV-B1'


def test_unallocated_correction_can_be_verified_and_replaced():
    s,_=setup(applied=False)
    ingest(s,bank(amount='600'))
    q=s.receipt_conflicts()[0];resolve(s,'replace')
    assert not s.receipt_conflicts() and not s.list_matches()
    assert [p.id for p in s.unapplied_payments()]==[q['observation']]
    with pytest.raises(ConflictError):s.apply_by_person('P1','INV-A1',D('600'))
    ingest(s,bank(amount='600',status='Booked'))
    assert not s.receipt_conflicts()


def test_allocated_correction_never_automatically_reverses_matches():
    s,_=setup();history=s.list_matches()
    ingest(s,bank(amount='600'))
    with pytest.raises(ConflictError,match='no automatic undo'):resolve(s,'replace')
    assert s.list_matches()==history and s.receipt_conflicts()


def test_verified_keep_requires_fresh_text_and_approval_for_unsent_acknowledgement():
    s,drafts=setup();ingest(s,bank(status='Reverted'))
    resolve(s,'keep')
    assert s.approval(drafts['A'].id) is None
    p=plan(s);d=next(d for d in p.thanks if d.customer_id=='A')
    s.approve_draft(d.id,d.hash);assert s.reserve_send(d.id,d.hash,'eml')


def test_stale_resolution_token_cannot_verify_changed_evidence():
    s,_=setup();ingest(s,bank(amount='600'));q=s.receipt_conflicts()[0]
    ingest(s,bank(amount='500'))
    with pytest.raises(ConflictError,match='receipt update snapshot changed'):
        s.resolve_receipt_update(q['original'],q['observation'],'keep',expected_snapshot=q['snapshot'])
    assert s.receipt_conflicts()


def test_adverse_status_cannot_be_declared_distinct_cash():
    s,_=setup();ingest(s,bank(status='Reverted'))
    with pytest.raises(ConflictError,match='adverse status cannot create separate cash'):resolve(s,'distinct')


def test_earlier_terminal_observation_blocks_a_later_stale_booked_row():
    with Store(':memory:') as s:
        ingest(s,bank(status='Reverted'))
        assert not s.list_payments()
        ingest(s,bank())
        assert s.receipt_conflicts() and not s.unapplied_payments()


def test_initial_pending_then_booked_remains_usable():
    with Store(':memory:') as s:
        ingest(s,bank(status='Pending'));ingest(s,bank())
        assert not s.receipt_conflicts() and len(s.unapplied_payments())==1


def test_conflict_is_persistent_across_reopening(tmp_path):
    path=tmp_path/'ledger.sqlite'
    with Store(path) as s:ingest(s,bank());ingest(s,bank(status='Reverted'))
    with Store(path) as s:assert s.receipt_conflicts() and not s.unapplied_payments()


def test_direct_add_refuses_to_silently_overwrite_an_explicit_id():
    with Store(':memory:') as s:
        s.add_payments([Payment('P1',DAY,D('100'),'EUR')])
        with pytest.raises(ConflictError,match='receipt update conflict'):s.add_payments([Payment('P1',DAY,D('200'),'EUR')])
        assert s.get_payment('P1').amount==D('100')


def test_same_local_id_in_two_account_rows_of_one_file_is_not_discarded():
    text='id,date,amount,currency,counterparty,reference,Account Number\n1,2026-11-03,1200,EUR,Alpha Demo,INV-A1,123\n1,2026-11-03,300,EUR,Beta Demo,INV-B1,456\n'
    r=read_bank_csv(text);assert r.ok and len(r.records)==2
    with Store(':memory:') as s:
        assert s.ingest(r)['payments']==2 and not s.receipt_conflicts()


def test_known_distinct_accounts_do_not_generate_derived_overlap_questions():
    with Store(':memory:') as s:
        ingest(s,bank(pid='',account='123'))
        ingest(s,bank(pid='',account='456'))
        assert len(s.list_payments())==2 and not s.payment_overlaps() and not s.receipt_conflicts()


def test_bank_and_account_label_aliases_keep_scope_stable():
    with Store(':memory:') as s:
        ingest(s,bank(account='123'))
        text=bank(account='123',status='Booked').replace('Account Number,','Account No,').replace('Bank,','Bank Name,')
        counts=ingest(s,text)
        assert counts['payments']==0 and not s.receipt_conflicts()


def test_conflicting_statement_and_row_accounts_refuse_instead_of_guessing():
    text='Account Number,123\nid,date,amount,currency,Account Number\nP1,2026-11-03,100,EUR,456\n'
    r=read_bank_csv(text)
    assert not r.ok and 'bank account evidence conflicts' in r.errors[0].message


def test_sent_acknowledgement_history_is_never_reset_by_later_reversal():
    s,drafts=setup();d=drafts['A']
    key=s.reserve_send(d.id,d.hash,'eml');s.confirm_send(key)
    state=s.chase_state().thanked.copy();history=s.list_sends()
    ingest(s,bank(status='Reverted'))
    assert s.list_sends()==history and s.get_draft(d.id).status=='sent'
    resolve(s,'keep')
    assert s.chase_state().thanked==state and not plan(s).thanks


def test_approval_is_revoked_after_ledger_reopen_with_receipt_question(tmp_path):
    path=tmp_path/'ledger.sqlite'
    with Store(path,today=lambda:DAY) as s:
        s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example')])
        s.upsert_invoices([Invoice('INV-A1','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
        d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
        ingest(s,bank());ingest(s,bank(amount='600'))
    with Store(path,today=lambda:DAY) as s:
        assert s.approval(d.id) is None and s.receipt_conflicts()
        with pytest.raises(ConflictError):s.reserve_send(d.id,d.hash,'eml')


def test_nonbooked_state_never_becomes_spendable_cash_after_distinct_attempt():
    s,_=setup();ingest(s,bank(status='Reverted'))
    with pytest.raises(ConflictError):resolve(s,'distinct')
    assert not s.unapplied_payments() and s.receipt_conflicts()


def test_account_metadata_change_does_not_silently_double_count():
    with Store(':memory:') as s:
        ingest(s,bank(account='123'))
        ingest(s,bank(account='123').replace('Bank,Demo Bank\n',''))
        assert s.receipt_conflicts() and not s.unapplied_payments()


def test_repeated_unallocated_corrections_follow_the_current_verified_version():
    s,_=setup(applied=False)
    ingest(s,bank(amount='600'));resolve(s,'replace')
    ingest(s,bank(amount='500'));resolve(s,'replace')
    ingest(s,bank(amount='500',status='Booked'))
    assert not s.receipt_conflicts()
    assert len(s.unapplied_payments())==1 and s.unapplied_payments()[0].amount==D('500')


def test_replacement_back_to_prior_amount_keeps_version_history_acyclic():
    s,_=setup(applied=False)
    ingest(s,bank(amount='600'));resolve(s,'replace')
    ingest(s,bank(amount='500'));resolve(s,'replace')
    ingest(s,bank(amount='600',status='Booked'));resolve(s,'replace')
    assert len(s.unapplied_payments())==1 and s.unapplied_payments()[0].amount==D('600')
    assert len(s.list_payments())==4


def test_unqualified_row_iban_is_not_assumed_to_be_the_statement_account():
    first='id,date,amount,currency,counterparty,reference,IBAN\nP1,2026-11-03,1200,EUR,Alpha Demo,INV-A1,DE89370400440532013000\n'
    second='id,date,amount,currency,counterparty,reference,IBAN\nP1,2026-11-04,300,EUR,Beta Demo,INV-B1,CZ6508000000192000145399\n'
    with Store(':memory:') as s:
        ingest(s,first);ingest(s,second)
        assert s.receipt_conflicts() and not s.unapplied_payments()


def test_explicit_own_iban_provides_statement_account_scope():
    first='id,date,amount,currency,Own IBAN\nP1,2026-11-03,100,EUR,DE89370400440532013000\n'
    second=first.replace('DE89370400440532013000','CZ6508000000192000145399')
    with Store(':memory:') as s:
        ingest(s,first);ingest(s,second)
        assert len(s.list_payments())==2 and not s.receipt_conflicts()


def test_verified_legacy_scope_adoption_keeps_later_status_bound_to_original_allocation():
    s,_=setup();history=s.list_matches()
    ingest(s,bank(account='123'))
    resolve(s,'keep')
    assert s.verified_matches()==history
    ingest(s,bank(account='123',status='Reverted'))
    assert s.receipt_conflicts() and not s.verified_matches()
    assert s.list_matches()==history and not plan(s).thanks


def test_scoped_receipt_is_quarantined_when_later_reversal_omits_account_metadata():
    with Store(':memory:') as s:
        ingest(s,bank(account='123'))
        ingest(s,bank(status='Reverted'))
        assert s.receipt_conflicts() and not s.unapplied_payments()
        assert any('Reverted' in str(q['evidence']) for q in s.receipt_conflicts())


def test_booked_reimport_losing_account_scope_is_not_new_certain_cash():
    with Store(':memory:') as s:
        ingest(s,bank(account='123'))
        ingest(s,bank())
        assert s.receipt_conflicts() and not s.unapplied_payments()


def test_unscoped_terminal_evidence_prevents_scoped_stale_booked_import():
    with Store(':memory:') as s:
        ingest(s,bank(status='Reverted'))
        ingest(s,bank(account='123'))
        assert s.receipt_conflicts() and not s.unapplied_payments()
        assert any('Reverted' in str(q['evidence']) for q in s.receipt_conflicts())


def test_keep_cannot_discard_an_observation_with_historical_allocations():
    s,_=setup();ingest(s,bank(amount='300',payer='Beta Demo',reference='INV-B1'))
    q=s.receipt_conflicts()[0]
    # Model a restored historical allocation, not a permitted current Apply action.
    s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',(q['observation'],'INV-B1','300','EUR','restored','verified','historical evidence','2026-11-03T00:00:00+00:00'))
    history=s.list_matches()
    with pytest.raises(ConflictError,match='allocated observation requires verified allocation reconciliation'):resolve(s,'keep')
    assert s.list_matches()==history and s.receipt_conflicts()


def test_update_through_confirmed_alias_exposes_and_protects_canonical_allocations():
    from dataclasses import replace
    s,_=setup();s.add_payments([replace(s.get_payment('P1'),id='LEGACY')])
    s._conn.execute('INSERT INTO receipt_aliases VALUES (?,?,?)',('LEGACY','P1','2026-11-03T00:00:00+00:00'))
    ingest(s,bank(pid='LEGACY',amount='600'))
    q=s.receipt_conflicts()[0]
    assert any(m['payment_id']=='P1' for m in q['allocations'])
    assert not s.verified_matches()
    assert any(i.kind=='RECEIPT_UPDATE' and i.payment.id=='P1' for i in s.match_result(DAY).review)
