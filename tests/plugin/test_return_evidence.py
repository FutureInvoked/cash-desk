"""Return evidence is disclosed, with a human-only verification command."""
from tests.plugin.test_api import client

def test_return_review_status_brief_and_snapshot_checked_human_command(plugin,hermes_fakes):
    module,ctx=plugin
    folder=hermes_fakes/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'returned.csv'
    path.write_text('id,date,amount,currency,counterparty,reference,memo\nR1,2026-11-04,-100,EUR,Alpha Demo,,RETURNED R01\n')
    result=ctx.call('cashdesk_import',kind='bank',path=str(path))
    status=ctx.call('cashdesk_status')
    assert status['return_questions']==1 and status['balances_need_review']
    text=ctx.command('returns');assert 'Raw evidence' in text and 'Snapshot' in text and 'not clear it as unrelated' in text
    assert 'Returned payment review' in ctx.call('cashdesk_brief')['brief']
    assert 'returned payment review' in ctx.command('return-unrelated bad stale bank checked')
    with module._store() as s:q=s.return_reviews()[0]
    assert 'No balances' in ctx.command(f"return-unrelated {q['id']} {q['snapshot']} Verified bank observation is unrelated to retained receipts")
    with module._store() as s:assert not s.return_reviews()


def test_return_only_nonbooked_row_is_visible_in_desktop_without_spendable_payment(plugin,hermes_fakes,client):
    module,ctx=plugin
    folder=hermes_fakes/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'return-status.csv';path.write_text('id,date,amount,currency,status\nR1,,,,Returned\n')
    result=ctx.call('cashdesk_import',kind='bank',path=str(path))
    assert result['ok'] is False and result['quarantined'] and result['imported']['return_questions']==1
    assert any('returned payment review' in w['message'] for w in result['warnings'])
    response=client[0].get('/review').json()
    assert response['return_questions'] and not response['payments']
    with module._store() as s:assert not s.list_payments()


def test_return_only_slash_review_and_match_do_not_claim_an_empty_queue(plugin,hermes_fakes):
    module,ctx=plugin
    folder=hermes_fakes/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'status-only.csv';path.write_text('id,date,amount,currency,status\nR1,,,,Returned\n')
    ctx.call('cashdesk_import',kind='bank',path=str(path))
    assert '/cashdesk returns' in ctx.command('review')
    assert ctx.call('cashdesk_match')['return_questions']


def test_statement_with_receipt_and_its_return_applies_nothing_until_verified(plugin,hermes_fakes,client):
    module,ctx=plugin
    folder=hermes_fakes/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    (folder/'customers.csv').write_text('id,name,email,contact_name\nacme,Acme Ltd,ap@acme.example,Ann Able\n')
    (folder/'invoices.csv').write_text('number,customer_id,issue_date,due_date,amount,currency,balance,status,reference\nINV-1001,acme,2026-10-01,2026-10-22,1200.00,EUR,1200.00,open,INV-1001\n')
    (folder/'bank.csv').write_text('id,date,amount,currency,counterparty,reference,memo\nP1,2026-11-02,1200.00,EUR,Acme Ltd,INV-1001,\nR1,2026-11-04,-1200.00,EUR,Acme Ltd,INV-1001,RETURNED R01\n')
    for kind,name in (('customers','customers.csv'),('invoices','invoices.csv'),('bank','bank.csv')):
        ctx.call('cashdesk_import',kind=kind,path=str(folder/name))
    matched=ctx.call('cashdesk_match')
    assert not matched.get('applied')
    with module._store() as s:
        assert s.get_invoice('INV-1001').balance==s.get_invoice('INV-1001').amount and not s.list_matches()
        q=s.return_reviews()[0]
    assert q['quarantined']==['P1']
    payments=client[0].get('/review').json()['payments']
    p1=[p for p in payments if p['id']=='P1']
    assert p1 and not p1[0]['incoming']
    assert 'may have been returned' in client[0].post('/payments/P1/apply',json={'invoice_number':'INV-1001'}).text
    listing=ctx.command('returns');assert 'return-reverses' in listing and 'P1' in listing
    assert 'returned payment review' in ctx.command(f"return-reverses {q['id']} P1 stale bank checked")
    assert 'not received' in ctx.command(f"return-reverses {q['id']} P1 {q['snapshot']} Bank statement shows R1 is the R01 return of P1")
    with module._store() as s:
        assert not s.return_reviews() and not s.list_matches() and s.get_invoice('INV-1001').status=='open'
