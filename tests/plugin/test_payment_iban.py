"""Reminders from exports without an IBAN carry the business's own account (judge 13)."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00\n"
OWN = "GB82 WEST 1234 5698 7654 32"


def test_payment_iban_appears_in_reminders_and_is_locked(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['payment_iban'] = OWN
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert 'GB82' in d['body'].replace(' ', '') or 'GB82WEST12345698765432' in d['body'].replace(' ', '')
    changed = d['body'].replace('GB82 WEST 1234 5698 7654 32', 'GB33 BUKB 2020 1555 5555 55').replace('GB82WEST12345698765432', 'GB33BUKB20201555555555')
    assert ctx.call('cashdesk_revise', draft_id=d['id'], new_text=changed)['ok'] is False
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    assert ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])['ok']


def test_invalid_payment_iban_refuses_drafting(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['payment_iban'] = 'GB00 1234'
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    out = ctx.call('cashdesk_draft')
    assert out['ok'] is False and 'payment_iban setting is not a valid IBAN' in out['error']


def test_setting_set_after_import_reaches_reminders(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    ctx.config['payment_iban'] = OWN
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert 'GB82WEST12345698765432' in d['body'].replace(' ', '')


def test_changed_setting_withdraws_the_old_account_and_redrafts(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['payment_iban'] = OWN
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    ctx.config['payment_iban'] = 'GB33 BUKB 2020 1555 5555 55'
    assert ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])['ok'] is False
    fresh = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert 'GB33BUKB20201555555555' in fresh['body'].replace(' ', '')


def test_export_iban_is_never_replaced_by_the_setting(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['payment_iban'] = OWN
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', "invoice_no,customer_id,issue_date,due_date,amount,currency,balance,iban\nINV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00,GB33BUKB20201555555555\n")
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert 'GB33BUKB20201555555555' in d['body'].replace(' ', '') and 'GB82WEST' not in d['body'].replace(' ', '')
