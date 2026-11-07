"""Failed partial imports are visible and recoverable only with fresh evidence."""
from tests.plugin.support import load_demo


def failed_import(ctx,home):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    p=folder/'partial.csv'
    p.write_text('number,customer_id,issue_date,due_date,amount,balance,currency,status\nINV-2026-0041,C001,2026-10-01,2026-10-29,1980,bad,EUR,paid\n')
    return ctx.call('cashdesk_import',kind='invoices',source='generic',path=str(p),allow_errors=True,open_list=False)


def test_invoice_import_uncertainty_is_visible_and_requires_verified_recovery(plugin,hermes_fakes):
    module,ctx=plugin;load_demo(ctx)
    result=failed_import(ctx,hermes_fakes)
    assert not result['ok'] and result['quarantined']
    assert result['imported']['invoice_import_questions']==1
    assert any('invoice import uncertainty' in w['message'] for w in result['warnings'])
    text=ctx.command('import-errors');assert 'Raw evidence' in text and 'partial.csv' in text
    assert 'invoice import uncertainty' in ctx.call('cashdesk_brief')['brief'].lower()
    status=ctx.call('cashdesk_status');assert status['invoice_import_questions']==1 and status['balances_need_review']
    assert 'import-errors' in ctx.command('status')
    with module._store() as s:q=s.invoice_import_questions()[0]
    assert ctx.command(f"import-error {q['id']} confirm stale").startswith('Refused:')
    assert 'No financial rows changed' in ctx.command(f"import-error {q['id']} confirm {q['snapshot']}")
    assert ctx.command('import-errors')=='No invoice import errors await verification.'


def test_invoice_import_error_confirmation_has_no_agent_write_tool(plugin):
    module,ctx=plugin
    assert 'cashdesk_import_error_confirm' not in ctx.tools
    assert ctx.command('import-error').startswith('Usage:')
