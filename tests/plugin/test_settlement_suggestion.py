"""/cashdesk settlements proposes the usual reconcile figure (bookkeeper 11, judge 10)."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,2026-09-01,2026-10-27,1000.00,EUR,{bal}\n"


def test_settlements_lists_the_reconcile_command_for_an_export_that_caught_up(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'w1.csv', INV.format(bal='600.00'))
    import_file(ctx, hermes_fakes, 'bank.csv', "id,date,amount,currency,counterparty,reference\nP1,2026-11-03,100.00,EUR,Example Client Ltd,INV-1\n", kind='bank')
    assert ctx.command('apply P1 INV-1').startswith('Applied')
    import_file(ctx, hermes_fakes, 'w2.csv', INV.format(bal='500.00'))
    out = ctx.command('settlements')
    assert 'If that already includes the EUR 100.00 applied here' in out
    line = next(l for l in out.splitlines() if l.strip().startswith('/cashdesk reconcile INV-1 400'))
    _, _, number, figure, token = line.split()
    assert ctx.command(f'reconcile {number} {figure} {token}').lower().startswith(('reconciled', 'recorded'))
    with mod._store() as store:
        assert 'INV-1' not in store.settlement_reviews()
        assert str(store.get_invoice('INV-1').balance) == '500.00'
