"""A refused bank statement stops reminder drafting until it is fixed (judge 14)."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00\n"
BOTH_WAYS = "id,date,amount,currency,counterparty,reference\nP1,03/11/2026,100.00,EUR,Example Client Ltd,INV-1\n"


def test_refused_statement_blocks_drafting_until_a_clean_import(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    folder = hermes_fakes / 'plugin-data' / 'cash-desk'
    (folder / 'bank.csv').write_text(BOTH_WAYS)
    assert ctx.call('cashdesk_import', kind='bank', path=str(folder / 'bank.csv'))['ok'] is False
    blocked = ctx.call('cashdesk_draft')
    assert blocked['ok'] and not [d for d in blocked['drafts'] if d['kind'].startswith('stage')]
    assert any('bank statement bank.csv was refused, so payments in it are unknown' in h['detail'] for h in blocked['held'])
    assert ctx.call('cashdesk_import', kind='bank', path=str(folder / 'bank.csv'), date_order='dmy')['ok']
    assert ctx.call('cashdesk_draft')['ok']


def test_refusal_also_stops_an_approved_reminder_and_clears_only_for_that_file(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    folder = hermes_fakes / 'plugin-data' / 'cash-desk'
    (folder / 'bank.csv').write_text(BOTH_WAYS)
    assert ctx.call('cashdesk_import', kind='bank', path=str(folder / 'bank.csv'))['ok'] is False
    assert ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])['ok'] is False
    (folder / 'other.csv').write_text("id,date,amount,currency,counterparty,reference\nX9,2026-11-04,5.00,EUR,Someone,misc\n")
    assert ctx.call('cashdesk_import', kind='bank', path=str(folder / 'other.csv'))['ok']
    with mod._store() as store:
        assert store.bank_refusals()


def test_path_typo_records_nothing(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    assert ctx.call('cashdesk_import', kind='bank', path=str(hermes_fakes / 'plugin-data' / 'cash-desk' / 'nope.csv'))['ok'] is False
    with mod._store() as store:
        assert not store.bank_refusals()
