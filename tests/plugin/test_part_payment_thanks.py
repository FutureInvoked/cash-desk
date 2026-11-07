"""A thank-you for a part payment says what is still open (judge 14), and still passes Figure Lock."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-0201,C1,2026-09-01,2026-10-27,1000.00,EUR,1000.00\n"
BANK = "id,date,amount,currency,counterparty,reference\nP1,2026-11-03,400.00,EUR,Example Client Ltd,INV-0201\n"


def test_part_payment_thank_you_names_the_open_balance_and_can_be_sent(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'b.csv', BANK, kind='bank')
    assert ctx.command('apply P1 INV-0201').startswith('Applied')
    thanks = next(d for d in ctx.call('cashdesk_draft')['drafts'] if d['kind'] == 'thanks')
    assert 'EUR 400.00' in thanks['body'] and 'INV-0201: EUR 600.00 is still open.' in thanks['body']
    warmer = thanks['body'].replace('We appreciate it.', 'We really appreciate it, thank you.')
    revised = ctx.call('cashdesk_revise', draft_id=thanks['id'], new_text=warmer)
    assert revised['ok'], revised
    d = next(x for x in ctx.call('cashdesk_queue', status='pending')['drafts'] if x['kind'] == 'thanks')
    assert ctx.command(f"approve {d['id']} {d['hash'][:12]}").startswith('Approved')
    assert ctx.call('cashdesk_send', draft_id=d['id'], draft_hash=d['hash'])['ok']
