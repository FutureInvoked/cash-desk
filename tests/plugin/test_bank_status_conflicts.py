"""An import must not report success while dropping contradictory status evidence."""
from tests.plugin.support import load_demo


def bank_file(home,name,headers,values):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/name
    path.write_text('id,date,amount,currency,counterparty,reference,'+headers+'\nPNEW,2026-11-03,1980,EUR,Northgate Labs Ltd,INV-2026-0041,'+values+'\n')
    return path


def test_bank_status_conflicts_cannot_be_bypassed_with_allow_errors(plugin,hermes_fakes):
    module,ctx=plugin;load_demo(ctx)
    path=bank_file(hermes_fakes,'conflict.csv','Status,Transaction Status','Completed,Reversed')
    with module._store() as s:before=(s.list_payments(),s.list_matches(),s.list_imports())
    result=ctx.call('cashdesk_import',kind='bank',path=str(path),allow_errors=True)
    assert not result['ok'] and 'bank status evidence conflicts' in result['error']
    assert 'cannot be partially imported' in result['error']
    assert result['errors'][0]['row']==2
    assert 'transaction status' in result['errors'][0]['message']
    with module._store() as s:assert (s.list_payments(),s.list_matches(),s.list_imports())==before


def test_duplicate_bank_status_headers_show_refusal_and_corrected_file_is_importable(plugin,hermes_fakes):
    module,ctx=plugin
    bad=bank_file(hermes_fakes,'duplicated.csv','Status,status','Failed,Completed')
    result=ctx.call('cashdesk_import',kind='bank',path=str(bad),allow_errors=True)
    assert not result['ok'] and 'repeated status headers' in result['errors'][0]['message']
    good=bank_file(hermes_fakes,'corrected.csv','Status,State','Completed,Posted')
    result=ctx.call('cashdesk_import',kind='bank',path=str(good))
    assert result['ok'] and result['imported']['payments']==1
