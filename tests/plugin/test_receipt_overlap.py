"""P2b through registered fake handlers and the actual Desktop backend."""
import shlex


from tests.plugin.test_api import client
from tests.plugin.test_eligibility_lifecycle import import_file

INVOICES = ('invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference\n'
            'INV-A,C1,2026-10-01,2026-10-22,1200,EUR,1200,open,INV-A\n'
            'INV-B,C2,2026-10-01,2026-10-22,600,EUR,600,open,INV-B\n')
BANK = 'date,amount,currency,reference,counterparty,memo\n2026-11-01,600,EUR,{reference},{payer},{memo}\n'


def setup(ctx, root):
    import_file(ctx, root, 'customers.csv', 'customer_id,name,email\nC1,Alpha Ltd,ap@alpha.example\nC2,Beta Ltd,ap@beta.example\n', kind='customers')
    import_file(ctx, root, 'invoices.csv', INVOICES)


def bank(ctx, root, name, reference='INV-A', payer='Alpha Ltd', memo='first'):
    return import_file(ctx, root, name, BANK.format(reference=reference, payer=payer, memo=memo), kind='bank')


def command(ctx, question, decision):
    return ctx.command(shlex.join(['receipt', question['id1'], question['id2'], decision, question['snapshot']]))


def test_changed_receipt_cannot_pay_twice_or_reuse_an_approval(plugin, hermes_fakes):
    module, ctx = plugin
    setup(ctx, hermes_fakes)
    bank(ctx, hermes_fakes, 'first.csv')
    with module._store() as store:
        first = store.list_payments()[0].id
    assert ctx.command(f'apply {first} INV-A 600').startswith('Applied')
    assert ctx.call('cashdesk_draft', as_of='2026-11-10')['ok']
    draft = next(d for d in ctx.call('cashdesk_queue')['drafts'] if d['kind'].startswith('stage') and d['customer_id'] == 'C1')
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith('Approved')
    bank(ctx, hermes_fakes, 'overlap.csv', reference='INV-B', payer='Beta Ltd', memo='corrected reference')
    matched = ctx.call('cashdesk_match')
    assert matched['ok'] and not matched['applied']
    assert matched['totals']['review_in'].get('EUR', '0') == '0'
    assert len(matched['receipt_overlaps']) == 1
    with module._store() as store:
        assert store.approval(draft['id']) is None
        assert str(store.get_invoice('INV-A').balance) == '600.00'
        assert str(store.get_invoice('INV-B').balance) == '600.00'
        assert len(store.list_matches()) == 1
    sent = ctx.call('cashdesk_send', draft_id=draft['id'], draft_hash=draft['hash'])
    assert not sent['ok'] and 'possible receipt overlap' in sent['error']
    assert not list(hermes_fakes.rglob('*.eml'))
    question = matched['receipt_overlaps'][0]
    shown = ctx.command('overlaps')
    assert 'observed rows, not confirmed extra cash' in shown
    assert first in shown and 'INV-A 600.00' in shown
    stale = dict(question, snapshot='stale')
    assert 'receipt snapshot changed' in command(ctx, stale, 'same')
    assert 'choose same or distinct' in command(ctx, question, 'guess')
    assert 'Same receipt' in command(ctx, question, 'same')
    with module._store() as store:
        assert not store.payment_overlaps()
        assert len(store.list_payments()) == 2
        alias = next(p.id for p in store.list_payments() if p.id != first)
    assert 'receipt is a confirmed duplicate' in ctx.command(f'apply {alias} INV-B 600')
    with module._store() as store:
        resolved = dict(question, snapshot=store.overlap_snapshot(question['id1'], question['id2']))
    assert 'decision is already recorded' in command(ctx, resolved, 'distinct')
    assert not ctx.call('cashdesk_send', draft_id=draft['id'], draft_hash=draft['hash'])['ok']
    with module._store() as store:
        placement = store.receipt_placement_reviews()[0]
    assert 'receipt allocation review required' in ctx.command('overlaps')
    assert 'allocation/disposition confirmed' in ctx.command(shlex.join(['receipt-keep', placement['canonical'], placement['snapshot']]))
    assert ctx.call('cashdesk_draft', as_of='2026-11-10')['ok']
    fresh = next(d for d in ctx.call('cashdesk_queue')['drafts'] if d['kind'].startswith('stage') and d['customer_id'] == 'C1')
    assert ctx.command(f"approve {fresh['id']} {fresh['hash'][:12]}").startswith('Approved')
    assert ctx.call('cashdesk_send', draft_id=fresh['id'], draft_hash=fresh['hash'])['ok']
    assert len(list(hermes_fakes.rglob('*.eml'))) == 1


def test_genuine_equal_receipts_are_retained_until_verified_distinct(plugin, hermes_fakes):
    module, ctx = plugin
    setup(ctx, hermes_fakes)
    bank(ctx, hermes_fakes, 'first.csv')
    # Two equal receipts in the later export: one may be the earlier line, one a second payment. A person decides.
    row = BANK.format(reference='INV-A', payer='Alpha Ltd', memo='first')
    text = row.replace('memo\n', 'memo,extra\n').replace('first\n', 'first,month-export\n')
    text = text + text.splitlines()[-1] + '\n'
    import_file(ctx, hermes_fakes, 'month.csv', text, kind='bank')
    matched = ctx.call('cashdesk_match')
    assert not matched['applied'] and matched['receipt_overlaps']
    with module._store() as store:
        assert len(store.list_payments()) == 3  # every raw observation is retained
        assert not store.list_matches()

def test_desktop_shows_identity_questions_and_disables_apply_and_dismiss(client, hermes_fakes):
    api, ctx = client
    setup(ctx, hermes_fakes)
    bank(ctx, hermes_fakes, 'first.csv')
    bank(ctx, hermes_fakes, 'other.csv', memo='changed')
    review = api.get('/review').json()
    assert len(review['receipt_overlaps']) == 1
    assert len(review['payments']) == 2
    assert all(p['kind'] == 'POSSIBLE_DUPLICATE' and not p['incoming'] for p in review['payments'])
    assert all('/cashdesk overlaps' in p['why'] for p in review['payments'])
    assert review['cash_totals']['review_in'].get('EUR', '0') == '0'
    assert 'possible receipt overlap' in ctx.command('review')
    status = api.get('/status').json()
    assert status['receipt_overlap_questions'] == 1 and status['balances_need_review']
    assert status['chip'].startswith('Review 1 receipt identity')
    assert ctx.call('cashdesk_status')['receipt_overlap_questions'] == 1


def test_receipt_commands_handle_spaces_and_reject_missing_records(plugin, hermes_fakes):
    module, ctx = plugin
    setup(ctx, hermes_fakes)
    with module._store() as store:
        e = module._engine()
        import datetime
        from decimal import Decimal
        store.add_payments([e.Payment('bank original', datetime.date(2026,11,1), Decimal('600'), 'EUR')])
    bank(ctx, hermes_fakes, 'derived.csv')
    question = ctx.call('cashdesk_match')['receipt_overlaps'][0]
    assert 'Same receipt' in command(ctx, question, 'same')
    assert 'needs two stored receipts' in ctx.command('receipt missing other same token')
    assert 'Usage:' in ctx.command('receipt')
