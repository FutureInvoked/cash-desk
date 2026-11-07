"""Explicit contradictory legal identity cannot authorize automatic cash."""
from dataclasses import replace
import pytest
from cashdesk import Customer,Match,Store,plan_followups
from cashdesk.matching import payer_fits,match_payments
from cashdesk.store import ConflictError
from tests.test_payer_identity import invoice,payment,DAY
from tests.test_receipt_lifecycle import plan

BAD=[('Acme Ltd','Acme GmbH'),('Acme LLC','Acme Ltd'),('Acme Inc','Acme LLC'),
 ('Acme PLC','Acme Limited'),('Acme AG','Acme GmbH'),('Acme KG','Acme GmbH & Co KG'),
 ('Acme s.r.o.','Acme a.s.'),('Acme B.V.','Acme Oy'),('Acme LLP','Acme LLC')]
GOOD=[('Acme Limited','ACME LTD.'),('Acme LLC','Acme L.L.C.'),('Acme Incorporated','Acme Inc.'),
 ('Nová Ukázka spol. s r.o.','NOVA UKAZKA SRO'),('Acme GmbH & Co. KG','ACME GMBH & CO KG'),
 ('Acme B.V.','Acme BV'),('Müller GmbH','MULLER'),('北京测试有限公司','北京测试有限公司')]

@pytest.mark.parametrize('name,payer',BAD)
def test_contradictory_legal_forms_require_payer_review(name,payer):
 assert not payer_fits(name,payer)
 r=match_payments([invoice()],[payment(payer)],[Customer('C1',name)],DAY)
 assert not r.applied and r.review[0].kind=='PAYER' and 'INV-1001' in r.held_invoices

@pytest.mark.parametrize('name,payer',GOOD)
def test_compatible_legal_abbreviations_unicode_and_omitted_forms_remain_usable(name,payer):
 assert payer_fits(name,payer)
 assert match_payments([invoice()],[payment(payer)],[Customer('C1',name)],DAY).applied

@pytest.mark.parametrize('name,payer',BAD[:3])
def test_bank_import_conflicting_entity_revokes_reminder_but_human_can_place(name,payer):
 from cashdesk.importers import read_bank_csv
 s=Store(':memory:',today=lambda:DAY);s.upsert_customers([Customer('C1',name,email='ap@acme.example')]);s.upsert_invoices([invoice()])
 d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
 r=read_bank_csv(f'id,date,amount,currency,counterparty,reference\nP1,2026-11-03,100,EUR,{payer},INV-1001\n');assert r.ok;s.ingest(r)
 assert not s.match_result(DAY).applied and not s.list_matches() and s.get_invoice('INV-1001').balance==100
 assert s.approval(d.id) is None
 with pytest.raises(ConflictError):s.approve_draft(d.id,d.hash)
 with pytest.raises(ConflictError):s.reserve_send(d.id,d.hash,'eml')
 assert not plan(s).thanks and not plan(s).drafts
 s.apply_by_person('P1','INV-1001',by='Verified bank authority and customer relationship')
 assert s.get_invoice('INV-1001').balance==0 and s.list_matches()[0].method=='person' and plan(s).thanks


def test_automatic_write_rechecks_payer_legal_identity_after_proposal():
 s=Store(':memory:',today=lambda:DAY);s.upsert_customers([Customer('C1','Acme Ltd')]);s.upsert_invoices([invoice()]);s.add_payments([payment('Acme Ltd')])
 proposed=s.match_result(DAY).applied[0]
 s.upsert_customers([Customer('C1','Acme GmbH')])
 with pytest.raises(ConflictError,match='payer legal identity needs review'):s.apply_match(proposed)
 assert not s.list_matches() and s.get_invoice('INV-1001').balance==100

@pytest.mark.parametrize('sent',[False,True])
def test_old_automatic_entity_conflict_pauses_authority_without_financial_undo(tmp_path,sent):
 path=tmp_path/'ledger.sqlite';s=Store(path,today=lambda:DAY);s.upsert_customers([Customer('C1','Acme Ltd',email='ap@acme.example')]);s.upsert_invoices([invoice()]);s.add_payments([payment('Acme GmbH')])
 s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',('P1','INV-1001','100.00','EUR','reference+exact','high','legacy automatic match','2026-11-03T12:00:00+00:00'))
 s._conn.execute("UPDATE invoices SET balance='0.00',status='paid' WHERE number='INV-1001'")
 old=plan_followups(s.list_invoices(),s.list_customers(),s.list_matches(),s.chase_state(),DAY,sender='Demo Studio');d=old.thanks[0];s.create_drafts(old.all_drafts);s.save_chase_state(old.state)
 s._conn.execute('INSERT INTO approvals VALUES (?,?,?,?)',(d.id,d.hash,'2026-11-03T12:00:00+00:00','legacy fake approval'));s._conn.execute('UPDATE drafts SET status=? WHERE id=?',('sent' if sent else 'approved',d.id))
 if sent:s._conn.execute('INSERT INTO sends VALUES (?,?,?,?,?,?,?,?)',('send1',d.id,d.hash,'2026-11-03T12:00:00+00:00','2026-11-03T12:00:00+00:00','eml','sent','retained fake history'))
 history=s.list_sends();matches=s.list_matches();s.close()
 with Store(path,today=lambda:DAY) as s:
  assert s.list_matches()==matches and s.list_sends()==history and s.get_invoice('INV-1001').balance==0
  q=s.bank_evidence_questions()[0];assert q['evidence']['kind']=='payer' and q['evidence']['customer_names']==['Acme Ltd']
  assert not s.verified_matches() and not plan(s).thanks
  if sent:assert s.get_draft(d.id).status=='sent'
  else:
   assert s.approval(d.id) is None
   with pytest.raises(ConflictError,match='payer legal identity needs review'):s.reserve_send(d.id,d.hash,'eml')
  with pytest.raises(ConflictError):s.confirm_bank_evidence(q['id'],expected_snapshot='stale',reason='Bank checked',by='test')
  q=s.bank_evidence_questions()[0]
  s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Original bank entity and allocation verified independently',by='test')
  assert not s.bank_evidence_questions() and s.list_matches()==matches
 with Store(path,today=lambda:DAY) as s:assert not s.bank_evidence_questions()


def test_customer_name_change_rechecks_prior_automatic_allocation():
 s=Store(':memory:',today=lambda:DAY);s.upsert_customers([Customer('C1','Acme Ltd',email='ap@acme.example')]);s.upsert_invoices([invoice()]);s.add_payments([payment('Acme Ltd')]);s.apply_match(s.match_result(DAY).applied[0]);d=plan(s).thanks[0];s.approve_draft(d.id,d.hash)
 s.upsert_customers([Customer('C1','Acme GmbH',email='ap@acme.example')])
 assert s.bank_evidence_questions() and s.approval(d.id) is None and not s.verified_matches()
 with pytest.raises(ConflictError):s.reserve_send(d.id,d.hash,'eml')


def test_changed_explicit_entity_reopens_material_history_question():
 s=Store(':memory:',today=lambda:DAY);s.upsert_customers([Customer('C1','Acme Ltd')]);s.upsert_invoices([invoice()]);s.add_payments([payment('Acme GmbH')])
 s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',('P1','INV-1001','100.00','EUR','reference+exact','high','legacy','2026-11-03T12:00:00+00:00'));s.refresh_reminders()
 q=s.bank_evidence_questions()[0];s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Bank legal relationship checked',by='test')
 assert not s.bank_evidence_questions()
 s.upsert_customers([Customer('C1','Acme LLC')]);assert s.bank_evidence_questions() and s.bank_evidence_questions()[0]['id']!=q['id']
