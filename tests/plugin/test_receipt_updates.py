"""Import response and human-only conflict recovery are explicit."""
from decimal import Decimal as D


def write_bank(ctx,home,name,amount,status='Completed'):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/name
    path.write_text(f'id,date,amount,currency,counterparty,reference,status\nP1,2026-11-03,{amount},EUR,Alpha Demo,INV-A1,{status}\n')
    return ctx.call('cashdesk_import',kind='bank',path=str(path))


def test_conflicting_import_reports_quarantine_and_human_resolution(plugin,hermes_fakes):
    module,ctx=plugin
    assert write_bank(ctx,hermes_fakes,'first.csv','100')['ok']
    result=write_bank(ctx,hermes_fakes,'changed.csv','200')
    assert result['ok'] is False and result['quarantined']
    assert any('receipt update conflict' in w['message'] for w in result['warnings'])
    text=ctx.command('receipt-updates')
    assert 'Raw evidence' in text and 'Allocations' in text and 'receipt-update' in text
    with module._store() as s:q=s.receipt_conflicts()[0]
    resolved=ctx.command(f"receipt-update {q['original']} {q['observation']} replace {q['snapshot']}")
    assert 'No allocation was reversed' in resolved
    with module._store() as s:
        assert not s.receipt_conflicts() and len(s.unapplied_payments())==1
        assert s.unapplied_payments()[0].amount==D('200')


def test_unallocated_status_update_is_visible_and_cannot_be_dismissed_as_money(plugin,hermes_fakes):
    module,ctx=plugin
    assert write_bank(ctx,hermes_fakes,'first.csv','100')['ok']
    assert not write_bank(ctx,hermes_fakes,'reverted.csv','100','Reverted')['ok']
    with module._store() as s:
        review=s.match_result().review
        assert review[0].kind=='RECEIPT_UPDATE'
    assert ctx.command('dismiss P1').startswith('Refused:')


def test_status_and_brief_warn_that_retained_balances_are_uncertain(plugin,hermes_fakes):
    module,ctx=plugin
    assert write_bank(ctx,hermes_fakes,'first.csv','100')['ok']
    assert not write_bank(ctx,hermes_fakes,'reverted.csv','100','Reverted')['ok']
    status=ctx.call('cashdesk_status')
    assert status['receipt_update_questions']==1 and status['balances_need_review']
    assert 'receipt-updates' in ctx.command('status')
    brief=ctx.call('cashdesk_brief')['brief']
    assert 'Receipt update conflict' in brief and 'receipt-updates' in brief
