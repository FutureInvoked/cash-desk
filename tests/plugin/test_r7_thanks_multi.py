"""r7: two part payments of one invoice in one thank-you; a pending credit note."""
from tests.plugin.test_eligibility_lifecycle import import_file
CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-0201,C1,2026-09-01,2026-10-27,1000.00,EUR,1000.00\n"

def test_two_part_payments(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers'); import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'b.csv', "id,date,amount,currency,counterparty,reference\n"
                "P1,2026-11-03,300.00,EUR,Example Client Ltd,INV-0201\nP2,2026-11-04,300.00,EUR,Example Client Ltd,INV-0201\n", kind='bank')
    print(ctx.command('apply P1 INV-0201')); print(ctx.command('apply P2 INV-0201'))
    out = ctx.call('cashdesk_draft'); print(out.get('error'))
    for d in out.get('drafts', []):
        if d['kind'] == 'thanks': print(d['body'])

def test_pending_credit_note(plugin, hermes_fakes):
    mod, ctx = plugin
    H = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance,type\n"
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers'); import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'b.csv', "id,date,amount,currency,counterparty,reference\nP1,2026-11-03,400.00,EUR,Example Client Ltd,INV-0201\n", kind='bank')
    ctx.command('apply P1 INV-0201')
    import_file(ctx, hermes_fakes, 'cn.csv', H + "CN-1,C1,2026-11-01,2026-11-01,-600,EUR,-600,credit note\n", open_list=False)
    out = ctx.call('cashdesk_draft')
    print('held:', [(h['reason'], h['invoice_numbers']) for h in out['held']])
    for d in out['drafts']:
        if d['kind'] == 'thanks': print(d['body'])
