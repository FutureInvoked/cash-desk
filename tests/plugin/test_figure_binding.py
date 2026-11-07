"""Exercise cross-invoice permutations through the registered revision tool."""
import pytest
from tests.plugin.support import AS_OF, load_demo


@pytest.mark.parametrize('kind', ['money', 'date', 'invoice_no', 'reference'])
def test_registered_revision_rejects_cross_invoice_permutations(plugin, kind):
    module, ctx = plugin
    load_demo(ctx)
    assert ctx.command("dismiss B1-02 verified noninvoice receipt for mutation control").startswith("Dismissed")
    assert ctx.call('cashdesk_draft', as_of=AS_OF)['ok']
    draft = next(d for d in ctx.call('cashdesk_queue')['drafts'] if len(d['invoice_numbers']) == 2)
    figures = module._engine().extract_figures(draft['body'], draft['lang']).of(kind)
    assert len(figures) >= 2
    a, b = figures[:2]
    body = draft['body'].replace(a.raw, '__SWAP__').replace(b.raw, a.raw).replace('__SWAP__', b.raw)
    assert body != draft['body']
    result = ctx.call('cashdesk_revise', draft_id=draft['id'], new_text=body)
    assert result['ok'] is False and result['rejected']
    assert 'financial lines changed' in ' '.join(result['diff'])
    after = next(d for d in ctx.call('cashdesk_queue')['drafts'] if d['id'] == draft['id'])
    assert (after['body'], after['hash']) == (draft['body'], draft['hash'])
    assert ctx.call('cashdesk_revise', draft_id=draft['id'], new_text='Thank you!\n' + draft['body'])['ok']
