"""Settlement basis is exercised through the real offline handlers."""
from tests.plugin.test_eligibility_lifecycle import import_file

HEADER = 'invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference,iban\n'
ROW = 'INV-2026-1001,C1,2026-10-01,2026-10-22,1000,EUR,{balance},open,20261001,DE89370400440532013000\n'


def start(ctx, root):
    import_file(ctx, root, 'contacts.csv', 'customer_id,name,email\nC1,Acme Ltd,ap@acme.example\n', kind='customers')
    import_file(ctx, root, 'invoices.csv', HEADER + ROW.format(balance=600))


def pay(ctx, root, value):
    import_file(ctx, root, 'bank.csv', 'id,date,amount,currency,counterparty,reference\nP1,2026-11-01,' + str(value) + ',EUR,Acme Ltd,20261001\n', kind='bank')
    if value == 600:
        assert ctx.call('cashdesk_match')['ok']
    else:
        assert ctx.command('apply P1 INV-2026-1001 ' + str(value)).startswith('Applied')


def test_initial_settlement_plus_local_payment_never_reopens_or_exports(plugin, hermes_fakes):
    module, ctx = plugin
    start(ctx, hermes_fakes)
    pay(ctx, hermes_fakes, 600)
    import_file(ctx, hermes_fakes, 'stale.csv', HEADER + ROW.format(balance='600.00'))
    with module._store() as s:
        inv = s.get_invoice('INV-2026-1001')
        assert (str(inv.balance), inv.status) == ('0.00', 'paid')
    assert ctx.call('cashdesk_draft', as_of='2026-11-10')['ok']
    drafts = ctx.call('cashdesk_queue')['drafts']
    assert not [d for d in drafts if d['kind'].startswith('stage')]
    assert not list(hermes_fakes.rglob('*.eml'))


def test_changed_export_settlement_requires_human_reconciliation(plugin, hermes_fakes):
    module, ctx = plugin
    start(ctx, hermes_fakes)
    pay(ctx, hermes_fakes, 100)
    ctx.call('cashdesk_draft', as_of='2026-11-10')
    draft = next(d for d in ctx.call('cashdesk_queue')['drafts'] if d['kind'].startswith('stage'))
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith('Approved')
    result = import_file(ctx, hermes_fakes, 'ambiguous.csv', HEADER + ROW.format(balance=500))
    assert result['imported']['settlement_review'] == 1
    with module._store() as s:
        assert s.approval(draft['id']) is None
    result = ctx.call('cashdesk_send', draft_id=draft['id'], draft_hash=draft['hash'])
    assert not result['ok'] and 'settlement reconciliation required' in result['error']
    assert not list(hermes_fakes.rglob('*.eml'))
    review = ctx.command('settlements')
    assert 'INV-2026-1001' in review and 'excluding' in review
    token = review.split('snapshot ')[1].split()[0]
    assert 'snapshot changed' in ctx.command('reconcile INV-2026-1001 400 stale-token')
    resolved = ctx.command(f'reconcile INV-2026-1001 400 {token}')
    assert 'EUR 500.00' in resolved
    assert not ctx.call('cashdesk_send', draft_id=draft['id'], draft_hash=draft['hash'])['ok']
    assert ctx.call('cashdesk_draft', as_of='2026-11-10')['ok']
    with module._store() as s:
        assert not s.settlement_reviews()
        assert s.get_invoice('INV-2026-1001').balance == 500
        assert len(s.list_matches()) == 1


def test_reconciliation_input_refusals_are_actionable(plugin, hermes_fakes):
    module, ctx = plugin
    start(ctx, hermes_fakes)
    with module._store() as s:
        token = s.settlement_snapshot('INV-2026-1001')
    assert 'enter a verified external settlement in the invoice currency' in ctx.command(f'reconcile INV-2026-1001 nonsense {token}')
    assert 'verified external settlement must be between zero and the invoice amount' in ctx.command(f'reconcile INV-2026-1001 -1 {token}')
    with module._store() as s:
        assert s.get_invoice('INV-2026-1001').balance == 600
