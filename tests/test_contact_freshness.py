"""Current contact identity is part of draft authority, independently of figures."""
from dataclasses import replace
import pytest
from cashdesk import Customer,Store,plan_followups
from cashdesk.importers import read_customers
from cashdesk.store import ConflictError
from tests.test_payer_identity import invoice,DAY,payment
from tests.test_receipt_lifecycle import plan

C=Customer('C1','Acme Ltd',email='old@acme.example',contact_name='Ann')
def setup(thanks=False):
 s=Store(':memory:',today=lambda:DAY);s.upsert_customers([C]);s.upsert_invoices([invoice()])
 if thanks:s.add_payments([payment('Acme Ltd')]);s.apply_match(s.match_result(DAY).applied[0])
 d=plan(s).all_drafts[0];return s,d

@pytest.mark.parametrize('field,value',[('email','new@acme.example'),('contact_name','Ben'),('name','Acme Limited'),('language','cs')])
@pytest.mark.parametrize('approved',[False,True])
@pytest.mark.parametrize('thanks',[False,True])
def test_contact_change_revokes_old_text_and_all_authority(field,value,approved,thanks):
 s,d=setup(thanks)
 if approved:s.approve_draft(d.id,d.hash)
 s.upsert_customers([replace(C,**{field:value})])
 assert s.get_draft(d.id).status=='held' and s.approval(d.id) is None
 for action in [s.approve_draft,s.check_draft,lambda did,h:s.reserve_send(did,h,'eml')]:
  with pytest.raises(ConflictError,match='customer contact changed'):action(d.id,d.hash)
 for old,why in s.stale_drafts():s.reject_draft(old.id,why)
 fresh=plan(s).all_drafts[0];assert fresh.recipients==((value,) if field=='email' else (C.email,));assert field=="name" or fresh.hash!=d.hash
 assert s.approval(fresh.id) is None;s.approve_draft(fresh.id,fresh.hash);assert s.reserve_send(fresh.id,fresh.hash,'eml')


def test_email_change_then_return_does_not_restore_original_approval():
 s,d=setup();s.approve_draft(d.id,d.hash);s.upsert_customers([replace(C,email='new@acme.example')]);s.upsert_customers([C]);assert s.approval(d.id) is None
 with pytest.raises(ConflictError,match='customer contact changed'):s.reserve_send(d.id,d.hash,'eml')

@pytest.mark.parametrize('header,row,email,contact',[
 ('customer_id,name,email,contact_name','C1,Acme Ltd,,','', ''),
 ('customer_id,name','C1,Acme Ltd',C.email,C.contact_name),
 ('customer_id,name,email','C1,Acme Ltd,', '',C.contact_name),
 ('customer_id,name,contact_name','C1,Acme Ltd,',C.email,'')])
def test_present_blank_columns_clear_but_omitted_contact_columns_preserve(header,row,email,contact):
 s,d=setup();s.approve_draft(d.id,d.hash);s.ingest(read_customers(header+'\n'+row+'\n'))
 c=s.get_customer('C1');assert (c.email,c.contact_name)==(email,contact)
 if (email,contact)!=(C.email,C.contact_name):assert s.approval(d.id) is None
 else:assert s.approval(d.id)


def test_unchanged_and_derived_fill_updates_preserve_authority():
 s,d=setup();s.approve_draft(d.id,d.hash);s.upsert_customers([C]);s.upsert_customers([replace(C,email='wrong@acme.example',contact_name='Wrong')],mode='fill')
 assert s.approval(d.id) and s.reserve_send(d.id,d.hash,'eml')


def test_contact_change_only_invalidates_that_customers_drafts():
 s,d=setup();other=replace(C,id='C2',name='Beta Demo',email='beta@beta.example');s.upsert_customers([other]);s.upsert_invoices([replace(invoice(),number='INV-2002',customer_id='C2')]);otherd=next(x for x in plan(s).drafts if x.customer_id=='C2');s.approve_draft(otherd.id,otherd.hash)
 s.upsert_customers([replace(C,email='new@acme.example')]);assert s.approval(otherd.id) and s.reserve_send(otherd.id,otherd.hash,'eml')


def test_send_gate_rechecks_current_contact_even_without_update_hook():
 s,d=setup();s.approve_draft(d.id,d.hash);s._conn.execute("UPDATE customers SET email='new@acme.example' WHERE id='C1'")
 with pytest.raises(ConflictError,match='customer contact changed'):s.reserve_send(d.id,d.hash,'eml')
 assert s.approval(d.id) is None


def test_revision_cannot_rebind_obsolete_contact_to_current_approval():
 from cashdesk.approval import with_hash
 s,d=setup();s.upsert_customers([replace(C,email='new@acme.example')]);s.replace_draft(with_hash(replace(d,recipients=('new@acme.example',))))
 with pytest.raises(ConflictError,match='customer contact changed'):s.approve_draft(d.id,s.get_draft(d.id).hash)

@pytest.mark.parametrize('sent',[False,True])
def test_legacy_contact_basis_is_not_invented_and_sent_history_is_preserved(tmp_path,sent):
 s,d=setup(thanks=True);s.approve_draft(d.id,d.hash)
 if sent:s.confirm_send(s.reserve_send(d.id,d.hash,'eml'))
 # SQLite backup retains exact old rows while modelling a pre-contact-basis ledger.
 import sqlite3
 path=tmp_path/'ledger.sqlite';dest=sqlite3.connect(path);s._conn.backup(dest);dest.execute('DROP TABLE IF EXISTS draft_contact_basis');dest.commit();dest.close();history=s.list_sends();matches=s.list_matches();s.close()
 with Store(path,today=lambda:DAY) as s:
  assert s.list_matches()==matches and s.list_sends()==history
  if sent:assert s.get_draft(d.id).status=='sent' and not plan(s).thanks
  else:
   assert s.approval(d.id) is None and s.get_draft(d.id).status=='held'
   with pytest.raises(ConflictError,match='customer contact changed'):s.reserve_send(d.id,d.hash,'eml')


def test_sent_history_never_changes_or_repeats_after_contact_update():
 s,d=setup(thanks=True);s.approve_draft(d.id,d.hash);s.confirm_send(s.reserve_send(d.id,d.hash,'eml'));history=s.list_sends();s.upsert_customers([replace(C,email='new@acme.example')]);assert s.list_sends()==history and s.get_draft(d.id).recipients==(C.email,) and not plan(s).thanks


def test_explicit_programmatic_contact_clear_and_missing_email_hold():
 s,d=setup();s.approve_draft(d.id,d.hash);s.upsert_customers([replace(C,email='')],contact_fields={'email'})
 assert s.get_customer('C1').email=='' and s.approval(d.id) is None
 assert not plan(s).all_drafts
 with pytest.raises(ConflictError,match='customer contact changed'):s.reserve_send(d.id,d.hash,'eml')


def test_unsent_contact_basis_survives_reopen_without_blessing_changes(tmp_path):
 import sqlite3
 s,d=setup();s.approve_draft(d.id,d.hash);path=tmp_path/'ledger.sqlite';dest=sqlite3.connect(path);s._conn.backup(dest);dest.close();s.close()
 with Store(path,today=lambda:DAY) as s:assert s.approval(d.id)
 with Store(path,today=lambda:DAY) as s:
  s.upsert_customers([replace(C,email='new@acme.example')]);assert s.approval(d.id) is None
 with Store(path,today=lambda:DAY) as s:
  assert s.approval(d.id) is None
  with pytest.raises(ConflictError):s.reserve_send(d.id,d.hash,'eml')


def test_contact_change_audit_preserves_prior_current_identity():
 import json
 s,d=setup();s.upsert_customers([replace(C,contact_name='Ben')]);event=s._conn.execute("SELECT detail FROM events WHERE kind='customer_contact_changed' ORDER BY id DESC LIMIT 1").fetchone();value=json.loads(event['detail']);assert value['before']['contact_name']=='Ann' and value['after']['contact_name']=='Ben'

@pytest.mark.parametrize('text',[
 'customer_id,name,email,Email\nC1,Acme Ltd,old@acme.example,new@acme.example\n',
 'customer_id,name,email,email address\nC1,Acme Ltd,old@acme.example,new@acme.example\n',
 'customer_id,name,email,contact_name\nC1,Acme Ltd\n'])
def test_uncertain_contact_columns_cannot_clear_or_partially_replace_authority(text):
 s,d=setup();r=read_customers(text);assert r.errors
 with pytest.raises(ConflictError,match='customer contact evidence conflicts'):s.ingest(r,allow_errors=True)
 assert s.get_customer('C1')==C


def test_compatible_contact_aliases_retain_explicit_column_semantics():
 s,d=setup();r=read_customers('customer_id,name,email,email address\nC1,Acme Ltd,,new@acme.example\n');assert r.ok;s.ingest(r);assert s.get_customer('C1').email=='new@acme.example'


def test_batched_contact_change_back_to_original_still_revokes_old_authority():
 s,d=setup();s.approve_draft(d.id,d.hash)
 with s.transaction():s.upsert_customers([replace(C,email='new@acme.example'),C])
 assert s.approval(d.id) is None
 with pytest.raises(ConflictError,match='customer contact changed'):s.reserve_send(d.id,d.hash,'eml')
