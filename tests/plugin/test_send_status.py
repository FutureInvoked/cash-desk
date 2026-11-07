"""A send interrupted between reservation and confirmation can be resolved by a person (judge 13)."""
from tests.plugin.support import load_demo


def test_send_left_in_progress_is_resolved_with_send_status(plugin):
    mod, ctx = plugin; load_demo(ctx)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['id'].startswith('bluebell'))
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    with mod._store() as store:
        store.reserve_send(d['id'], d['hash'], 'eml')  # the gateway stopped here
    stuck = ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])
    assert stuck['ok'] is False and 'in progress' in stuck['error']
    assert ctx.command(f"send-status {d['id']}").startswith('Usage')
    assert ctx.command(f"send-status {d['id']} failed checked Sent folder, not there").startswith('Recorded')
    assert ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])['ok']
    assert 'has no send in progress' in ctx.command(f"send-status {d['id']} sent x")
