"""Drafting reminders on stale or missing bank data says so (judge 11 rank 5)."""
from tests.plugin.support import load_demo
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00\n"


def test_no_bank_statement_warns(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    out = ctx.call('cashdesk_draft')
    assert out['drafts'] and 'No bank statement has been imported' in out['bank_warning']


def test_old_bank_lines_warn_even_when_imported_today(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'old.csv', "id,date,amount,currency,counterparty,reference\nX1,2026-08-03,5.00,EUR,Someone,misc\n", kind='bank')
    out = ctx.call('cashdesk_draft')
    assert 'The newest bank line is 99 days old' in out['bank_warning']


def test_recent_bank_lines_do_not_warn(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'new.csv', "id,date,amount,currency,counterparty,reference\nX1,2026-11-09,5.00,EUR,Someone,misc\n", kind='bank')
    assert ctx.call('cashdesk_draft')['bank_warning'] == ''
