"""Raw bank alias questions are visible and verification stays human-only."""
from tests.plugin.test_api import client


def bad_import(ctx,home,allow_errors=True):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'bank-aliases.csv'
    path.write_text('id,date,amount,currency,counterparty,payer name,reference,payment reference\nP1,2026-11-03,1200,EUR,Acme Ltd,Beta GmbH,INV-1,INV-2\n')
    return ctx.call('cashdesk_import',kind='bank',path=str(path),allow_errors=allow_errors)


def test_bank_alias_partial_import_is_visible_in_tools_brief_and_human_review(plugin,hermes_fakes,client):
    module,ctx=plugin
    refused=bad_import(ctx,hermes_fakes,False)
    assert not refused['ok'] and 'bank payer/reference evidence conflicts' in refused['errors'][0]['message']
    with module._store() as s:assert not s.bank_evidence_questions()
    result=bad_import(ctx,hermes_fakes)
    assert not result['ok'] and result['quarantined'] and result['imported']['payments']==0
    assert result['imported']['bank_evidence_questions']==1
    assert 'bank payer/reference evidence conflicts' in result['warnings'][0]['message']
    status=ctx.call('cashdesk_status')
    assert status['bank_evidence_questions']==1 and status['balances_need_review']
    assert '/cashdesk bank-evidence' in ctx.call('cashdesk_brief')['brief']
    assert '/cashdesk bank-evidence' in ctx.command('status') and '/cashdesk bank-evidence' in ctx.command('review')
    text=ctx.command('bank-evidence')
    assert all(word in text for word in ['Raw evidence','Original cells','Acme Ltd','Beta GmbH','INV-1','INV-2','bank-aliases.csv'])
    assert ctx.call('cashdesk_match')['bank_evidence_questions']
    body=client[0].get('/review').json()
    assert body['bank_evidence_questions'] and not body['payments']
    assert client[0].get('/status').json()['balances_need_review']
    with module._store() as s:q=s.bank_evidence_questions()[0]
    assert 'Refused:' in ctx.command(f"bank-evidence-verify {q['id']} stale Verified original bank")
    # Read a fresh token after the brief/matching actions.
    with module._store() as s:q=s.bank_evidence_questions()[0]
    assert 'No balances, allocations or sent history changed' in ctx.command(f"bank-evidence-verify {q['id']} {q['snapshot']} Verified original bank and current ledger; this row is not new cash")
    assert ctx.command('bank-evidence')=='No bank payer/reference questions await verification.'


def test_bank_alias_verification_has_no_agent_write_tool(plugin):
    module,ctx=plugin
    assert not any('bank_evidence' in name for name in ctx.tools)
    assert ctx.command('bank-evidence-verify').startswith('Usage:')
