"""Every populated lifecycle alias must agree before skipping or creating debt."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Store,Customer,Invoice,plan_followups
from cashdesk.importers import read_invoices,read_bank_csv
from cashdesk.store import ConflictError
DAY=dt.date(2026,11,6)


def export(source='generic',status='open',other='void',kind='invoice',other_kind='',number='INV-A',amount='100',balance='100',extra_headers='',extra_values=''):
    header=('Number' if source=='stripe' else 'invoice_no')+',customer_id,ContactName,EmailAddress,issue_date,due_date,amount,balance,currency,status,Stav,type,Document Type'+extra_headers+'\n'
    return header+f'{number},A,Alpha Studio,a@alpha.example,2026-10-01,2026-10-23,{amount},{balance},EUR,{status},{other},{kind},{other_kind}'+extra_values+'\n'


def setup(applied=False):
    s=Store(':memory:',today=lambda:DAY)
    s.upsert_customers([Customer('A','Alpha Studio',email='a@alpha.example'),Customer('B','Beta Workshop',email='b@beta.example')])
    s.upsert_invoices([Invoice('INV-A','A',dt.date(2026,10,1),dt.date(2026,10,23),D('100'),'EUR',D('100')),Invoice('INV-B','B',dt.date(2026,10,1),dt.date(2026,10,23),D('200'),'EUR',D('200'))])
    if applied:
        s.ingest(read_bank_csv('id,date,amount,currency,counterparty,reference\nP1,2026-11-05,100,EUR,Alpha Studio,INV-A\n'))
        for m in s.match_result(DAY).applied:s.apply_match(m)
    p=plan(s)
    for d in p.all_drafts:s.approve_draft(d.id,d.hash)
    return s,{d.customer_id:d for d in p.all_drafts}


def plan(s):
    p=plan_followups(s.list_invoices(),s.list_customers(),s.verified_matches(),s.chase_state(),DAY,sender='Example Studio',holds=s.reminder_holds(DAY))
    s.create_drafts(p.all_drafts);s.save_chase_state(p.state);return p


@pytest.mark.parametrize('source',['generic','xero','quickbooks','stripe'])
@pytest.mark.parametrize('status,other',[('open','void'),('paid','open'),('void','disputed'),('open','mystery'),('draft','void')])
def test_all_invoice_status_aliases_are_reconciled_before_acceptance_or_skip(source,status,other):
    r=read_invoices(source,export(source,status,other,balance='0' if status=='paid' else '100'))
    assert r.errors and not r.records and not r.skipped
    assert 'invoice lifecycle evidence conflicts' in r.errors[0].message
    assert r.invoice_issues and r.invoice_issues[0].raw


@pytest.mark.parametrize('source',['generic','xero','quickbooks','stripe'])
@pytest.mark.parametrize('kind,other_kind',[('invoice','credit note'),('bill','invoice'),('credit memo','payment'),('invoice','mystery')])
def test_document_type_aliases_cannot_hide_credit_bill_or_other_type(source,kind,other_kind):
    r=read_invoices(source,export(source,'open','',kind,other_kind))
    assert r.errors and 'invoice lifecycle evidence conflicts' in r.errors[0].message
    assert not r.records and not r.credit_notes and not r.skipped


@pytest.mark.parametrize('source',['generic','xero','quickbooks','stripe'])
def test_duplicate_normalized_type_and_status_headers_refuse_before_mapping_loss(source):
    text=export(source,'open','').replace(',Document Type',',Type')
    r=read_invoices(source,text)
    assert r.errors and not r.records and 'invoice lifecycle evidence conflicts' in r.errors[0].message


@pytest.mark.parametrize('status,other,kind,other_kind',[('open','overdue','invoice','sales invoice'),('paid','paid','invoice','ACCREC'),('void','voided','credit note','credit memo'),('draft','draft','bill','purchase bill'),('','open','','invoice')])
def test_agreeing_empty_or_equivalent_generic_aliases_preserve_supported_layouts(status,other,kind,other_kind):
    r=read_invoices('generic',export(status=status,other=other,kind=kind,other_kind=other_kind,balance='0' if status=='paid' else '100'))
    assert r.ok,r.errors
    assert r.records or r.credit_updates or r.skipped


def test_partial_import_retains_status_uncertainty_and_revokes_reminder_and_thanks():
    for applied in [False,True]:
        s,drafts=setup(applied);before=s.get_invoice('INV-A');matches=s.list_matches()
        r=read_invoices('generic',export())
        s.ingest(r,allow_errors=True,file_name='conflicting.csv')
        assert s.get_invoice('INV-A')==before and s.list_matches()==matches
        assert s.invoice_import_questions() and s.approval(drafts['A'].id) is None
        with pytest.raises(ConflictError,match='invoice import uncertainty'):s.reserve_send(drafts['A'].id,drafts['A'].hash,'FAKE-NO-DELIVERY')
        assert not [d for d in plan(s).all_drafts if d.customer_id=='A']
        assert s.reserve_send(drafts['B'].id,drafts['B'].hash,'FAKE-NO-DELIVERY')


def test_rejected_new_credit_lifecycle_holds_customer_other_invoices_during_partial_import():
    s,drafts=setup();before=s.list_invoices()
    r=read_invoices('generic',export(status='open',other='void',kind='credit note',number='CN-NEW',amount='-20',balance='-20'))
    s.ingest(r,allow_errors=True)
    assert s.list_invoices()==before and 'INV-A' in s.reminder_holds(DAY)
    assert s.approval(drafts['A'].id) is None and s.approval(drafts['B'].id)


def test_later_xero_line_item_lifecycle_conflict_cannot_be_lost_during_collapse():
    text=export('xero','AUTHORISED','',kind='ACCREC')
    text+=text.split('\n')[1].replace(',AUTHORISED,,',',AUTHORISED,void,')+'\n'
    r=read_invoices('xero',text)
    assert r.errors and not r.records and 'invoice lifecycle evidence conflicts' in r.errors[0].message
    assert 'void' in r.invoice_issues[0].raw


def test_stripe_billing_aliases_must_agree_before_auto_charge_or_collection():
    r=read_invoices('stripe',export('stripe','open','',extra_headers=',Billing,Collection Method',extra_values=',send_invoice,charge_automatically'))
    assert r.errors and 'invoice lifecycle evidence conflicts' in r.errors[0].message


def test_valid_reimport_or_snapshot_verification_never_restores_old_approval():
    s,drafts=setup();r=read_invoices('generic',export());s.ingest(r,allow_errors=True)
    assert s.invoice_import_questions() and s.approval(drafts['A'].id) is None
    fixed=read_invoices('generic',export(other='open'));assert fixed.ok
    s.ingest(fixed)
    assert not s.invoice_import_questions() and s.approval(drafts['A'].id) is None
    new=next(d for d in plan(s).drafts if d.customer_id=='A');assert s.approval(new.id) is None


@pytest.mark.parametrize('kind,status,code',[('invoice','draft','draft'),('bill','open','bill')])
def test_known_issued_invoice_cannot_silently_ignore_a_later_unissued_or_non_sale_row(kind,status,code):
    s,drafts=setup();before=s.get_invoice('INV-A')
    result=read_invoices('generic',export(status=status,other='',kind=kind))
    assert result.ok and result.skipped[0].code==code
    assert any('skipped source observation may contradict a stored issued document' in q.detail for q in result.invoice_issues)
    s.ingest(result)
    assert s.get_invoice('INV-A')==before and s.invoice_import_questions()
    assert s.approval(drafts['A'].id) is None
    assert s.approval(drafts['B'].id)


def test_new_unissued_rows_remain_ordinary_skips_without_false_ledger_hold():
    s,drafts=setup();r=read_invoices('generic',export(status='draft',other='',number='NEW-DRAFT'))
    s.ingest(r);assert r.ok and r.skipped
    assert not s.invoice_import_questions() and s.approval(drafts['A'].id)


def test_bad_collapsed_lifecycle_group_does_not_guess_customer_scope_from_first_line():
    s,drafts=setup();text=export('xero','open','')
    text+=text.split('\n')[1].replace(',A,',',B,').replace(',open,,',',open,void,')+'\n'
    r=read_invoices('xero',text);assert r.errors
    s.ingest(r,allow_errors=True)
    assert all(s.approval(d.id) is None for d in drafts.values())
    assert {'INV-A','INV-B'}<=set(s.reminder_holds(DAY))


@pytest.mark.parametrize('source,status,other',[('xero','AUTHORISED','open'),('quickbooks','closed','paid'),('stripe','uncollectible','void'),('generic','overdue','open')])
def test_source_specific_equivalent_status_aliases_remain_usable(source,status,other):
    r=read_invoices(source,export(source,status,other,balance='0' if other=='paid' else '100'))
    assert r.ok,r.errors
    assert r.records[0].status==('paid' if other=='paid' else 'void' if other=='void' else 'open')


def test_xero_equivalent_line_types_and_blank_aliases_do_not_false_refuse():
    text=export('xero','AUTHORISED','','ACCREC')
    text+=text.split('\n')[1].replace(',AUTHORISED,,ACCREC,',',open,,invoice,')+'\n'
    r=read_invoices('xero',text);assert r.ok,r.errors
    assert len(r.records)==1 and r.records[0].balance==D('100')


def test_corrected_numbered_credit_recovers_import_question_but_preserves_real_credit_hold():
    s,drafts=setup();bad=read_invoices('generic',export(status='open',other='void',kind='credit note',number='CN-NEW',amount='-20',balance='-20'))
    s.ingest(bad,allow_errors=True);assert s.invoice_import_questions()
    fixed=read_invoices('generic',export(status='open',other='open',kind='credit note',number='CN-NEW',amount='-20',balance='-20'))
    s.ingest(fixed);assert not s.invoice_import_questions() and 'INV-A' in s.reminder_holds(DAY)
    closed=read_invoices('generic',export(status='paid',other='paid',kind='credit note',number='CN-NEW',amount='-20',balance='0'))
    s.ingest(closed);assert 'INV-A' not in s.reminder_holds(DAY)
    assert s.approval(drafts['A'].id) is None


def test_conflicting_update_retains_already_sent_history_and_refuses_stale_manual_verification(tmp_path):
    s,drafts=setup();d=drafts['A'];s.confirm_send(s.reserve_send(d.id,d.hash,'FAKE-NO-DELIVERY'))
    before=s.list_sends();s.ingest(read_invoices('generic',export()),allow_errors=True)
    q=s.invoice_import_questions()[0]
    with pytest.raises(ConflictError,match='invoice import uncertainty'):
        s.confirm_invoice_import_error(q['id'],expected_snapshot='stale',by='test')
    assert s.list_sends()==before and s.get_draft(d.id).status=='sent'
    s.confirm_invoice_import_error(q['id'],expected_snapshot=q['snapshot'],by='person who verified original records')
    assert s.list_sends()==before and not s.invoice_import_questions()


def test_other_customer_correction_cannot_clear_lifecycle_question_using_kept_old_owner():
    s,drafts=setup();s.ingest(read_invoices('generic',export()),allow_errors=True)
    assert s.invoice_import_questions()
    # Identity reconciliation retains A's operative financial row, but the
    # incoming B row is not proof that A's rejected lifecycle was corrected.
    text=export(other='open').replace('INV-A,A,','INV-A,B,')
    s.ingest(read_invoices('generic',text))
    assert s.get_invoice('INV-A').customer_id=='A' and s.customer_identity_reviews()
    assert s.invoice_import_questions() and s.approval(drafts['A'].id) is None
