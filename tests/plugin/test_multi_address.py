"""A contact cell with two addresses sends to both (bookkeeper: 'a; b' became one invalid recipient)."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example; finance@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00\n"


def test_two_addresses_in_one_cell_are_two_recipients_and_the_send_goes(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert d['recipients'] == ['ap@client.example', 'finance@client.example']
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    out = ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])
    assert out.get('ok'), out
