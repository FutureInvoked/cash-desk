"""Authoritative customer IDs cannot be replaced by a same-name recipient."""
import datetime as dt
from decimal import Decimal as D
from dataclasses import replace
import pytest
from cashdesk import Customer,Invoice,Store,plan_followups
from cashdesk.importers import read_customers,read_invoices,read_bank_csv,customer_key
from cashdesk.store import ConflictError,DuplicateImportError
DAY=dt.date(2026,11,6)
STRIPE='id,Number,Customer,Customer Name,Customer Email,Amount Due,Amount Paid,Amount Remaining,Total,Currency,Date (UTC),Due Date (UTC),Status,Paid\n'


def stripe(number='STR-9001',customer='cus_branch_A',email='branch-a@amber.example',amount='100',status='open',paid='false',name='Amber Studio Ltd'):
    return STRIPE+f'in_{number},{number},{customer},{name},{email},{amount},0,{amount},{amount},eur,2026-10-01,2026-10-23,{status},{paid}\n'


def ingest(s,text,source='stripe',file='invoices.csv',**kw):
    if source=='quickbooks':text=text.replace('AUTHORISED','open')
    r=read_invoices(source,text);assert r.ok,r.errors
    return s.ingest(r,file_name=file,**kw)


def plan(s):
    p=plan_followups(s.list_invoices(),s.list_customers(),s.verified_matches(),s.chase_state(),DAY,sender='Example Studio',holds=s.reminder_holds(DAY))
    s.create_drafts(p.all_drafts);s.save_chase_state(p.state);return p


def setup():
    s=Store(':memory:',today=lambda:DAY);ingest(s,stripe());return s


@pytest.mark.parametrize('single_file',[False,True])
def test_distinct_explicit_stripe_ids_keep_separate_debt_and_recipient(single_file):
    s=Store(':memory:',today=lambda:DAY)
    second=stripe('STR-9002','cus_branch_B','branch-b@amber.example','200')
    if single_file:ingest(s,stripe()+second.split('\n',1)[1])
    else:ingest(s,stripe());ingest(s,second,file='second.csv')
    assert s.get_invoice('STR-9002').customer_id=='cus_branch_B'
    assert {c.id for c in s.list_customers()}=={'cus_branch_A','cus_branch_B'}
    p=plan(s);assert len(p.drafts)==2
    for d in p.drafts:
        cid=d.customer_id;expected='branch-a@amber.example' if cid=='cus_branch_A' else 'branch-b@amber.example'
        assert tuple(d.recipients)==(expected,) and len(d.invoice_numbers)==1
        s.approve_draft(d.id,d.hash);assert s.reserve_send(d.id,d.hash,'FAKE-NO-DELIVERY')


def test_same_name_explicit_id_never_invalidates_or_changes_unrelated_customer_draft():
    s=setup();d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
    ingest(s,stripe('STR-9002','cus_branch_B','branch-b@amber.example','200'))
    assert s.get_draft(d.id)==replace(d,status='approved')
    assert s.approval(d.id) and s.get_invoice('STR-9002').customer_id=='cus_branch_B'


def test_explicit_stripe_id_equal_to_generated_slug_stays_authoritative():
    s=setup();slug=customer_key('Amber Studio Ltd')
    ingest(s,stripe('STR-9002',slug,'branch-b@amber.example','200'))
    assert s.get_invoice('STR-9002').customer_id==slug and s.get_customer(slug).email=='branch-b@amber.example'


@pytest.mark.parametrize('source,identifier',[('xero','ContactID'),('quickbooks','Customer ID')])
def test_recognized_explicit_ids_are_preserved_in_other_invoice_sources(source,identifier):
    s=Store(':memory:',today=lambda:DAY)
    header=f'{identifier},ContactName,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Type,Status,EmailAddress\n'
    for n,email,amount in [('ID-A','a@amber.example','100'),('ID-B','b@amber.example','200')]:
        ingest(s,header+f'{n},Amber Studio Ltd,INV-{n},2026-10-01,2026-10-23,{amount},{amount},EUR,Invoice,AUTHORISED,{email}\n',source=source)
    assert {i.customer_id for i in s.list_invoices()}=={'ID-A','ID-B'}
    assert len(plan(s).drafts)==2


@pytest.mark.parametrize('source',['stripe','xero','quickbooks'])
def test_name_derived_contact_can_adopt_unique_compatible_explicit_id(source):
    s=Store(':memory:',today=lambda:DAY)
    s.ingest(read_customers('customer_id,name,email\nKNOWN,Amber Studio Ltd,branch-a@amber.example\n'))
    text=(stripe(customer='') if source=='stripe' else 'ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Type,Status\nAmber Studio Ltd,branch-a@amber.example,INV-A,2026-10-01,2026-10-23,100,100,EUR,Invoice,AUTHORISED\n')
    ingest(s,text,source=source)
    assert s.list_invoices()[0].customer_id=='KNOWN' and not s.customer_identity_reviews()
    assert len(plan(s).drafts)==1


@pytest.mark.parametrize('email',['branch-a@amber.example','branch-b@amber.example'])
def test_derived_same_name_with_multiple_explicit_targets_requires_identity_review(email):
    s=setup();ingest(s,stripe('STR-9002','cus_branch_B','branch-b@amber.example','200'))
    text='ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Type,Status\n'+f'Amber Studio Ltd,{email},INV-DERIVED,2026-10-01,2026-10-23,100,100,EUR,Invoice,AUTHORISED\n'
    ingest(s,text,source='xero')
    assert s.customer_identity_reviews() and not plan(s).drafts
    assert s.get_invoice('INV-DERIVED').customer_id not in {'cus_branch_A','cus_branch_B'}


def test_unique_name_with_conflicting_email_is_not_sufficient_for_derived_adoption():
    s=setup();text='ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Type,Status\nAmber Studio Ltd,other@amber.example,INV-D,2026-10-01,2026-10-23,100,100,EUR,Invoice,AUTHORISED\n'
    ingest(s,text,source='xero')
    assert s.customer_identity_reviews() and s.get_invoice('INV-D').customer_id!= 'cus_branch_A'


def test_explicit_id_change_on_known_invoice_retains_finances_and_revokes_old_authority():
    s=setup();d=plan(s).drafts[0];s.approve_draft(d.id,d.hash);before=s.get_invoice('STR-9001')
    ingest(s,stripe(customer='cus_branch_B',email='branch-b@amber.example'),file='changed.csv')
    assert s.get_invoice('STR-9001')==before and s.customer_identity_reviews()
    assert s.approval(d.id) is None
    with pytest.raises(ConflictError,match='customer identity review'):s.approve_draft(d.id,d.hash)
    with pytest.raises(ConflictError,match='customer identity review'):s.reserve_send(d.id,d.hash,'FAKE-NO-DELIVERY')
    assert not plan(s).drafts


def test_identity_observation_provenance_is_raw_and_reimport_is_idempotent():
    s=setup();before=s.customer_identity_observations()
    assert before[0]['origin']=='explicit' and 'cus_branch_A' in before[0]['raw']
    with pytest.raises(DuplicateImportError):ingest(s,stripe(),file='copied.csv')
    assert s.customer_identity_observations()==before


def test_legacy_import_with_unknown_original_owner_is_held_on_reopen(tmp_path):
    path=tmp_path/'synthetic.sqlite'
    with Store(path,today=lambda:DAY) as s:
        s.upsert_customers([Customer('cus_branch_A','Amber Studio Ltd',email='branch-a@amber.example')])
        s.upsert_invoices([Invoice('STR-OLD','cus_branch_A',dt.date(2026,10,1),dt.date(2026,10,23),D('200'),'EUR',D('200'))])
        d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
        s._conn.execute('INSERT INTO invoice_sources VALUES (?,?,?,?)',('STR-OLD','stripe','legacy.csv','2026-11-05T00:00:00+00:00'))
        before=s.get_invoice('STR-OLD')
    with Store(path,today=lambda:DAY) as s:
        assert s.get_invoice('STR-OLD')==before and s.customer_identity_reviews()
        assert s.approval(d.id) is None and not plan(s).drafts
        assert s.customer_identity_reviews()[0]['legacy_unknown']


def test_human_identity_resolution_requires_snapshot_and_explanation_and_fresh_text():
    s=setup();d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
    ingest(s,stripe(customer='cus_branch_B',email='branch-b@amber.example'))
    q=s.customer_identity_reviews()[0]
    with pytest.raises(ConflictError,match='customer identity review'):
        s.confirm_customer_identity(q['number'],customer_id='cus_branch_B',expected_snapshot='stale',reason='Verified accounting export',by='test')
    with pytest.raises(ConflictError,match='customer identity review'):
        s.confirm_customer_identity(q['number'],customer_id='cus_branch_B',expected_snapshot=q['snapshot'],reason='',by='test')
    s.confirm_customer_identity(q['number'],customer_id='cus_branch_B',expected_snapshot=q['snapshot'],reason='Verified original customer ID and email against original accounting record',by='test')
    assert s.get_invoice('STR-9001').customer_id=='cus_branch_B' and not s.customer_identity_reviews()
    assert s.approval(d.id) is None
    p=plan(s);assert len(p.drafts)==1 and tuple(p.drafts[0].recipients)==('branch-b@amber.example',)


@pytest.mark.parametrize('extra',['Customer ID','Customer'])
def test_invoice_conflicting_explicit_id_aliases_are_retained_as_partial_import_uncertainty(extra):
    s=setup();d=plan(s).drafts[0];s.approve_draft(d.id,d.hash)
    if extra=='Customer':
        text=stripe().replace('Customer,Customer Name','Customer,Customer,Customer Name').replace('cus_branch_A,Amber','cus_branch_A,cus_branch_B,Amber')
    else:
        text=stripe().replace('Customer,Customer Name','Customer,Customer ID,Customer Name').replace('cus_branch_A,Amber','cus_branch_A,cus_branch_B,Amber')
    r=read_invoices('stripe',text)
    assert r.errors and 'customer identity conflicts' in r.errors[0].message
    s.ingest(r,allow_errors=True)
    assert s.invoice_import_questions() and s.approval(d.id) is None
    assert s.get_invoice('STR-9001').customer_id=='cus_branch_A'


def test_customer_contact_ids_with_name_column_are_authoritative_and_alias_conflict_cannot_be_partial():
    s=Store(':memory:',today=lambda:DAY)
    r=read_customers('ContactID,Customer,Email\nCID-A,Amber Studio Ltd,branch-a@amber.example\nCID-B,Amber Studio Ltd,branch-b@amber.example\n')
    assert r.ok and {c.id for c in r.records}=={'CID-A','CID-B'}
    s.ingest(r)
    bad=read_customers('customer_id,id,name,email\nCID-A,CID-B,Amber Studio Ltd,branch-b@amber.example\n')
    assert bad.errors and 'customer identity conflicts' in bad.errors[0].message
    with pytest.raises(ConflictError,match='customer identity conflicts'):s.ingest(bad,allow_errors=True)
    assert s.get_customer('CID-A').email=='branch-a@amber.example'


def test_same_file_derived_email_conflict_cannot_leave_first_name_remap_in_force():
    s=setup()
    header='ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Type,Status\n'
    text=header+'Amber Studio Ltd,branch-a@amber.example,INV-D1,2026-10-01,2026-10-23,100,100,EUR,Invoice,AUTHORISED\nAmber Studio Ltd,branch-b@amber.example,INV-D2,2026-10-01,2026-10-23,100,100,EUR,Invoice,AUTHORISED\n'
    ingest(s,text,source='xero')
    assert {s.get_invoice(n).customer_id for n in ['INV-D1','INV-D2']}=={customer_key('Amber Studio Ltd')}
    assert len(s.customer_identity_reviews())==2 and not plan(s).drafts


def test_name_only_contact_list_adopts_unique_explicit_customer_without_renaming_id():
    s=Store(':memory:',today=lambda:DAY);s.upsert_customers([Customer('KNOWN','Amber Studio Ltd')])
    s.ingest(read_customers('Customer,Email\nAmber Studio Ltd,branch-a@amber.example\n'))
    assert s.get_customer('KNOWN').email=='branch-a@amber.example' and len(s.list_customers())==1


def test_uncertain_invoice_owner_cannot_receive_automatic_or_manual_allocations():
    s=setup();ingest(s,stripe(customer='cus_branch_B',email='branch-b@amber.example'))
    s.ingest(read_bank_csv('id,date,amount,currency,counterparty,reference\nP1,2026-11-05,100,EUR,Amber Studio Ltd,STR-9001\n'))
    assert not s.match_result(DAY).applied
    with pytest.raises(ConflictError,match='customer identity review'):s.apply_by_person('P1','STR-9001')
    assert not s.list_matches()


@pytest.mark.parametrize('history',['allocated','sent','export-paid'])
def test_customer_identity_change_never_reassigns_financial_or_sent_history(history):
    s=setup();original_matches=[];original_sends=[]
    if history=='allocated':
        s.ingest(read_bank_csv('id,date,amount,currency,counterparty,reference\nP1,2026-11-05,100,EUR,Amber Studio Ltd,STR-9001\n'))
        for m in s.match_result(DAY).applied:s.apply_match(m)
    elif history=='export-paid':
        ingest(s,stripe().replace(',0,100,100,eur',',100,0,100,eur').replace(',open,false',',paid,true'))
    else:
        d=plan(s).drafts[0];s.approve_draft(d.id,d.hash);s.confirm_send(s.reserve_send(d.id,d.hash,'FAKE-NO-DELIVERY'))
    original_matches=s.list_matches();original_sends=s.list_sends();before=s.get_invoice('STR-9001')
    ingest(s,stripe(customer='cus_branch_B',email='branch-b@amber.example'))
    q=s.customer_identity_reviews()[0]
    with pytest.raises(ConflictError,match='needs audited correction'):
        s.confirm_customer_identity(q['number'],customer_id='cus_branch_B',expected_snapshot=q['snapshot'],reason='Verified changed customer',by='test')
    assert s.get_invoice('STR-9001')==before and s.list_matches()==original_matches and s.list_sends()==original_sends
    assert s.customer_identity_reviews() and not s.verified_matches()
    if history=='sent':assert s.get_draft(d.id).status=='sent'


def test_explicit_credit_identity_does_not_pause_distinct_equal_name_customer():
    s=setup();s.upsert_customers([Customer('cus_branch_B','Amber Studio Ltd',email='branch-b@amber.example')])
    text='invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,type\nCN-B,cus_branch_B,2026-10-01,,-20,EUR,-20,open,credit note\n'
    ingest(s,text,source='generic')
    assert 'STR-9001' not in s.reminder_holds(DAY) and plan(s).drafts[0].customer_id=='cus_branch_A'


def test_legacy_credit_original_customer_hint_is_quarantined_without_guessed_remap(tmp_path):
    path=tmp_path/'old-credit.sqlite'
    with Store(path,today=lambda:DAY) as s:
        s.upsert_customers([Customer('cus_branch_A','Amber Studio Ltd',email='branch-a@amber.example')])
        s.upsert_invoices([Invoice('CN-OLD','cus_branch_A',dt.date(2026,10,1),dt.date(2026,10,1),D('-20'),'EUR',D('-20'))])
        s._conn.execute('INSERT INTO credit_sources VALUES (?,?,?,?,?,?)',('CN-OLD','stripe','credit_note','cus_branch_B','CN-OLD','2026-11-05T00:00:00+00:00'))
        before=s.get_invoice('CN-OLD')
    with Store(path,today=lambda:DAY) as s:
        q=s.customer_identity_reviews()[0]
        assert q['legacy_unknown'] and set(q['customer_ids'])=={'cus_branch_A','cus_branch_B'}
        assert q['observations'][0]['origin']=='unknown' and s.get_invoice('CN-OLD')==before


def test_new_anonymous_credit_has_origin_basis_and_does_not_become_false_legacy_question(tmp_path):
    path=tmp_path/'anonymous.sqlite'
    with Store(path,today=lambda:DAY) as s:
        text='ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Type,Status\nAmber Studio Ltd,branch-a@amber.example,,2026-10-01,,20,20,EUR,ACCRECCREDIT,AUTHORISED\n'
        ingest(s,text,source='xero')
        assert len(s.list_invoices())==1 and not s.customer_identity_reviews()
    with Store(path,today=lambda:DAY) as s:assert not s.customer_identity_reviews()


def test_identity_verification_cannot_guess_unobserved_customer_id():
    s=setup();s.upsert_customers([Customer('OTHER','Other Workshop',email='a@other.example')])
    ingest(s,stripe(customer='cus_branch_B',email='branch-b@amber.example'))
    q=s.customer_identity_reviews()[0]
    with pytest.raises(ConflictError,match='verify a retained customer ID'):
        s.confirm_customer_identity(q['number'],customer_id='OTHER',expected_snapshot=q['snapshot'],reason='Verify a new identity',by='test')
    assert s.get_invoice('STR-9001').customer_id=='cus_branch_A'


def test_contact_question_cannot_bulk_transfer_debt_of_other_customer():
    s=setup();ingest(s,stripe('STR-9002','cus_branch_B','branch-b@amber.example','200'))
    s.ingest(read_customers('Customer,Email\nAmber Studio Ltd,branch-a@amber.example\n'))
    q=s.customer_identity_reviews()[0];before=s.list_invoices()
    with pytest.raises(ConflictError,match='verify each affected invoice separately'):
        s.confirm_customer_identity(q['number'],customer_id='cus_branch_A',expected_snapshot=q['snapshot'],reason='Verified one contact only',by='test')
    assert s.list_invoices()==before and s.customer_identity_reviews()


def test_lost_legacy_source_owner_conservatively_pauses_whole_ledger(tmp_path):
    path=tmp_path/'lost-origin.sqlite'
    with Store(path,today=lambda:DAY) as s:
        for cid,name,amount in [('A','Amber Studio Ltd','100'),('B','Other Workshop','200')]:
            s.upsert_customers([Customer(cid,name,email=cid.lower()+'@client.example')])
            s.upsert_invoices([Invoice('INV-'+cid,cid,dt.date(2026,10,1),dt.date(2026,10,23),D(amount),'EUR',D(amount))])
        drafts=plan(s).drafts
        for d in drafts:s.approve_draft(d.id,d.hash)
        s._conn.execute('INSERT INTO invoice_sources VALUES (?,?,?,?)',('INV-A','stripe','old.csv','2026-11-05T00:00:00+00:00'))
    with Store(path,today=lambda:DAY) as s:
        assert {'INV-A','INV-B'}<=set(s.customer_identity_holds())
        assert all(s.approval(d.id) is None for d in drafts)
        assert s.customer_identity_reviews()[0]['scope']=='unknown legacy owner: whole ledger'


@pytest.mark.parametrize('source,text',[
    ('generic','invoice_no,customer,customer,issue_date,due_date,amount,currency,balance\nINV-A,CID-A,CID-B,2026-10-01,2026-10-23,100,EUR,100\n'),
    ('xero','ContactName,ContactName,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Status\nAmber Studio Ltd,Other Workshop,INV-A,2026-10-01,2026-10-23,100,100,EUR,AUTHORISED\n'),
    ('xero','ContactName,Customer Name,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountDue,Currency,Status\nAmber Studio Ltd,Other Workshop,INV-A,2026-10-01,2026-10-23,100,100,EUR,AUTHORISED\n')])
def test_fallback_customer_identity_columns_cannot_silently_discard_conflicting_identity(source,text):
    result=read_invoices(source,text)
    assert result.errors and not result.records
    assert 'customer identity conflicts' in result.errors[0].message
