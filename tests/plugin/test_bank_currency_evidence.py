"""Currency questions and configured fallbacks are visible at the plugin boundary."""
from tests.plugin.test_api import client


def import_bank(ctx,home,text,allow_errors=False):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'currency.csv';path.write_text(text)
    return ctx.call('cashdesk_import',kind='bank',path=str(path),allow_errors=allow_errors)


def test_currency_conflict_is_visible_and_snapshot_verified_without_new_agent_authority(plugin,hermes_fakes,client):
    module,ctx=plugin
    text='Currency,GBP\nid,date,amount,currency,counterparty,reference\nP1,2026-11-03,1200,EUR,Alpha Demo,INV-A1\n'
    result=import_bank(ctx,hermes_fakes,text,True)
    assert not result['ok'] and result['quarantined'] and result['imported']['payments']==0
    assert any('bank currency evidence conflicts' in w['message'] for w in result['warnings'])
    assert ctx.call('cashdesk_status')['balances_need_review']
    assert 'Bank currency evidence conflicts' in ctx.call('cashdesk_brief')['brief']
    assert 'Bank currency evidence conflicts' in ctx.command('review')
    assert 'Currency,GBP' in ctx.command('bank-evidence')
    assert client[0].get('/review').json()['bank_evidence_questions'][0]['evidence']['kind']=='currency'
    with module._store() as s:q=s.bank_evidence_questions()[0]
    assert 'Refused:' in ctx.command(f"bank-evidence-verify {q['id']} stale Verified bank")
    assert 'No balances, allocations or sent history changed' in ctx.command(f"bank-evidence-verify {q['id']} {q['snapshot']} Bank and current ledger independently verified; row is not new cash")
    assert not any('currency_verify' in name for name in ctx.tools)


def test_genuinely_currencyless_import_discloses_its_configured_default(plugin,hermes_fakes):
    module,ctx=plugin
    text='id,date,amount,counterparty,reference\nP1,2026-11-03,1200,Alpha Demo,INV-A1\n'
    refused=import_bank(ctx,hermes_fakes,text)
    assert refused['ok'] is False and 'has no currency column or currency in its headers' in refused['error']
    folder=hermes_fakes/'plugin-data'/'cash-desk'
    result=ctx.call('cashdesk_import',kind='bank',path=str(folder/'currency.csv'),default_currency='EUR')
    assert result['ok'] and any('configured default EUR' in w['message'] for w in result['warnings'])
    with module._store() as s:
        assert s.receipt_currency_evidence('P1')[0]['evidence']['basis']=='configured_default'


def test_demo_currency_label_is_recorded_as_demo_provenance(plugin):
    module,ctx=plugin
    import importlib
    paths=importlib.import_module(module.__package__+'.paths')
    result=module._engine().read_bank_csv('id,date,amount,currency\nP1,2026-11-03,1200,EUR\n')
    converted=paths.in_currency(result,'CZK')
    basis=converted.bank_observations[0].evidence['currency_basis']
    assert basis['resolved']=='CZK' and basis['basis']=='demo_label' and basis['original_currency']=='EUR'
