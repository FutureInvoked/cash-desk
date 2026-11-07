"""Real registered handlers and read-only Desktop surface over fake ledgers."""
from tests.plugin.test_api import client
from tests.test_customer_identity import stripe


def imp(ctx,home,text,name='invoices.csv'):
    folder=home/'plugin-data'/'cash-desk';folder.mkdir(parents=True,exist_ok=True)
    path=folder/name;path.write_text(text)
    return ctx.call('cashdesk_import',kind='invoices',source='stripe',path=str(path),open_list=False)


def test_registered_import_preserves_same_name_explicit_stripe_ids_and_two_recipients(plugin,hermes_fakes):
    module,ctx=plugin
    assert imp(ctx,hermes_fakes,stripe())['ok']
    assert imp(ctx,hermes_fakes,stripe('STR-9002','cus_branch_B','branch-b@amber.example','200'))['ok']
    result=ctx.draft_on('2026-11-06')
    assert result['ok'] and len(result['drafts'])==2
    assert {tuple(d['recipients']) for d in result['drafts']}=={('branch-a@amber.example',),('branch-b@amber.example',)}
    with module._store() as s:assert s.get_invoice('STR-9002').customer_id=='cus_branch_B'


def test_identity_question_is_visible_and_only_current_human_snapshot_can_verify(plugin,hermes_fakes,client):
    module,ctx=plugin;assert imp(ctx,hermes_fakes,stripe())['ok']
    draft=ctx.draft_on('2026-11-06')['drafts'][0]
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith('Approved')
    result=imp(ctx,hermes_fakes,stripe(customer='cus_branch_B',email='branch-b@amber.example'))
    assert not result['ok'] and result['quarantined'] and result['imported']['customer_identity_questions']==1
    assert 'Raw observations' in ctx.command('identities') and '/cashdesk identities' in ctx.command('review')
    assert 'Customer identity review' in ctx.call('cashdesk_brief')['brief']
    status=ctx.call('cashdesk_status');assert status['customer_identity_questions']==1 and status['balances_need_review']
    api=client[0];assert api.get('/review').json()['customer_identity_questions']
    assert api.get('/status').json()['customer_identity_questions']==1
    assert 'customer identity review' in ctx.command('identity-verify STR-9001 cus_branch_B stale Verified accounting records')
    with module._store() as s:q=s.customer_identity_reviews()[0]
    result=ctx.command(f"identity-verify STR-9001 cus_branch_B {q['snapshot']} Verified original accounting invoice customer ID and recipient")
    assert result.startswith('Customer identity verification recorded'),result
    with module._store() as s:
        assert s.get_invoice('STR-9001').customer_id=='cus_branch_B' and s.approval(draft['id']) is None
        assert not s.customer_identity_reviews()
    assert not list(hermes_fakes.rglob('*.eml'))
