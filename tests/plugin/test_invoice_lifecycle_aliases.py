"""Registered fake handlers preserve old financial history but revoke authority."""
import pytest
from tests.test_invoice_lifecycle_aliases import export


def write(ctx,root,name,text,kind='invoices',**kw):
    path=root/'plugin-data'/'cash-desk'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    return ctx.call('cashdesk_import',kind=kind,path=str(path),**kw)


@pytest.mark.parametrize('applied',[False,True])
def test_registered_partial_lifecycle_conflict_revokes_old_reminder_or_thanks(plugin,hermes_fakes,applied):
    module,ctx=plugin
    assert write(ctx,hermes_fakes,'customers.csv','customer_id,name,email\nA,Alpha Studio,a@alpha.example\n',kind='customers')['ok']
    assert write(ctx,hermes_fakes,'valid.csv',export(other='open'))['ok']
    if applied:
        assert write(ctx,hermes_fakes,'bank.csv','id,date,amount,currency,counterparty,reference\nP1,2026-11-05,100,EUR,Alpha Studio,INV-A\n',kind='bank')['ok']
        assert ctx.call('cashdesk_match')['applied']
    d=ctx.draft_on('2026-11-06')['drafts'][0]
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    with module._store() as s:before=s.get_invoice('INV-A');history=s.list_matches()
    result=write(ctx,hermes_fakes,'conflict.csv',export(),allow_errors=True)
    assert not result['ok'] and result['quarantined'] and result['imported']['invoice_import_questions']==1
    text=ctx.command('import-errors');assert 'invoice lifecycle evidence conflicts' in text and 'Raw evidence' in text
    result=ctx.call('cashdesk_send',draft_id=d['id'],draft_hash=d['hash'])
    assert not result['ok'] and 'invoice import uncertainty' in result['error']
    with module._store() as s:
        assert s.get_invoice('INV-A')==before and s.list_matches()==history and s.approval(d['id']) is None
    assert not list(hermes_fakes.rglob('*.eml'))
