"""The send gate names why an approval was withdrawn; an escalation note is never approvable (judge 12)."""
from tests.plugin.support import load_demo


def test_gate_names_the_hold_that_withdrew_an_approval(plugin):
    mod, ctx = plugin; load_demo(ctx)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['id'].startswith('bluebell'))
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    ctx.command(f"hold {d['customer_id']} asked us to wait for their audit")
    verdict = ctx.gate('cashdesk_send', {'draft_id': d['id'], 'draft_hash': d['hash']})
    assert verdict['action'] == 'block' and 'its approval was withdrawn' in verdict['message'] and 'hold' in verdict['message']


def test_escalation_note_cannot_be_approved():
    import datetime as dt
    from cashdesk.model import Draft
    from cashdesk.store import Store, ConflictError
    from cashdesk.approval import draft_hash
    import pytest
    s = Store(':memory:', today=lambda: dt.date(2026, 11, 10))
    d = Draft('esc-1', 'escalation', 'C1', ('INV-1',), 'en', 'Escalation', 'Internal note', (), dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc))
    from dataclasses import replace
    d = replace(d, hash=draft_hash(d.body, d.recipients, d.subject))
    s.create_draft(d)
    with pytest.raises(ConflictError, match='internal escalation note'):
        s.approve_draft(d.id, d.hash)
