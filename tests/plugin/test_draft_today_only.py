"""The model cannot choose the date a customer-facing draft is written for (P2ag)."""
import pytest
from tests.plugin.support import load_demo, AS_OF


@pytest.mark.parametrize('day', ['2026-12-01', '2026-11-11', '2026-11-09', '2026-10-01'])
def test_draft_for_another_day_is_refused_and_writes_nothing(plugin, day):
    mod, ctx = plugin; load_demo(ctx)
    before = ctx.call('cashdesk_queue')
    out = ctx.call('cashdesk_draft', as_of=day)
    assert out['ok'] is False
    assert 'drafts are written for today only' in out['error'] and AS_OF in out['error']
    assert ctx.call('cashdesk_queue') == before


def test_future_date_cannot_turn_a_seven_day_reminder_into_a_final_notice(plugin):
    mod, ctx = plugin; load_demo(ctx)
    assert ctx.call('cashdesk_draft', as_of='2026-12-01')['ok'] is False
    drafts = ctx.call('cashdesk_draft')['drafts']
    north = [d for d in drafts if 'northgate' in d['id'].lower() or d['customer'].startswith('Northgate')]
    assert north and all('28 days' not in d['subject'] and 'Urgent' not in d['subject'] for d in north)


@pytest.mark.parametrize('kw', [{}, {'as_of': AS_OF}, {'as_of': ' ' + AS_OF + ' '}])
def test_today_or_no_date_still_drafts(plugin, kw):
    mod, ctx = plugin; load_demo(ctx)
    out = ctx.call('cashdesk_draft', **kw)
    assert out['ok'] and out['drafts']


def test_status_and_brief_may_still_look_at_another_day(plugin):
    mod, ctx = plugin; load_demo(ctx)
    assert ctx.call('cashdesk_status', as_of='2026-12-01').get('ok', True) is not False
    assert ctx.call('cashdesk_brief', as_of='2026-12-01')['as_of'] == '2026-12-01'


def test_match_refuses_a_future_date_and_applies_nothing(plugin):
    mod, ctx = plugin; load_demo(ctx)
    before = ctx.call('cashdesk_status')
    out = ctx.call('cashdesk_match', as_of='2026-12-31')
    assert out['ok'] is False and 'matching cannot run for a future date' in out['error']
    assert ctx.call('cashdesk_status') == before


def test_match_for_today_or_no_date_still_runs(plugin):
    mod, ctx = plugin; load_demo(ctx)
    assert ctx.call('cashdesk_match', as_of=AS_OF)['ok']
    assert ctx.call('cashdesk_match')['ok']
