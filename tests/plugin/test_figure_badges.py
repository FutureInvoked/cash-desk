"""The per-figure badge reflects today's ledger, not a hard-coded tick (judge 13)."""
from dataclasses import replace
from decimal import Decimal as D
from tests.plugin.support import load_demo


def test_badges_turn_false_when_the_ledger_changes_under_a_draft(plugin):
    mod, ctx = plugin; load_demo(ctx)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['id'].startswith('bluebell'))
    assert d['figures'] and all(f['ok'] is True for f in d['figures'])
    with mod._store() as store:
        inv = store.get_invoice(d['invoice_numbers'][0])
        store._conn.execute("UPDATE invoices SET due_date='2026-09-01' WHERE number=?", (inv.number,)); store._conn.commit()
    again = next(x for x in ctx.call('cashdesk_queue')['drafts'] if x['id'] == d['id'])
    assert any(f['ok'] is False for f in again['figures'])


def test_totals_and_thank_you_amounts_count_as_matching(plugin):
    mod, ctx = plugin; load_demo(ctx)
    ctx.call('cashdesk_match')
    drafts = ctx.call('cashdesk_draft')['drafts']
    multi = [d for d in drafts if len(d['invoice_numbers']) > 1] + [d for d in drafts if d['kind'] == 'thanks']
    assert multi
    for d in multi:
        assert all(f['ok'] is True for f in d['figures']), (d['id'], d['figures'])
