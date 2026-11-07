"""Replies that mean 'not this week' pause reminders even without a date (bookkeeper 12, judge 12)."""
from tests.plugin.support import load_demo


def stage(ctx, customer_prefix):
    return next(d for d in ctx.call('cashdesk_draft')['drafts'] if d['kind'].startswith('stage') and d['id'].startswith(customer_prefix))


def test_already_paid_reply_stops_an_approved_reminder(plugin):
    mod, ctx = plugin; load_demo(ctx)
    d = stage(ctx, 'bluebell')
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    out = ctx.call('cashdesk_reply', customer_id=d['customer_id'], text='We already paid this last Friday.', kind='paid')
    assert out['paused_until'] == '2026-11-13' and 'No reminder goes to them before 2026-11-16' in out['advice']
    sent = ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])
    assert sent['ok'] is False


def test_promise_in_words_without_a_date_pauses_for_a_week(plugin, monkeypatch):
    mod, ctx = plugin; load_demo(ctx)
    d = stage(ctx, 'northgate')
    out = ctx.call('cashdesk_reply', customer_id=d['customer_id'], text='We will pay on 20 November.', kind='promise')
    assert out['paused_until'] == '2026-11-15' and 'No reminder goes to them before 2026-11-18' in out['advice']
    monkeypatch.setenv('CASHDESK_TODAY', '2026-11-12')
    assert not [x for x in ctx.call('cashdesk_draft')['drafts'] if x['customer_id'] == d['customer_id'] and x['kind'].startswith('stage')]


def test_a_vague_later_reply_never_shortens_a_dated_promise(plugin, monkeypatch):
    mod, ctx = plugin; load_demo(ctx)
    d = stage(ctx, 'northgate')
    ctx.call('cashdesk_reply', customer_id=d['customer_id'], text='We will pay on 5 December.', kind='promise', promised_date='2026-12-05')
    later = ctx.call('cashdesk_reply', customer_id=d['customer_id'], text='As said, we will pay, sorry for the delay.', kind='promise')
    assert later['paused_until'] == '2026-12-05'
    paid = ctx.call('cashdesk_reply', customer_id=d['customer_id'], text='It went out today.', kind='paid')
    assert paid['paused_until'] == '2026-12-05'
    monkeypatch.setenv('CASHDESK_TODAY', '2026-11-24')
    assert not [x for x in ctx.call('cashdesk_draft')['drafts'] if x['customer_id'] == d['customer_id'] and x['kind'].startswith('stage')]
