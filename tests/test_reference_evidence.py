"""Exact reference proof includes boundaries, unknown IDs and identifier roles."""
import datetime as dt
from dataclasses import replace
from decimal import Decimal as D

import pytest

from cashdesk import Customer,Invoice,Payment,Store,match_payments
from cashdesk.matching import invoices_named
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError
from tests.test_receipt_lifecycle import setup,plan,DAY


CUSTOMER=Customer('A','Alpha Demo',email='a@alpha.example')
INV=Invoice('INV-1001','A',dt.date(2026,10,1),dt.date(2026,10,20),D('1200'),'EUR',D('1200'),reference='20261001')


def payment(reference,memo=''):
    return Payment('P1',dt.date(2026,11,3),D('1200'),'EUR',reference,'Alpha Demo',memo)


@pytest.mark.parametrize('text',['ORDERX20261001Z','X20261001','20261001Z','Ж20261001Я','X_INV-1001_Z'])
def test_invoice_and_variable_symbol_match_only_whole_unicode_tokens(text):
    assert invoices_named(text,[INV])==[]
    result=match_payments([INV],[payment(text)],[CUSTOMER],DAY)
    assert not result.applied and 'INV-1001' in result.held_invoices


@pytest.mark.parametrize('reference',['INV   1001','inv___1001','INV///1001','inv1001','INV - 1001',
                                     'VS20261001','VS 2026 1001','Payment reference: 2026-1001','20261001'])
def test_supported_reference_separators_and_bank_prefixes_remain_exact(reference):
    result=match_payments([INV],[payment(reference)],[CUSTOMER],DAY)
    assert len(result.applied)==1 and result.applied[0].invoice_number=='INV-1001'


@pytest.mark.parametrize('reference',['INV-1001 and INV-9999','INV-9999 / INV-1001','INV-1001 VS99999999',
                                     'INV-1001 Invoice UK-9999','INV-1001 FA-9999'])
def test_unknown_additional_invoice_identifiers_are_not_silently_ignored(reference):
    result=match_payments([INV],[payment(reference)],[CUSTOMER],DAY)
    assert not result.applied
    item=result.review[0]
    assert item.kind=='REFERENCE' and 'reference evidence needs review' in item.detail
    assert item.unknown_references and 'INV-1001' in item.holds


@pytest.mark.parametrize('reference,memo',[('Order 1047','Deposit for next project'),('Project #1047',''),
                                        ('PO 1047',''),('Transaction 1047',''),('1047','Advance for a new project'),
                                        ('Objednávka 1047',''),('INV-1001','Deposit for next project')])
def test_order_project_and_advance_evidence_cannot_authorize_invoice_allocation(reference,memo):
    invoice=replace(INV,number='1047',reference='') if '1047' in reference else INV
    result=match_payments([invoice],[payment(reference,memo)],[CUSTOMER],DAY)
    assert not result.applied and result.review[0].kind=='REFERENCE'


@pytest.mark.parametrize('reference',['1047','Invoice 1047','INV1047','Payment ref: 1047'])
def test_exact_numeric_invoice_proof_remains_usable(reference):
    invoice=replace(INV,number='1047',reference='')
    assert match_payments([invoice],[payment(reference)],[CUSTOMER],DAY).applied


def test_unknown_refs_do_not_hide_known_candidates_beyond_three():
    invoices=[replace(INV,number=f'INV-{i}',reference='',customer_id=str(i)) for i in range(6)]
    result=match_payments(invoices,[payment('INV-0 INV-1 INV-2 INV-3 INV-4 INV-5 INV-9999')],[CUSTOMER],DAY)
    assert not result.applied and result.review[0].kind=='REFERENCE'
    assert set(result.review[0].named)=={f'INV-{i}' for i in range(6)}
    assert 'INV-9999' in result.review[0].unknown_references


def test_reference_review_revokes_collection_and_can_be_deliberately_placed():
    s=Store(':memory:',today=lambda:DAY);s.upsert_customers([CUSTOMER]);s.upsert_invoices([INV])
    d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
    result=read_bank_csv('id,date,amount,currency,counterparty,reference\nP1,2026-11-03,1200,EUR,Alpha Demo,INV-1001 and INV-9999\n')
    s.ingest(result)
    assert not s.match_result(DAY).applied and s.approval(d.id) is None
    with pytest.raises(ConflictError):s.reserve_send(d.id,d.hash,'eml')
    s.apply_by_person('P1','INV-1001',by='verified bank allocation')
    assert s.get_invoice('INV-1001').balance==0 and plan(s).thanks


def test_preexisting_automatic_allocation_with_bad_reference_is_held_without_undo(tmp_path):
    from cashdesk import Match
    path=tmp_path/'scratch.sqlite'
    s=Store(path,today=lambda:DAY);s.upsert_customers([CUSTOMER]);s.upsert_invoices([INV]);s.add_payments([payment('INV-1001 INV-9999')])
    # Model an older automatic allocation. Current write guards must reject a
    # newly requested automatic match, so migration is tested with retained SQL.
    s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',('P1','INV-1001','1200.00','EUR','reference+exact','high','old automatic reference choice','2026-11-03T12:00:00+00:00'))
    s._conn.execute("UPDATE invoices SET balance='0.00',status='paid' WHERE number='INV-1001'")
    before=s.list_matches();s.close()
    with Store(path,today=lambda:DAY) as s:
        assert s.list_matches()==before and s.get_invoice('INV-1001').balance==0
        assert s.bank_evidence_questions() and not s.verified_matches() and not plan(s).thanks
        q=s.bank_evidence_questions()[0]
        assert q['evidence']['kind']=='reference' and q['evidence']['unknown_references']
        with pytest.raises(ConflictError,match='reference evidence needs review'):
            s.confirm_bank_evidence(q['id'],expected_snapshot='stale',reason='Bank checked',by='test')
        s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Original bank remittance and current allocation independently verified',by='test')
        assert not s.bank_evidence_questions() and s.list_matches()==before and plan(s).thanks
    with Store(path,today=lambda:DAY) as s:assert not s.bank_evidence_questions()


@pytest.mark.parametrize('text',['A\u030120261001','20261001\u0301A','INV-1001\u0301'])
def test_combining_unicode_marks_are_part_of_identifier_boundaries(text):
    assert invoices_named(text,[INV])==[]
    assert not match_payments([INV],[payment(text)],[CUSTOMER],DAY).applied


@pytest.mark.parametrize('text',['REF_20261001','Reference:20261001','Payment_reference_20261001'])
def test_explicit_bank_reference_labels_remain_usable(text):
    assert match_payments([INV],[payment(text)],[CUSTOMER],DAY).applied


@pytest.mark.parametrize('text',['INV-1001 INV-ABCD','INV-1001 Invoice #ABCD','INV-1001 UK-123AB','INV-1001 UK-ABCD'])
def test_alphanumeric_unknown_invoice_codes_are_preserved(text):
    result=match_payments([INV],[payment(text)],[CUSTOMER],DAY)
    assert not result.applied and result.review[0].unknown_references


@pytest.mark.parametrize('receipt_exists',[True,False])
def test_automatic_write_requires_reference_proof_even_for_an_old_proposal(receipt_exists):
    from cashdesk import Match
    s=Store(':memory:',today=lambda:DAY);s.upsert_customers([CUSTOMER]);s.upsert_invoices([INV])
    if receipt_exists:s.add_payments([payment('INV-1001 INV-9999')])
    with pytest.raises(ConflictError,match='reference evidence needs review'):
        s.apply_match(Match('P1','INV-1001',D('1200'),'EUR','reference+exact','high'))
    assert not s.list_matches() and s.get_invoice('INV-1001').balance==D('1200')


def test_transaction_id_does_not_supply_invoice_reference_proof():
    result=match_payments([INV],[replace(payment(''),id='INV-1001')],[CUSTOMER],DAY)
    assert not result.applied


def test_numeric_reference_in_unrelated_free_text_is_not_verified_identity():
    result=match_payments([INV],[payment('ORDERX 20261001 Z')],[CUSTOMER],DAY)
    assert not result.applied and result.review[0].kind=='REFERENCE'


@pytest.mark.parametrize('sent',[False,True])
def test_old_automatic_thanks_loses_authority_but_sent_history_is_immutable(tmp_path,sent):
    from cashdesk import plan_followups
    path=tmp_path/'scratch.sqlite';s=Store(path,today=lambda:DAY)
    s.upsert_customers([CUSTOMER]);s.upsert_invoices([INV]);s.add_payments([payment('INV-1001 INV-9999')])
    s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',('P1','INV-1001','1200.00','EUR','reference+exact','high','old choice','2026-11-03T12:00:00+00:00'))
    s._conn.execute("UPDATE invoices SET balance='0.00',status='paid' WHERE number='INV-1001'")
    old=plan_followups(s.list_invoices(),s.list_customers(),s.list_matches(),s.chase_state(),DAY,sender='Demo Studio')
    d=old.thanks[0];s.create_drafts(old.all_drafts);s.save_chase_state(old.state)
    s._conn.execute('INSERT INTO approvals VALUES (?,?,?,?)',(d.id,d.hash,'2026-11-03T12:00:00+00:00','legacy fake approval'))
    s._conn.execute('UPDATE drafts SET status=? WHERE id=?',('sent' if sent else 'approved',d.id))
    if sent:
        s._conn.execute('INSERT INTO sends VALUES (?,?,?,?,?,?,?,?)',('old-send',d.id,d.hash,'2026-11-03T12:00:00+00:00','2026-11-03T12:00:00+00:00','eml','sent','retained fake history'))
    history=s.list_sends();matches=s.list_matches();s.close()
    with Store(path,today=lambda:DAY) as s:
        assert s.list_sends()==history and s.list_matches()==matches and s.get_invoice('INV-1001').balance==0
        assert s.bank_evidence_questions() and not s.verified_matches() and not plan(s).thanks
        if sent:assert s.get_draft(d.id).status=='sent'
        else:
            assert s.approval(d.id) is None
            with pytest.raises(ConflictError,match='reference evidence needs review'):s.reserve_send(d.id,d.hash,'eml')


def test_material_new_reference_target_requires_new_verification():
    s=Store(':memory:',today=lambda:DAY);s.upsert_customers([CUSTOMER]);s.upsert_invoices([INV]);s.add_payments([payment('INV-1001 INV-9999')])
    s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',('P1','INV-1001','1200.00','EUR','reference+exact','high','old choice','2026-11-03T12:00:00+00:00'))
    s._conn.execute("UPDATE invoices SET balance='0.00',status='paid' WHERE number='INV-1001'")
    s.refresh_reminders();q=s.bank_evidence_questions()[0]
    s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Original bank and current allocation independently verified',by='test')
    assert not s.bank_evidence_questions()
    s.upsert_invoices([replace(INV,number='INV-9999',reference='',amount=D('20'),balance=D('20'))])
    assert s.bank_evidence_questions() and not s.verified_matches()


@pytest.mark.parametrize('code,target',[('FA-Č123','FA-Č123'),('Invoice #Č123','Č123'),
                                      ('FA-C\u030c123','FA-Č123'),('FA-ק\u05b0123','FA-ק\u05b0123')])
def test_unknown_unicode_invoice_codes_are_retained_in_full(code,target):
    result=match_payments([INV],[payment('INV-1001 '+code)],[CUSTOMER],DAY)
    assert not result.applied and target in result.review[0].unknown_references


def test_canonically_equivalent_unicode_invoice_identifiers_remain_usable():
    invoice=replace(INV,number='FA-C\u030c123',reference='')
    assert match_payments([invoice],[payment('FA-Č123')],[CUSTOMER],DAY).applied
