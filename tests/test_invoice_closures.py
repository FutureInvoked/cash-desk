"""No fabricated due dates: retain and apply verified terminal observations."""
import csv
import datetime as dt
import io
from decimal import Decimal as D

import pytest
from cashdesk.importers import read_invoices
from cashdesk.model import Customer
from cashdesk.store import Store, ConflictError
from cashdesk.chasing import plan_followups

DAY = dt.date(2026, 11, 10)
SOURCES = ['generic', 'stripe', 'xero', 'quickbooks']


def export(source, *, number='INV-100', due='2026-10-20', balance='600', status='open',
           amount='1000', customer='C1', name='Alpha Demo', currency='EUR', issue='2026-10-01',
           omit_due=False, kind='Invoice', note='first'):
    row = dict(number=number, customer_id=customer, customer=customer, ContactName=name,
               email='ap@alpha.example', issue_date=issue, due_date=due, amount=amount,
               balance=balance, currency=currency, status=status, type=kind, note=note,
               reference='20261001', iban='DE89370400440532013000')
    if omit_due:
        row.pop('due_date')
    f = io.StringIO()
    writer = csv.DictWriter(f, fieldnames=list(row), lineterminator='\n')
    writer.writeheader(); writer.writerow(row)
    return f.getvalue()


def ingest(s, source, **kw):
    result = read_invoices(source, export(source, **kw))
    assert result.ok, result.errors
    return s.ingest(result)


def setup(source, path=':memory:'):
    s = Store(path, today=lambda: DAY)
    s.upsert_customers([Customer('C1', 'Alpha Demo', email='ap@alpha.example'),
                        Customer('C2', 'Beta Demo', email='ap@beta.example')])
    ingest(s, source)
    ingest(s, source, number='INV-200', customer='C2', name='Beta Demo')
    plan = plan_followups(s.list_invoices(), s.list_customers(), (), s.chase_state(), DAY,
                          sender='Demo Creditor', holds=s.reminder_holds(DAY))
    s.create_drafts(plan.all_drafts)
    for d in plan.drafts:
        s.approve_draft(d.id, d.hash)
    return s, {d.customer_id: d for d in plan.drafts}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('status,balance', [('paid', '0'), ('open', '0'), ('void', '600')])
@pytest.mark.parametrize('omit_due', [False, True])
def test_terminal_updates_without_due_date_revoke_approval(source, status, balance, omit_due):
    s, drafts = setup(source)
    before = s.get_invoice('INV-100')
    counts = ingest(s, source, due='', status=status, balance=balance, omit_due=omit_due)
    after = s.get_invoice('INV-100')
    assert after.balance == 0 and after.status == ('void' if status == 'void' else 'paid')
    assert (after.due_date, after.issue_date, after.reference, after.iban, after.amount) == (
        before.due_date, before.issue_date, before.reference, before.iban, before.amount)
    assert counts['invoice_closures'] == 1
    assert s.approval(drafts['C1'].id) is None
    with pytest.raises(ConflictError):
        s.reserve_send(drafts['C1'].id, drafts['C1'].hash, 'eml')
    assert s.reserve_send(drafts['C2'].id, drafts['C2'].hash, 'eml')
    plan = plan_followups(s.list_invoices(), s.list_customers(), (), s.chase_state(), DAY,
                         sender='Demo Creditor', holds=s.reminder_holds(DAY))
    assert all('INV-100' not in d.invoice_numbers for d in plan.drafts)
    ingest(s, source, note='stale open export')
    assert s.get_invoice('INV-100').balance == 0
    assert s.get_invoice('INV-100').status == after.status


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('status', ['paid', 'void'])
def test_initial_terminal_observation_never_invents_due_date_or_reopens(source, status, tmp_path):
    path = tmp_path / 'ledger.sqlite'
    with Store(path) as s:
        counts = ingest(s, source, due='', balance='0', status=status)
        assert s.list_invoices() == []
        assert counts['unmatched_invoice_closures'] == 1
    with Store(path) as s:
        ingest(s, source, note='later stale open row with a real date')
        inv = s.get_invoice('INV-100')
        assert inv.due_date == dt.date(2026, 10, 20)
        assert inv.balance == 0 and inv.status == status


@pytest.mark.parametrize('change', [dict(customer='C2', name='Beta Demo'), dict(currency='USD'),
    dict(amount='999'), dict(issue='2026-10-02')])
def test_terminal_invoice_update_conflicts_leave_ledger_and_approval_unchanged(change):
    s, drafts = setup('generic')
    before = s.get_invoice('INV-100')
    result = read_invoices('generic', export('generic', due='', balance='0', status='paid', **change))
    assert result.ok
    if 'customer' in change:
        s.ingest(result)
        assert s.get_invoice('INV-100')==before and s.customer_identity_reviews()
        assert s.approval(drafts['C1'].id) is None
        with pytest.raises(ConflictError,match='customer identity review'):
            s.reserve_send(drafts['C1'].id,drafts['C1'].hash,'eml')
        return
    with pytest.raises(ConflictError, match='terminal invoice update conflicts'):
        s.ingest(result)
    assert s.get_invoice('INV-100') == before and s.approval(drafts['C1'].id)
    assert not s.has_import(result.fingerprint)


@pytest.mark.parametrize('open_list', [False, True])
def test_wrong_source_terminal_update_is_atomic_even_for_open_list(open_list):
    s, drafts = setup('generic')
    result = read_invoices('stripe', export('stripe', due='', balance='0', status='paid'))
    with pytest.raises(ConflictError, match='terminal invoice update conflicts'):
        s.ingest(result, open_list=open_list)
    assert s.get_invoice('INV-100').balance == D('600')
    assert s.approval(drafts['C1'].id) and not s.has_import(result.fingerprint)


def test_open_list_keeps_terminal_identity_and_marks_only_other_missing_invoices():
    s, drafts = setup('generic')
    result = read_invoices('generic', export('generic', due='', balance='0', status='paid'))
    counts = s.ingest(result, open_list=True)
    assert counts['invoice_closures'] == 1 and counts['not_in_export'] == 1
    assert [r['number'] for r in s._conn.execute('SELECT number FROM missing_from_export')] == ['INV-200']
    assert s.get_invoice('INV-100').status == 'paid'


@pytest.mark.parametrize('source', SOURCES)
def test_terminal_invoice_update_conflicts_paid_with_nonzero_balance_is_refused(source):
    result = read_invoices(source, export(source, due='', status='paid', balance='600'))
    assert not result.ok
    assert 'terminal invoice update conflicts' in result.errors[0].message


@pytest.mark.parametrize('source', ['generic', 'xero', 'quickbooks'])
def test_open_invoice_without_due_date_is_not_promoted_to_terminal(source):
    result = read_invoices(source, export(source, due=''))
    assert not result.ok and not result.records


def test_settled_credit_and_invoice_close_together_without_crossing_identity():
    s, drafts = setup('xero')
    ingest(s, 'xero', number='CN-1', kind='ACCRECCREDIT', due='', amount='200', balance='200')
    assert s.reminder_holds(DAY)
    ingest(s, 'xero', number='CN-1', kind='ACCRECCREDIT', due='', amount='200', balance='0', status='paid')
    ingest(s, 'xero', due='', balance='0', status='paid')
    assert s.get_invoice('CN-1').balance == 0 and s.get_invoice('INV-100').balance == 0
    assert s.approval(drafts['C1'].id) is None
    assert s.get_invoice('INV-200').balance == D('600')


def test_credit_cannot_be_closed_as_an_invoice():
    s, _ = setup('xero')
    ingest(s, 'xero', number='CN-1', kind='ACCRECCREDIT', due='', amount='200', balance='200')
    with pytest.raises(ConflictError, match='terminal invoice update conflicts'):
        ingest(s, 'xero', number='CN-1', kind='ACCREC', due='', amount='200', balance='0', status='paid')
    assert s.get_invoice('CN-1').balance == D('-200')


def test_unknown_terminal_sale_cannot_later_become_a_credit():
    with Store(':memory:') as s:
        ingest(s, 'xero', due='', balance='0', status='paid')
        with pytest.raises(ConflictError, match='credit identity conflict'):
            ingest(s, 'xero', kind='ACCRECCREDIT', due='', balance='1000')
        assert s.list_invoices() == []


def test_terminal_update_with_unknown_legacy_provenance_fails_closed():
    s, drafts = setup('generic')
    s._conn.execute('DELETE FROM invoice_sources WHERE number=?', ('INV-100',))
    with pytest.raises(ConflictError, match='terminal invoice update conflicts'):
        ingest(s, 'generic', due='', balance='0', status='paid')
    assert s.get_invoice('INV-100').balance == D('600')


def test_repeated_terminal_observation_is_idempotent_and_preserves_void():
    s, _ = setup('generic')
    ingest(s, 'generic', due='', balance='0', status='void')
    ingest(s, 'generic', due='', balance='0', status='paid', note='later terminal observation')
    assert s.get_invoice('INV-100').status == 'void'
    assert s.get_invoice('INV-100').balance == 0


def test_duplicate_closure_and_open_row_refuses_entire_file():
    first = export('generic', due='', balance='0', status='paid')
    second = export('generic')
    result = read_invoices('generic', first + second.split('\n', 1)[1])
    assert not result.ok and 'duplicate invoice number' in result.errors[0].message


def test_terminal_closure_preserves_local_allocations_and_external_settlements():
    from cashdesk.model import Payment
    s, _ = setup('generic')
    s.add_payments([Payment('P1', DAY, D('200'), 'EUR', 'INV-100', 'Alpha Demo', identity_origin='provided')])
    s.apply_by_person('P1', 'INV-100', D('200'))
    matches = s.list_matches()
    assert s.get_invoice('INV-100').balance == D('400')
    ingest(s, 'generic', due='', balance='0', status='paid')
    assert s.get_invoice('INV-100').balance == 0 and s.list_matches() == matches
    ingest(s, 'generic', note='stale original snapshot')
    assert s.get_invoice('INV-100').balance == 0 and s.list_matches() == matches
