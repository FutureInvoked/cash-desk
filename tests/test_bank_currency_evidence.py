"""Explicit statement currency never silently becomes configured-default cash."""
import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from cashdesk import Customer, Invoice, Store
from cashdesk.importers import read_bank_csv
from cashdesk.store import ConflictError
from tests.test_receipt_lifecycle import setup, plan, DAY


HEADER='id,date,amount,counterparty,reference\n'
ROW='P1,2026-11-03,1200,Alpha Demo,INV-A1\n'


@pytest.mark.parametrize('preamble', ['Currency,GBP\n','Account currency;GBP\n','Statement Currency: GBP\n',
                                    'Currency = GBP\n','Měna účtu;GBP\n','Currency (GBP)\n',
                                    'Account Number,Account Currency\n123,GBP\n'])
def test_explicit_statement_currency_overrides_only_the_default(preamble):
    result=read_bank_csv(preamble+HEADER+ROW,default_currency='EUR')
    assert result.ok and result.records[0].currency=='GBP'
    assert not result.bank_currency_issues
    assert result.bank_observations[0].evidence['currency_basis']['resolved']=='GBP'


@pytest.mark.parametrize('text', [
    'Currency,GBP\n'+HEADER.replace('amount','amount (EUR)')+ROW,
    'Currency,GBP\n'+HEADER.replace('amount','amount,currency')+ROW.replace('1200','1200,EUR'),
    'Currency,GBP\n'+HEADER+ROW.replace('1200','EUR 1200'),
    'Currency,GBP\nAccount currency,EUR\n'+HEADER+ROW,
    'Currency,GBP,EUR\n'+HEADER+ROW,
    HEADER.replace('amount','amount,currency,Currency')+ROW.replace('1200','1200,EUR,GBP'),
    'Currency,not verified\n'+HEADER+ROW,
    'Currency,$\n'+HEADER+ROW,
    'Currency,\n'+HEADER+ROW,
    HEADER.replace('amount','amount,balance (GBP)')+ROW.replace('1200','1200,2000'),
])
def test_conflicting_or_unresolved_bank_currency_keeps_raw_review_evidence(text):
    result=read_bank_csv(text)
    assert not result.records and not result.bank_observations
    assert result.errors[0].code=='bank_currency_conflict'
    assert 'bank currency evidence conflicts' in result.errors[0].message
    assert result.bank_currency_issues[0].raw
    evidence=json.loads(result.bank_currency_issues[0].evidence)
    assert evidence['kind']=='currency' and evidence['headers'] and evidence['cells']


@pytest.mark.parametrize('text,currency', [
    (HEADER+ROW,'CZK'),
    (HEADER.replace('amount','amount (GBP)')+ROW,'GBP'),
    (HEADER+ROW.replace('1200','GBP 1200'),'GBP'),
    (HEADER.replace('amount','amount,currency')+ROW.replace('1200','1200,GBP'),'GBP'),
    ('Currency,GBP\nAccount currency,gbp\n'+HEADER+ROW,'GBP'),
    (HEADER.replace('amount','amount,currency,Currency')+ROW.replace('1200','1200,GBP,GBP'),'GBP'),
    (HEADER.replace('amount','amount,currency,balance (USD)')+ROW.replace('1200','1200,GBP,5000'),'GBP'),
])
def test_known_native_currency_and_explicit_configured_fallback_remain_usable(text,currency):
    result=read_bank_csv(text,default_currency='CZK')
    assert result.ok and result.records[0].currency==currency
    basis=result.bank_observations[0].evidence['currency_basis']
    if text==HEADER+ROW:
        assert basis['basis']=='configured_default'
        assert any(w.code=='bank_currency_default' and 'configured default CZK' in w.message for w in result.warnings)


def test_gbp_metadata_cannot_settle_eur_debt_or_create_thanks():
    previous,_=setup(applied=False)
    s=Store(':memory:',today=lambda:DAY)
    s.upsert_customers(previous.list_customers());s.upsert_invoices(previous.list_invoices())
    result=read_bank_csv('Currency,GBP\n'+HEADER+ROW.replace('P1','GBP1'))
    s.ingest(result)
    assert not s.match_result(DAY).applied and not s.verified_matches() and not plan(s).thanks
    assert s.get_invoice('INV-A1').balance==D('1200')


def test_partial_currency_failure_revokes_collection_and_thanks_without_undo():
    s,drafts=setup();balances=s.list_invoices();matches=s.list_matches();payments=s.list_payments()
    result=read_bank_csv('Currency,GBP\n'+HEADER.replace('amount','amount,currency')+ROW.replace('1200','1200,EUR'))
    with pytest.raises(ConflictError):s.ingest(result)
    counts=s.ingest(result,allow_errors=True,file_name='currency.csv')
    assert counts['bank_evidence_questions']==1 and not s.verified_matches()
    assert s.list_invoices()==balances and s.list_matches()==matches and s.list_payments()==payments
    assert not plan(s).thanks and not plan(s).drafts
    for draft in drafts.values():
        assert s.approval(draft.id) is None
        with pytest.raises(ConflictError,match='bank currency evidence conflicts'):s.approve_draft(draft.id,draft.hash)
        with pytest.raises(ConflictError,match='bank currency evidence conflicts'):s.reserve_send(draft.id,draft.hash,'eml')
    q=s.bank_evidence_questions()[0]
    assert q['evidence']['kind']=='currency' and 'GBP' in str(q['evidence'])
    with pytest.raises(ConflictError,match='bank currency evidence conflicts'):
        s.confirm_bank_evidence(q['id'],expected_snapshot='stale',reason='Verified bank',by='test')
    s.confirm_bank_evidence(q['id'],expected_snapshot=q['snapshot'],reason='Verified original bank and ledger: conflicting source row is not additional cash',by='test')
    assert not s.bank_evidence_questions() and all(s.approval(d.id) is None for d in drafts.values())


@pytest.mark.parametrize('provided',[True,False])
def test_changed_currency_metadata_holds_old_receipts_including_derived_identity(provided):
    s,drafts=setup();header=HEADER if provided else HEADER.removeprefix('id,');row=ROW if provided else ROW.removeprefix('P1,')
    s.ingest(read_bank_csv('Currency,EUR\n'+header+row),file_name='old.csv')
    matches=s.list_matches();balances=s.list_invoices()
    s.ingest(read_bank_csv('Currency,GBP\n'+header+row),file_name='corrected.csv')
    assert s.bank_evidence_questions() and not s.verified_matches()
    assert s.list_matches()==matches and s.list_invoices()==balances
    assert all(s.approval(d.id) is None for d in drafts.values())


def test_preexisting_import_can_be_reobserved_to_retain_correct_currency_without_undo():
    s,drafts=setup();current=read_bank_csv('Currency,GBP\n'+HEADER+ROW)
    old=read_bank_csv(HEADER+ROW)
    # Simulate an old reader that did not retain currency provenance.
    old=replace(old,fingerprint=current.fingerprint,bank_observations=tuple(replace(o,evidence=None) for o in old.bank_observations))
    s.ingest(old)
    matches=s.list_matches();balances=s.list_invoices()
    s.ingest(current)
    assert s.bank_evidence_questions() and not s.verified_matches()
    assert s.list_matches()==matches and s.list_invoices()==balances
    assert all(s.approval(d.id) is None for d in drafts.values())


def test_horizontal_currency_metadata_keeps_the_actual_account_identity():
    a=read_bank_csv('Account Number,Account Currency\n123,GBP\n'+HEADER+ROW)
    b=read_bank_csv('Account Number,Account Currency\n456,GBP\n'+HEADER+ROW)
    assert a.bank_observations[0].account=='account:123'
    assert b.bank_observations[0].account=='account:456'


def test_currency_provenance_survives_reopen_and_is_available_for_review(tmp_path):
    path=tmp_path/'scratch.sqlite'
    with Store(path,today=lambda:DAY) as s:
        s.ingest(read_bank_csv('Currency,GBP\n'+HEADER+ROW))
    with Store(path,today=lambda:DAY) as s:
        values=s.receipt_currency_evidence('P1')
        assert values and values[0]['currency']=='GBP'
        assert 'Currency,GBP' in str(values[0]['evidence']['preamble'])


@pytest.mark.parametrize('currency',['USD','CAD','AUD','NZD'])
def test_dollar_symbol_requires_and_respects_an_explicit_native_code(currency):
    text=HEADER.replace('amount','amount,currency')+ROW.replace('1200',f'$1200,{currency}')
    result=read_bank_csv(text)
    assert result.ok and result.records[0].currency==currency
    unresolved=read_bank_csv(HEADER+ROW.replace('1200','$1200'),default_currency=currency)
    assert unresolved.errors[0].code=='bank_currency_conflict' and not unresolved.records


def test_receipt_update_snapshot_binds_retained_currency_provenance():
    s,_=setup()
    s.ingest(read_bank_csv('Currency,GBP\n'+HEADER+ROW))
    q=s.receipt_conflicts()[0]
    s._conn.execute("UPDATE bank_currency_evidence SET evidence=? WHERE payment_id=?",(json.dumps({'resolved':'EUR','basis':'changed'}),q['original']))
    with pytest.raises(ConflictError,match='receipt update snapshot changed'):
        s.resolve_receipt_update(q['original'],q['observation'],'keep',expected_snapshot=q['snapshot'],by='test')


@pytest.mark.parametrize('metadata',['Currency\n','Account Number,Account Currency\n','Currency unknown units\n'])
def test_unfinished_or_unrecognized_labelled_statement_currency_never_becomes_default(metadata):
    result=read_bank_csv(metadata+HEADER+ROW)
    assert result.errors and result.errors[0].code=='bank_currency_conflict' and not result.records


@pytest.mark.parametrize('header',['amount,currency (GBP)','amount,account (GBP)'])
def test_explicit_currency_and_account_header_annotations_establish_the_basis(header):
    result=read_bank_csv(HEADER.replace('amount',header)+ROW.replace('1200','1200,'))
    assert result.ok and result.records[0].currency=='GBP'


def test_currency_uncertainty_preserves_sent_history_and_cannot_reacknowledge():
    s,drafts=setup();d=drafts['A'];token=s.reserve_send(d.id,d.hash,'eml');s.confirm_send(token)
    sent=s.list_sends();matches=s.list_matches()
    text='Currency,GBP\n'+HEADER.replace('amount','amount,currency')+ROW.replace('1200','1200,EUR')
    s.ingest(read_bank_csv(text),allow_errors=True)
    assert s.bank_evidence_questions() and s.list_sends()==sent and s.list_matches()==matches
    assert s.get_draft(d.id).status=='sent' and not plan(s).thanks


@pytest.mark.parametrize('label',['AccountCurrency','StatementCurrency','CurrencyCode','TransactionCurrency','CCY'])
def test_joined_and_abbreviated_explicit_currency_labels_do_not_fall_back(label):
    result=read_bank_csv(label+',GBP\n'+HEADER+ROW,default_currency='EUR')
    assert result.ok and result.records[0].currency=='GBP'


@pytest.mark.parametrize('value',['USD1200EUR','USD invalid','GBP 1200 EUR'])
def test_malformed_amounts_cannot_discard_explicit_conflicting_currency_tokens(value):
    text=HEADER.replace('amount','amount,currency')+ROW.replace('1200',value+',EUR')
    result=read_bank_csv(text)
    assert result.errors[0].code=='bank_currency_conflict' and result.bank_currency_issues
    s=Store(':memory:',today=lambda:DAY);s.ingest(result,allow_errors=True)
    assert s.bank_evidence_questions() and not s.list_payments()
