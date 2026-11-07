"""Reference proof is visible and enforced through the real plugin adapters."""
import pytest
from tests.plugin.test_api import client


def load(ctx,home):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    texts={
        'customers':'customer_id,name,email\nA,Alpha Demo,ap@alpha.example\n',
        'invoices':'number,customer_id,issue_date,due_date,amount,balance,currency\nINV-1001,A,2026-10-01,2026-10-20,1200,1200,EUR\n',
        'bank':'id,date,amount,currency,counterparty,reference\nP1,2026-11-03,1200,EUR,Alpha Demo,INV-1001 and INV-9999\n',
    }
    for kind,text in texts.items():
        path=folder/(kind+'.csv');path.write_text(text)
        assert ctx.call('cashdesk_import',kind=kind,path=str(path))['ok']


def test_reference_unknowns_are_visible_before_any_automatic_cash_or_thanks(plugin,hermes_fakes,client):
    module,ctx=plugin;load(ctx,hermes_fakes)
    result=ctx.call('cashdesk_match',as_of='2026-11-10')
    assert not result['applied'] and result['review'][0]['kind']=='REFERENCE'
    assert 'INV-9999' in result['review'][0]['unknown_references']
    assert 'reference evidence needs review' in ctx.command('review')
    body=client[0].get('/review').json()['payments'][0]
    assert body['unknown_references']==['INV-9999'] and body['named']==['INV-1001']
    assert body['incoming']  # Human placement stays available after bank verification.
    with module._store() as s:assert not s.list_matches() and not s.verified_matches()
    assert 'paid in full' in ctx.command('apply P1 INV-1001').lower()
    with module._store() as s:
        assert s.get_invoice('INV-1001').balance==0 and s.list_matches()[0].method=='person'


def test_historical_reference_questions_disclose_raw_evidence_and_have_no_apply_action(plugin,hermes_fakes,client):
    module,ctx=plugin;load(ctx,hermes_fakes)
    with module._store() as s:
        s._conn.execute('INSERT INTO matches VALUES (?,?,?,?,?,?,?,?)',('P1','INV-1001','1200.00','EUR','reference+exact','high','old choice','2026-11-03T12:00:00+00:00'))
        s._conn.execute("UPDATE invoices SET balance='0.00',status='paid' WHERE number='INV-1001'")
    status=ctx.call('cashdesk_status')
    assert status['bank_evidence_questions']==1 and status['balances_need_review']
    assert 'Reference evidence needs review' in ctx.call('cashdesk_brief')['brief']
    text=ctx.command('bank-evidence')
    assert 'bank_observations' in text and 'INV-9999' in text and 'allocations' in text
    body=client[0].get('/review').json()
    assert not body['payments'] and body['bank_evidence_questions'][0]['evidence']['kind']=='reference'
    assert 'Refused:' in ctx.command('bank-evidence-verify stale stale Bank checked')
