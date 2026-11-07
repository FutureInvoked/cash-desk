"""A reminder approved but sent days later must not state an untrue age (P1n)."""
from tests.plugin.support import load_demo, AS_OF


def first_reminder(ctx):
    return next(d for d in ctx.call('cashdesk_draft')['drafts'] if d['kind'].startswith('stage'))


def test_reminder_sent_more_than_six_days_after_writing_is_refused_and_redrafted(plugin, monkeypatch):
    mod, ctx = plugin; load_demo(ctx)
    d = first_reminder(ctx)
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    monkeypatch.setenv('CASHDESK_TODAY', '2026-11-17')
    out = ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])
    assert out['ok'] is False and 'days-overdue figures are no longer true' in (out.get('error') or '')
    fresh = [x for x in ctx.call('cashdesk_draft')['drafts'] if x['customer_id'] == d['customer_id'] and x['kind'].startswith('stage')]
    assert fresh and fresh[0]['hash'] != d['hash']


def test_reminder_approved_and_sent_later_in_the_week_still_goes(plugin, monkeypatch):
    mod, ctx = plugin; load_demo(ctx)
    d = first_reminder(ctx)
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    monkeypatch.setenv('CASHDESK_TODAY', '2026-11-16')
    out = ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])
    assert out.get('ok'), out


def test_thank_you_is_not_age_bound(plugin, monkeypatch):
    mod, ctx = plugin; load_demo(ctx)
    ctx.call('cashdesk_match')
    thanks = [d for d in ctx.call('cashdesk_draft')['drafts'] if d['kind'] == 'thanks']
    if not thanks:
        return
    d = thanks[0]
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    monkeypatch.setenv('CASHDESK_TODAY', '2026-11-30')
    out = ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])
    assert 'days-overdue' not in (out.get('error') or '')
