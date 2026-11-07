"""Contradictory invoice evidence never becomes debt or mutates approval history."""
import csv,io,datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk.importers import read_invoices
from cashdesk import Store,Customer,plan_followups
from cashdesk.store import ConflictError
DAY=dt.date(2026,11,10)
SOURCES=['generic','xero','quickbooks','stripe']


def export(source,changes=None,extra=(),duplicate=None):
    row={'number':'INV-1001','customer_id':'C1','customer':'C1','ContactName':'Alpha Demo','email':'a@alpha.example',
         'issue_date':'2026-10-01','due_date':'2026-10-20','amount':'1000','balance':'600','currency':'EUR',
         'status':'open','type':'Invoice','note':'initial'}
    row.update(changes or {})
    headers=list(row)+[k for k,v in extra];values=list(row.values())+[v for k,v in extra]
    if duplicate:
        headers.append(duplicate[0]);values.append(duplicate[1])
    out=io.StringIO();w=csv.writer(out,lineterminator='\n');w.writerow(headers);w.writerow(values);return out.getvalue()


BAD=[({'amount':'USD 1000'},()),({'balance':'USD 600'},()),({},(('měna','USD'),)),
     ({'amount':'1000','balance':'1000'},(('amount paid','400'),)),
     ({},(('outstanding','0'),)),({},(('total','900'),)),({},(('amount paid','400'),('InvoiceAmountPaid','300'))),
     ({'balance':'1200'},()),({'status':'paid'},()),({},(('amount paid','-400'),)),
     ({'amount':'EUR 1000','balance':'GBP 600'},()),({},(('total','not-money'),)),
     ({},(('amount paid','USD 400'),)),({},(('Currency (USD)','USD'),))]


@pytest.mark.parametrize('source',SOURCES)
@pytest.mark.parametrize('changes,extra',BAD)
def test_invoice_monetary_evidence_conflicts_refuse_before_ingestion(source,changes,extra):
    r=read_invoices(source,export(source,changes,extra))
    assert not r.ok and not r.records
    assert 'invoice monetary evidence conflicts' in r.errors[0].message
    with Store(':memory:') as s:
        with pytest.raises(ConflictError):s.ingest(r)
        assert not s.list_invoices() and not s.list_matches() and not s.list_drafts()
        assert not s.has_import(r.fingerprint)


@pytest.mark.parametrize('header',['amount','balance','currency','amount paid','Amount (EUR)'])
@pytest.mark.parametrize('value',['0','1000'])
def test_duplicate_financial_headers_refuse_even_when_values_look_equal(header,value):
    extras=((header,'0'),) if header=='amount paid' else ()
    if header=='Amount (EUR)': extras=()
    r=read_invoices('generic',export('generic',extra=extras,duplicate=(header,value)))
    assert not r.ok and 'invoice monetary evidence conflicts' in r.errors[0].message
    assert 'repeated financial headers' in r.errors[0].message


@pytest.mark.parametrize('source',SOURCES)
def test_equal_aliases_and_consistent_partial_controls(source):
    r=read_invoices(source,export(source,extra=(('total','1000.00'),('outstanding','600.00'),('amount paid','400'),('InvoiceAmountPaid','400.00'),('měna','eur'))))
    assert r.ok,r.errors
    assert (r.records[0].amount,r.records[0].balance,r.records[0].currency)==(D('1000'),D('600'),'EUR')


@pytest.mark.parametrize('source',SOURCES)
def test_currency_can_be_inferred_from_remaining_when_gross_has_no_label(source):
    r=read_invoices(source,export(source,{'currency':'','balance':'USD 600'}))
    assert r.ok and r.records[0].currency=='USD'


def test_stripe_billing_due_and_remaining_are_distinct_with_partial_and_credit():
    r=read_invoices('stripe',export('stripe',{'balance':'','note':'credit offset'},(('Amount Due','800'),('Amount Remaining','600'),('Amount Paid','200'))))
    assert r.ok,r.errors
    assert r.records[0].balance==D('600') and r.records[0].amount==D('1000')


def test_stripe_contradictory_billing_due_paid_remaining_is_refused():
    r=read_invoices('stripe',export('stripe',{'balance':''},(('Amount Due','1000'),('Amount Remaining','600'),('Amount Paid','200'))))
    assert not r.ok and 'invoice monetary evidence conflicts' in r.errors[0].message


@pytest.mark.parametrize('status,remaining,paid',[('void','0','0'),('void','1000','0'),('paid','0','1000')])
def test_terminal_and_void_controls_preserve_source_semantics(status,remaining,paid):
    r=read_invoices('xero',export('xero',{'status':status,'balance':remaining},(('InvoiceAmountPaid',paid),)))
    assert r.ok,r.errors
    assert r.records[0].status==status


def test_conflicting_reimport_is_atomic_and_preserves_settlement_and_approval():
    with Store(':memory:',today=lambda:DAY) as s:
        s.upsert_customers([Customer('C1','Alpha Demo',email='a@alpha.example')])
        s.ingest(read_invoices('generic',export('generic',extra=(('amount paid','400'),))))
        plan=plan_followups(s.list_invoices(),s.list_customers(),(),s.chase_state(),DAY,sender='Demo Creditor')
        d=plan.drafts[0];s.create_draft(d);s.approve_draft(d.id,d.hash)
        before=s.get_invoice('INV-1001');basis=s.settlement_snapshot('INV-1001')
        bad=read_invoices('generic',export('generic',{'note':'later export'},(('amount paid','600'),)))
        assert not bad.ok
        with pytest.raises(ConflictError):s.ingest(bad)
        assert s.get_invoice('INV-1001')==before and s.settlement_snapshot('INV-1001')==basis
        assert s.approval(d.id) and not s.has_import(bad.fingerprint)
        assert not s.list_sends()


def test_xero_alias_conflict_on_second_line_cannot_hide_in_collapsing():
    first=export('xero',extra=(('outstanding','600'),))
    second=export('xero',{'note':'second line'},(('outstanding','0'),)).split('\n',1)[1]
    r=read_invoices('xero',first+second)
    assert not r.ok and not r.records


def test_financial_header_currency_is_reconciled_with_embedded_and_field_evidence():
    text=export('generic').replace('amount,','Amount (USD),',1)
    r=read_invoices('generic',text)
    assert not r.ok and 'invoice monetary evidence conflicts' in r.errors[0].message


def test_paid_status_cannot_hide_a_positive_derived_balance():
    r=read_invoices('generic',export('generic',{'balance':'','status':'paid'},(('amount paid','400'),)))
    assert not r.ok and 'invoice monetary evidence conflicts' in r.errors[0].message


def test_currency_header_and_currency_cell_must_agree():
    text=export('generic').replace('currency,','Currency (USD),')
    r=read_invoices('generic',text)
    assert not r.ok and 'invoice monetary evidence conflicts' in r.errors[0].message


@pytest.mark.parametrize('status,balance',[('open','1200'),('paid','600')])
def test_ingest_cannot_coerce_invalid_preconstructed_financial_records(status,balance):
    from dataclasses import replace
    from cashdesk.importers import ImportResult
    good=read_invoices('generic',export('generic')).records[0]
    bad=ImportResult('invoices','generic',records=(replace(good,status=status,balance=D(balance)),),fingerprint='fake-invalid-record')
    with Store(':memory:') as s:
        with pytest.raises(ConflictError,match='invoice monetary evidence conflicts'):s.ingest(bad)
        assert not s.list_invoices() and not s.has_import(bad.fingerprint)


def test_paid_credit_cannot_ignore_a_recognized_remaining_alias():
    r=read_invoices('xero',export('xero',{'balance':'','status':'paid','type':'ACCRECCREDIT'},(('Open Balance','600'),)))
    assert not r.ok and 'paid credit has a nonzero exported balance' in r.errors[0].message


def test_negative_remaining_on_sale_must_reconcile_without_absolute_value():
    r=read_invoices('generic',export('generic',{'balance':'-600'},(('amount paid','400'),)))
    assert not r.ok and 'invoice monetary evidence conflicts' in r.errors[0].message


def test_legitimate_overpaid_sale_keeps_negative_remaining_and_warning():
    r=read_invoices('generic',export('generic',{'balance':'-200'},(('amount paid','1200'),)))
    assert r.ok and r.records[0].balance==D('-200') and r.warnings


def test_negative_credit_paid_amount_derives_remaining_in_credit_direction():
    r=read_invoices('generic',export('generic',{'amount':'-1000','balance':'','type':'credit note'},(('amount paid','400'),)))
    assert r.ok and r.credit_notes[0].amount==D('600')


def test_paid_credit_with_partial_paid_evidence_cannot_clear_the_remainder():
    r=read_invoices('xero',export('xero',{'balance':'','status':'paid','type':'ACCRECCREDIT'},(('InvoiceAmountPaid','400'),)))
    assert not r.ok and 'paid credit has a nonzero exported balance' in r.errors[0].message
