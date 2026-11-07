"""Surplus warnings and human-only accounting verification are visible."""
from tests.plugin.support import load_demo


def test_surplus_import_status_brief_and_human_review_are_consistent(plugin,hermes_fakes):
    module,ctx=plugin;load_demo(ctx)
    folder=hermes_fakes/'plugin-data'/'cash-desk';p=folder/'surplus.csv'
    p.write_text('number,customer_id,issue_date,due_date,amount,balance,currency,status\nINV-SURPLUS,C001,2026-10-01,2026-10-20,100,-10,EUR,paid\n')
    result=ctx.call('cashdesk_import',kind='invoices',source='generic',path=str(p),open_list=False)
    assert result['ok'] and result['imported']['surplus_questions']==1
    assert any('customer surplus review' in x['message'] for x in result['warnings'])
    assert ctx.call('cashdesk_status')['surplus_questions']==1
    assert 'surpluses' in ctx.command('status')
    assert 'Customer surplus review' in ctx.call('cashdesk_brief')['brief']
    text=ctx.command('surpluses');assert 'Raw observations' in text and 'Settlement basis' in text and 'NOT summed' in text
    with module._store() as s:q=s.surplus_reviews()[0]
    assert ctx.command(f"surplus-accounted {q['number']} bad").startswith('Refused:')
    result=ctx.command(f"surplus-accounted {q['number']} {q['snapshot']}")
    assert 'No money' in result and 'old approval stays revoked' in result
    assert ctx.command('surpluses')=='No customer surplus questions await verification.'


def test_surplus_verification_is_not_an_agent_write_tool(plugin):
    module,ctx=plugin
    assert 'cashdesk_surplus_accounted' not in ctx.tools
    assert ctx.command('surplus-accounted').startswith('Usage:')
