"""A lifecycle field cannot hide a populated contradictory bank status."""
import csv
import io
import pytest
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError
from tests.test_receipt_lifecycle import setup, bank, plan, DAY


def statement(headers,values,*,pid='P2',amount='300',reference='INV-B1',payer='Beta Demo'):
    out=io.StringIO();w=csv.writer(out,lineterminator='\n')
    w.writerow(['id','date','amount','currency','counterparty','reference',*headers])
    w.writerow([pid,'2026-11-03',amount,'EUR',payer,reference,*values]);return out.getvalue()


@pytest.mark.parametrize('alias',['State','Transaction Status','Booking Status'])
@pytest.mark.parametrize('first,second',[('Completed','Reversed'),('posted','Failed'),('booked','Pending'),('Returned','Completed'),('Pending','Completed'),('RefuNded','Complete')])
def test_all_bank_status_aliases_must_agree_before_cash_is_accepted(alias,first,second):
    r=read_bank_csv(statement(['Status',alias],[first,second]))
    assert not r.ok and not r.records and not r.bank_observations
    assert 'bank status evidence conflicts' in r.errors[0].message
    assert r.errors[0].row==2 and first in r.errors[0].raw and second in r.errors[0].raw


@pytest.mark.parametrize('headers',[['Status','status'],['Transaction Status','transaction_status'],['STATE','state'],['Booking Status','booking-status']])
def test_duplicate_normalized_bank_status_headers_refuse_whole_file(headers):
    r=read_bank_csv(statement(headers,['Failed','Completed']))
    assert not r.ok and not r.records
    assert 'bank status evidence conflicts' in r.errors[0].message and 'repeated' in r.errors[0].message


@pytest.mark.parametrize('second',['Completed','posted','settled','cleared','gebucht','zaúčtováno','geboekt'])
def test_equivalent_booked_status_aliases_remain_spendable(second):
    r=read_bank_csv(statement(['Status','State'],['Completed',second]))
    assert r.ok and len(r.records)==1


@pytest.mark.parametrize('first,second',[('pending','processing'),('in progress','scheduled'),('cancelled','voided'),('reverted','reversed'),('returned','reverted'),('failed','declined'),('refunded','refunded')])
def test_equivalent_nonbooked_aliases_retain_lifecycle_without_money(first,second):
    r=read_bank_csv(statement(['Status','State'],[first,second]))
    assert r.ok and not r.records and len(r.bank_observations)==1
    assert r.skipped_counts=={'not_booked':1}
    assert first in r.bank_observations[0].raw and second in r.bank_observations[0].raw


@pytest.mark.parametrize('value',['Pending','FAILED','Returned','Voided','refunded','In Progress','Abgelehnt','Vorgemerkt'])
def test_supported_single_nonbooked_statuses_retain_evidence(value):
    r=read_bank_csv(statement(['Status'],[value]))
    assert r.ok and not r.records and len(r.bank_observations)==1


@pytest.mark.parametrize('values',[['','Completed'],['Completed',''],['','pending']])
def test_empty_secondary_or_primary_alias_does_not_hide_populated_evidence(values):
    r=read_bank_csv(statement(['Status','State'],values))
    assert r.ok,r.errors
    assert bool(r.records)==('Completed' in values)


@pytest.mark.parametrize('values',[['Completed','mystery'],['',''],['Pending','Returned'],['Failed','Refunded']])
def test_unknown_empty_or_conflicting_statuses_are_non_bypassable(values):
    s,d=setup();history=s.list_matches();payments=s.list_payments();imports=s.list_imports()
    r=read_bank_csv(statement(['Status','State'],values))
    assert not r.ok
    with pytest.raises(ConflictError,match='bank status evidence conflicts'):s.ingest(r,allow_errors=True)
    assert s.list_matches()==history and s.list_payments()==payments and s.list_imports()==imports
    assert s.approval(d['A'].id)


def test_one_bad_status_row_prevents_good_rows_from_a_partial_success():
    text=statement(['Status','State'],['Completed','Completed'])
    text+='P3,2026-11-04,700,EUR,Gamma Workshop,INV-C1,Completed,Failed\n'
    r=read_bank_csv(text);assert not r.ok and not r.records and r.errors[0].row==3
    s,d=setup();before=s.list_payments()
    with pytest.raises(ConflictError,match='bank status evidence conflicts'):s.ingest(r,allow_errors=True)
    assert s.list_payments()==before


def test_concordant_adverse_update_reaches_existing_receipt_history_and_revokes_thanks():
    s,d=setup();matches=s.list_matches()
    r=read_bank_csv(statement(['Status','State'],['Returned','Reversed'],pid='P1',amount='1200',reference='INV-A1',payer='Alpha Demo'))
    assert r.ok
    s.ingest(r)
    assert s.receipt_conflicts() and s.list_matches()==matches
    assert not s.verified_matches() and not plan(s).thanks
    assert s.approval(d['A'].id) is None
    with pytest.raises(ConflictError):s.reserve_send(d['A'].id,d['A'].hash,'eml')
    assert s.reserve_send(d['C'].id,d['C'].hash,'eml')


def test_agreeing_booked_aliases_preserve_refund_direction():
    r=read_bank_csv('id,date,amount,Status,State,reference,counterparty\nR1,2026-11-03,-1200,Posted,Completed,INV-A1,Alpha Demo\n')
    assert r.ok and r.records[0].amount<0


def test_reordered_alias_columns_do_not_change_lifecycle_identity():
    left=read_bank_csv(statement(['Status','State'],['returned','reversed']))
    right=read_bank_csv(statement(['State','Status'],['reversed','returned']))
    assert left.ok and right.ok and not left.records and not right.records
    assert left.bank_observations[0].status==right.bank_observations[0].status
