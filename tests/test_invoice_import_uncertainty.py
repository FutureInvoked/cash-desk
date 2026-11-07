"""Stripe Paid flags and partial-import uncertainty must survive to send gates."""
import csv
import io
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk.importers import read_invoices
from cashdesk.store import Store, ConflictError
from cashdesk.model import Payment
from cashdesk.chasing import plan_followups
from tests.test_invoice_closures import setup, export, DAY


def changed(source='stripe', **changes):
    row = next(csv.DictReader(io.StringIO(export(source))))
    row.update(changes)
    f=io.StringIO(); w=csv.DictWriter(f,fieldnames=list(row),lineterminator='\n');w.writeheader();w.writerow(row)
    return f.getvalue()


def plan(s):
    return plan_followups(s.list_invoices(),s.list_customers(),s.verified_matches(),s.chase_state(),DAY,
                          sender='Demo Creditor',holds=s.reminder_holds(DAY))


@pytest.mark.parametrize('flag',['true','TRUE','1','yes'])
@pytest.mark.parametrize('status',['paid',''])
def test_stripe_paid_boolean_closes_known_invoice_without_fabricated_date(flag,status):
    s,d=setup('stripe'); before=s.get_invoice('INV-100')
    r=read_invoices('stripe',changed(Paid=flag,status=status,balance='0',due_date=''))
    assert r.ok,r.errors
    s.ingest(r)
    inv=s.get_invoice('INV-100')
    assert inv.status=='paid' and inv.balance==0 and inv.paid_date is None
    assert inv.due_date==before.due_date and inv.issue_date==before.issue_date
    assert s.approval(d['C1'].id) is None
    with pytest.raises(ConflictError):s.reserve_send(d['C1'].id,d['C1'].hash,'eml')
    assert s.reserve_send(d['C2'].id,d['C2'].hash,'eml')


@pytest.mark.parametrize('flag',['false','FALSE','0','no',''])
def test_stripe_unpaid_and_blank_flags_are_not_dates_or_settlements(flag):
    r=read_invoices('stripe',changed(Paid=flag))
    assert r.ok and len(r.records)==1
    assert r.records[0].balance==D('600') and r.records[0].paid_date is None


@pytest.mark.parametrize('flag,status,balance',[('true','open','0'),('false','paid','0'),('true','void','0'),('true','paid','600'),('maybe','paid','0')])
def test_stripe_paid_boolean_conflict_is_refused(flag,status,balance):
    r=read_invoices('stripe',changed(Paid=flag,status=status,balance=balance,due_date=''))
    assert not r.ok
    assert 'invoice' in r.errors[0].message


@pytest.mark.parametrize('alias',['Paid At','paid_date','paiddate'])
def test_stripe_explicit_paid_dates_remain_dates(alias):
    r=read_invoices('stripe',changed(Paid='true',status='paid',balance='0',due_date='',**{alias:'2026-11-03 12:00:00'}))
    assert r.ok,r.errors
    assert r.invoice_closures[0].paid_date==dt.date(2026,11,3)


def test_explicit_paid_date_alias_disagreement_is_refused():
    r=read_invoices('stripe',changed(Paid='true',status='paid',balance='0',due_date='',**{'Paid At':'2026-11-03','paid_date':'2026-11-04'}))
    assert not r.ok


@pytest.mark.parametrize('source',['generic','stripe','xero','quickbooks'])
def test_allow_errors_retains_failed_known_update_and_revokes_old_approval(source,tmp_path):
    s,d=setup(source,tmp_path/'ledger.sqlite')
    r=read_invoices(source,changed(source,balance='0',status='paid',due_date='',**{'Paid At':'invalid date'}))
    assert not r.ok
    counts=s.ingest(r,allow_errors=True,file_name='partial.csv')
    assert counts['invoice_import_questions']==1
    assert 'invoice import uncertainty' in s.reminder_holds(DAY)['INV-100']
    assert s.approval(d['C1'].id) is None
    with pytest.raises(ConflictError):s.reserve_send(d['C1'].id,d['C1'].hash,'eml')
    assert s.reserve_send(d['C2'].id,d['C2'].hash,'eml')
    assert all('INV-100' not in x.invoice_numbers for x in plan(s).drafts)
    path=s.path if hasattr(s,'path') else tmp_path/'ledger.sqlite'
    s.close()
    with Store(path,today=lambda:DAY) as reopened:
        assert 'INV-100' in reopened.invoice_import_holds()
        issue=reopened.invoice_import_questions()[0]
        assert issue['raw'] and issue['number']=='INV-100' and issue['source']==source
        assert issue['fingerprint']==r.fingerprint


def test_partial_import_valid_rows_do_not_clear_a_failed_row_or_its_approval():
    s,d=setup('generic')
    bad=changed('generic',balance='bad')
    good=next(csv.DictReader(io.StringIO(export('generic',number='INV-300',customer='C2',name='Beta Demo'))))
    header=next(csv.reader(io.StringIO(bad))); f=io.StringIO(bad);f.seek(0,2)
    csv.DictWriter(f,fieldnames=header,lineterminator='\n').writerow(good)
    r=read_invoices('generic',f.getvalue());assert not r.ok and len(r.records)==1
    s.ingest(r,allow_errors=True)
    assert s.get_invoice('INV-300').balance==D('600')
    assert s.approval(d['C1'].id) is None
    assert s.invoice_import_holds().keys()=={'INV-100'}
    s.ingest(read_invoices('generic',export('generic',number='INV-200',customer='C2',name='Beta Demo',note='unrelated corrected export')))
    assert 'INV-100' in s.invoice_import_holds()


def test_rejected_strict_import_preserves_transaction_and_previous_approval():
    s,d=setup('stripe');r=read_invoices('stripe',changed(Paid='maybe'))
    assert not r.ok
    with pytest.raises(ConflictError):s.ingest(r)
    assert s.approval(d['C1'].id) and not s.has_import(r.fingerprint)
    assert not s.invoice_import_questions()


def test_valid_correction_resolves_only_same_source_identity_without_restoring_approval():
    s,d=setup('generic');r=read_invoices('generic',changed('generic',balance='bad'))
    s.ingest(r,allow_errors=True)
    s.ingest(read_invoices('stripe',changed('stripe',note='wrong source')))
    assert s.invoice_import_questions()
    s.ingest(read_invoices('generic',changed('generic',note='verified corrected source row')))
    assert not s.invoice_import_questions() and not s.invoice_import_holds()
    assert s.approval(d['C1'].id) is None
    with pytest.raises(ConflictError):s.reserve_send(d['C1'].id,d['C1'].hash,'eml')
    assert plan(s).drafts


def test_unknown_row_identity_holds_source_until_explicit_snapshot_verification():
    s,d=setup('generic');r=read_invoices('generic',changed('generic',number='',balance='bad'))
    s.ingest(r,allow_errors=True)
    assert set(s.invoice_import_holds())=={'INV-100','INV-200'}
    issue=s.invoice_import_questions()[0]
    with pytest.raises(ConflictError,match='invoice import uncertainty'):
        s.confirm_invoice_import_error(issue['id'],expected_snapshot='stale',by='FAKE REVIEW')
    s.confirm_invoice_import_error(issue['id'],expected_snapshot=issue['snapshot'],by='FAKE REVIEW')
    assert not s.invoice_import_questions() and s.approval(d['C1'].id) is None
    assert s.get_invoice('INV-100').balance==D('600')
    assert not s.list_matches() and not s.list_sends()


def test_new_unknown_number_is_retained_and_cannot_become_eligible_later_without_correction():
    s,d=setup('generic');r=read_invoices('generic',changed('generic',number='NEW-404',balance='bad'))
    s.ingest(r,allow_errors=True)
    assert not s.invoice_import_holds() and len(s.invoice_import_questions())==1
    s.upsert_invoices([__import__('dataclasses').replace(s.get_invoice('INV-100'),number='NEW-404')])
    assert 'NEW-404' in s.invoice_import_holds()


def test_invoice_import_uncertainty_blocks_new_allocation_and_acknowledgement():
    s,d=setup('generic');r=read_invoices('generic',changed('generic',balance='bad'))
    s.ingest(r,allow_errors=True)
    s.add_payments([Payment('P1',dt.date(2026,11,3),D('600'),'EUR','INV-100','Alpha Demo')])
    result=s.match_result(DAY)
    assert not result.applied and result.review
    from cashdesk.model import Match
    with pytest.raises(ConflictError,match='invoice import uncertainty'):
        s.apply_match(Match('P1','INV-100',D('600'),'EUR','manual','reviewed','FAKE REVIEW'))


@pytest.mark.parametrize('header',['Paid','Paid At','number','status'])
def test_duplicate_lifecycle_headers_cannot_hide_failed_terminal_evidence(header):
    text=changed(Paid='true',status='paid',balance='0',due_date='',**{'Paid At':'2026-11-03'})
    rows=list(csv.reader(io.StringIO(text)));rows[0].append(header);rows[1].append('false')
    f=io.StringIO();csv.writer(f,lineterminator='\n').writerows(rows)
    r=read_invoices('stripe',f.getvalue())
    assert not r.ok and not r.records and not r.invoice_closures


def test_stripe_unpaid_flag_with_paid_date_is_refused():
    r=read_invoices('stripe',changed(Paid='false',**{'Paid At':'2026-11-03'}))
    assert not r.ok


def test_generic_paid_date_alias_still_supported():
    r=read_invoices('generic',changed('generic',Paid='2026-11-03',status='paid',balance='0',due_date=''))
    assert r.ok and r.invoice_closures[0].paid_date==dt.date(2026,11,3)


def test_invalid_paid_date_with_due_date_is_not_silently_discarded():
    r=read_invoices('stripe',changed(status='paid',balance='0',**{'Paid At':'not a date'}))
    assert not r.ok


def test_stripe_paid_flag_checks_every_populated_status_alias():
    r=read_invoices('stripe',changed(Paid='true',status='paid',stav='open',balance='0',due_date=''))
    assert not r.ok


def test_conflicting_invoice_number_aliases_get_source_wide_error_scope():
    s,d=setup('generic');r=read_invoices('generic',changed('generic',invoice_number='INV-200',balance='bad'))
    assert not r.ok
    s.ingest(r,allow_errors=True)
    assert set(s.invoice_import_holds())=={'INV-100','INV-200'}


def test_duplicate_row_cannot_clear_its_own_error_after_valid_record():
    s,d=setup('generic');text=changed('generic',note='same import')
    text+=text.splitlines()[1]+'\n'
    r=read_invoices('generic',text);assert len(r.records)==1 and r.errors
    s.ingest(r,allow_errors=True)
    assert 'INV-100' in s.invoice_import_holds() and s.approval(d['C1'].id) is None


def test_confirmation_snapshot_covers_invoice_changes_and_prior_history():
    s,d=setup('generic');s.ingest(read_invoices('generic',changed('generic',balance='bad')),allow_errors=True)
    q=s.invoice_import_questions()[0]
    from dataclasses import replace
    s.upsert_invoices([replace(s.get_invoice('INV-200'),note='changed since review')])
    with pytest.raises(ConflictError,match='invoice import uncertainty'):
        s.confirm_invoice_import_error(q['id'],expected_snapshot=q['snapshot'])
    assert len(s.invoice_import_questions())==1


def test_partial_header_failure_pauses_legacy_source_and_is_visible():
    s,d=setup('generic');s._conn.execute('DELETE FROM invoice_sources')
    r=read_invoices('generic','not,a,usable,export\nhello,world,broken,data\n')
    assert r.errors
    s.ingest(r,allow_errors=True,file_name='broken.csv')
    assert set(s.invoice_import_holds())=={'INV-100','INV-200'}
    assert all(s.approval(x.id) is None for x in d.values())


def test_paid_flag_initial_closure_prevents_later_stale_opening():
    with Store(':memory:',today=lambda:DAY) as s:
        r=read_invoices('stripe',changed(Paid='true',status='paid',balance='0',due_date=''))
        assert r.ok
        s.ingest(r);assert not s.list_invoices()
        s.ingest(read_invoices('stripe',changed(note='late stale row')))
        assert s.get_invoice('INV-100').balance==0


def test_previously_sent_history_is_retained_after_partial_import_and_recovery():
    s,d=setup('generic');draft=d['C1']
    s.save_chase_state(plan(s).state)
    key=s.reserve_send(draft.id,draft.hash,'eml');s.confirm_send(key)
    before=s.list_sends()
    s.ingest(read_invoices('generic',changed('generic',balance='bad')),allow_errors=True)
    assert s.list_sends()==before and s.get_draft(draft.id).status=='sent'
    s.ingest(read_invoices('generic',changed('generic',note='corrected valid row')))
    assert s.list_sends()==before
    assert all('INV-100' not in x.invoice_numbers for x in plan(s).drafts)


def test_failed_update_suppresses_verified_receipt_thanks_but_keeps_allocations():
    s,d=setup('generic')
    s.add_payments([Payment('P1',dt.date(2026,11,3),D('600'),'EUR','INV-100','Alpha Demo')])
    for m in s.match_result(DAY).applied:s.apply_match(m)
    matches=s.list_matches();assert matches
    thank=plan(s).thanks[0];s.create_drafts([thank]);s.approve_draft(thank.id,thank.hash)
    s.ingest(read_invoices('generic',changed('generic',balance='bad')),allow_errors=True)
    assert s.list_matches()==matches and not s.verified_matches()
    assert s.approval(thank.id) is None and not plan(s).thanks
    with pytest.raises(ConflictError):s.reserve_send(thank.id,thank.hash,'eml')


def test_empty_or_malformed_snapshot_cannot_remove_invoice_import_uncertainty():
    s,d=setup('generic');s.ingest(read_invoices('generic',changed('generic',balance='bad')),allow_errors=True)
    q=s.invoice_import_questions()[0]
    with pytest.raises(ConflictError,match='invoice import uncertainty'):
        s.confirm_invoice_import_error(q['id'],expected_snapshot='')
    assert s.invoice_import_questions()
