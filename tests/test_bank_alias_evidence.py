"""Conflicting aliases never become certain cash or preserve collection authority."""
import datetime as dt
import json
from decimal import Decimal as D

import pytest

from cashdesk import Customer, Invoice, Store
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError
from tests.test_receipt_lifecycle import setup, plan, DAY


def statement(payer='Beta Demo', reference='INV-B1', *, headers='Payer Name,Payment Reference',
              status='Completed', pid='P1'):
    return (f'id,date,amount,currency,Counterparty,Reference,{headers},Status\n'
            f'{pid},2026-11-03,1200,EUR,Alpha Demo,INV-A1,{payer},{reference},{status}\n')


@pytest.mark.parametrize('payer,reference', [('Beta Demo','INV-A1'), ('Alpha Demo','INV-B1'),
                                         ('Beta Demo','INV-B1'), ('Ltd','INV-A1')])
@pytest.mark.parametrize('status', ['Completed','Returned','Pending'])
def test_conflicting_bank_aliases_retain_all_evidence_before_identity(payer,reference,status):
    result=read_bank_csv(statement(payer,reference,status=status))
    assert not result.records and not result.bank_observations
    assert result.errors and result.errors[0].code=='bank_alias_conflict'
    assert 'bank payer/reference evidence conflicts' in result.errors[0].message
    evidence=result.bank_alias_issues[0]
    assert payer in evidence.raw and reference in evidence.raw
    values=json.loads(evidence.evidence)
    assert values['payers']==['Alpha Demo',payer] and values['references']==['INV-A1',reference]


@pytest.mark.parametrize('headers', ['Counterparty,Reference','counter_party,reference','Payer Name,Payment Reference'])
@pytest.mark.parametrize('reverse', [False,True])
def test_duplicate_normalized_identity_columns_are_reconciled_without_mapping_loss(headers,reverse):
    text=statement(headers=headers)
    if reverse:
        import csv,io
        rows=list(csv.reader(io.StringIO(text)))
        buf=io.StringIO();csv.writer(buf).writerows([list(reversed(row)) for row in rows]);text=buf.getvalue()
    result=read_bank_csv(text)
    assert not result.records and result.errors[0].code=='bank_alias_conflict'
    assert 'Beta Demo' in result.bank_alias_issues[0].raw


@pytest.mark.parametrize('payer,reference', [('ALPHA DEMO LTD','INV A1'),('Alpha Démo','INV-A1'),('',''),('Alpha Demo','INV-A1')])
@pytest.mark.parametrize('headers', ['Payer Name,Payment Reference','Counterparty,Reference'])
def test_equivalent_and_empty_aliases_keep_supported_cash(payer,reference,headers):
    result=read_bank_csv(statement(payer,reference,headers=headers))
    assert result.ok and len(result.records)==1 and not result.bank_alias_issues
    assert result.records[0].counterparty=='Alpha Demo' and result.records[0].reference=='INV-A1'


def test_transaction_id_is_not_invoice_reference_or_payer_from_memo():
    result=read_bank_csv('Bank Reference,Date,Amount,Currency,Description\nINV-A1,2026-11-03,1200,EUR,Alpha Demo\n')
    assert result.ok and result.records[0].id=='INV-A1'
    assert not result.records[0].reference and not result.records[0].counterparty


@pytest.mark.parametrize('first,second,ok', [('Acme Ltd','ACME LIMITED',True),('Acme Ltd','Acme GmbH',False),
                                         ('Acme GmbH','Acme Inc',False),('小林商事','小林商事',True),('小林商事','小森商事',False)])
def test_payer_alias_equivalence_does_not_erase_explicit_legal_or_unicode_differences(first,second,ok):
    text=f'date,amount,counterparty,payer name,reference\n2026-11-03,1200,{first},{second},INV-A1\n'
    result=read_bank_csv(text)
    assert result.ok is ok
    if not ok:assert result.errors[0].code=='bank_alias_conflict'


@pytest.mark.parametrize('payer,reference', [('Beta Demo','INV-A1'),('Alpha Demo','INV-B1')])
def test_partial_alias_questions_revoke_thanks_and_collection_without_financial_undo(payer,reference):
    s,drafts=setup();history=s.list_matches();invoices=s.list_invoices();payments=s.list_payments()
    result=read_bank_csv(statement(payer,reference))
    with pytest.raises(ConflictError):s.ingest(result)
    assert not s.bank_evidence_questions()  # strict rejection wrote nothing
    counts=s.ingest(result,allow_errors=True,file_name='bank.csv')
    assert counts['bank_evidence_questions']==1 and counts['payments']==0
    assert s.list_matches()==history and s.list_invoices()==invoices and s.list_payments()==payments
    assert all(s.approval(d.id) is None for d in drafts.values())
    assert not s.verified_matches() and not plan(s).thanks and not plan(s).drafts
    for d in drafts.values():
        with pytest.raises(ConflictError,match='bank payer/reference evidence conflicts'):s.approve_draft(d.id,d.hash)
        with pytest.raises(ConflictError,match='bank payer/reference evidence conflicts'):s.reserve_send(d.id,d.hash,'eml')
    q=s.bank_evidence_questions()[0]
    assert 'INV-A1' in q['named'] and q['raw'] and q['file']=='bank.csv'


def test_mixed_partial_import_holds_new_cash_and_future_invoices_until_human_verification(tmp_path):
    path=tmp_path/'scratch.sqlite';s=Store(path,today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Demo',email='a@alpha.example')])
    s.upsert_invoices([Invoice('INV-A1','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    text=statement()+statement('Alpha Demo','INV-A1',pid='P2').split('\n')[1]+'\n'
    s.ingest(read_bank_csv(text),allow_errors=True)
    assert len(s.list_payments())==1 and not s.match_result(DAY).applied
    with pytest.raises(ConflictError,match='bank payer/reference evidence conflicts'):s.apply_by_person('P2','INV-A1')
    q=s.bank_evidence_questions()[0];s.close()
    with Store(path,today=lambda:DAY) as s:
        assert s.bank_evidence_questions()[0]['id']==q['id']
        s.upsert_invoices([Invoice('INV-A2','A',dt.date(2026,10,1),dt.date(2026,10,20),D('100'),'EUR',D('100'))])
        assert {'INV-A1','INV-A2'}<=set(s.reminder_holds(DAY))
        with pytest.raises(ConflictError,match='bank payer/reference evidence conflicts'):
            s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Verified statement',by='test')


def test_bank_evidence_recovery_is_explicit_snapshot_bound_and_preserves_sent_history():
    s,drafts=setup();d=drafts['A'];token=s.reserve_send(d.id,d.hash,'eml');s.confirm_send(token)
    sent=s.list_sends();history=s.list_matches()
    s.ingest(read_bank_csv(statement()),allow_errors=True)
    q=s.bank_evidence_questions()[0]
    for snapshot,reason,by in [('stale','Verified original bank','test'),(q['snapshot'],'','test'),(q['snapshot'],'Verified original bank','')]:
        with pytest.raises(ConflictError,match='bank payer/reference evidence conflicts'):
            s.confirm_bank_evidence(q['id'],expected_snapshot=snapshot,reason=reason,by=by)
    s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Verified original bank: original receipt settled; conflicting row is not additional cash',by='test')
    assert not s.bank_evidence_questions() and s.list_sends()==sent and s.list_matches()==history
    assert s.get_draft(d.id).status=='sent' and not plan(s).thanks
    assert s.approval(drafts['B'].id) is None


def test_alias_evidence_is_not_silently_resolved_by_a_later_corrected_export():
    s,_=setup();s.ingest(read_bank_csv(statement()),allow_errors=True)
    s.ingest(read_bank_csv(statement('Alpha Demo','INV-A1')))
    assert s.bank_evidence_questions() and not s.verified_matches()


def test_all_named_documents_and_payer_candidates_survive_beyond_three():
    s=Store(':memory:',today=lambda:DAY)
    for i in range(6):
        s.upsert_customers([Customer(str(i),f'Company {i}',email=f'ap@company{i}.example')])
        s.upsert_invoices([Invoice(f'INV-{i}',str(i),dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'))])
    text='date,amount,counterparty,payer name,reference,payment reference\n2026-11-03,1200,Company 0,Company 5,INV-0 INV-1 INV-2,INV-3 INV-4 INV-5\n'
    s.ingest(read_bank_csv(text),allow_errors=True)
    q=s.bank_evidence_questions()[0]
    assert set(q['named'])=={f'INV-{i}' for i in range(6)} and {'0','5'}<=set(q['customer_ids'])
    assert len(s.reminder_holds(DAY))==6 and not s.list_payments()


@pytest.mark.parametrize('headers', ['sender,remittance information','název protiúčtu,variabilní symbol',
                                   'auftraggeber empfänger,verwendungszweck','tegenpartij,betalingskenmerk'])
def test_translated_recognized_payer_and_reference_evidence_cannot_override_other_aliases(headers):
    result=read_bank_csv(statement(headers=headers))
    assert result.errors[0].code=='bank_alias_conflict' and not result.records


@pytest.mark.parametrize('bad_field', ['amount','date'])
def test_other_row_errors_cannot_discard_alias_uncertainty_in_partial_import(bad_field):
    text=statement().replace('1200','bad') if bad_field=='amount' else statement().replace('2026-11-03','not a date')
    result=read_bank_csv(text)
    s=Store(':memory:',today=lambda:DAY)
    s.ingest(result,allow_errors=True)
    assert s.bank_evidence_questions() and not s.list_payments()


def test_bank_preamble_and_original_duplicate_cells_remain_auditable():
    result=read_bank_csv('Account Number,123\nBank,Demo Bank\n'+statement(headers='Counterparty,Reference'))
    s=Store(':memory:',today=lambda:DAY);s.ingest(result,allow_errors=True)
    evidence=s.bank_evidence_questions()[0]['evidence']
    assert evidence['preamble']==['Account Number,123','Bank,Demo Bank']
    assert evidence['headers'].count('Counterparty')==2
    assert ['counterparty','Alpha Demo'] in evidence['cells'] and ['counterparty','Beta Demo'] in evidence['cells']


def test_each_pending_question_requires_a_fresh_snapshot_and_never_restores_approval():
    s,drafts=setup()
    s.ingest(read_bank_csv(statement()),allow_errors=True)
    s.ingest(read_bank_csv(statement(pid='OTHER')),allow_errors=True)
    questions=s.bank_evidence_questions();first,second=questions
    s.confirm_bank_evidence(first['id'],expected_snapshot=first['snapshot'],reason='Original bank and ledger verified',by='test')
    assert len(s.bank_evidence_questions())==1 and not s.verified_matches()
    with pytest.raises(ConflictError,match='bank payer/reference evidence conflicts'):
        s.confirm_bank_evidence(second['id'],expected_snapshot=second['snapshot'],reason='Original bank and ledger verified',by='test')
    second=s.bank_evidence_questions()[0]
    s.confirm_bank_evidence(second['id'],expected_snapshot=second['snapshot'],reason='Original bank and ledger verified',by='test')
    assert not s.bank_evidence_questions() and s.verified_matches()
    assert all(s.approval(d.id) is None for d in drafts.values())
    fresh=plan(s);assert fresh.thanks and fresh.drafts


def test_pure_planner_suppresses_thanks_under_explicit_bank_evidence_holds():
    from cashdesk import plan_followups
    s,_=setup()
    p=plan_followups(s.list_invoices(),s.list_customers(),s.list_matches(),s.chase_state().__class__(),DAY,
                     sender='Demo',holds={'INV-A1':'bank_evidence: bank payer/reference evidence conflicts'})
    assert not p.thanks


def test_reobserving_preexisting_import_retains_newly_detected_alias_questions():
    from dataclasses import replace
    s,drafts=setup()
    bad=read_bank_csv(statement())
    # Model the prior reader's first-alias choice, with the original file's
    # fingerprint and existing bank observation marker. No history is deleted.
    old=read_bank_csv('id,date,amount,currency,counterparty,reference\nP1,2026-11-03,1200,EUR,Alpha Demo,INV-A1\n')
    s.ingest(replace(old,fingerprint=bad.fingerprint))
    before=s.list_payments();history=s.list_matches()
    assert history and all(s.approval(d.id) for d in drafts.values())
    counts=s.ingest(bad,allow_errors=True)
    assert counts['bank_evidence_questions']==1
    assert s.list_payments()==before and s.list_matches()==history
    assert all(s.approval(d.id) is None for d in drafts.values())
    assert not s.match_result(DAY).applied
