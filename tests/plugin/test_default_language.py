"""Exports without a language column use the default_language setting (judge 14)."""
from tests.plugin.test_eligibility_lifecycle import import_file

XERO = ("ContactName,EmailAddress,InvoiceNumber,InvoiceDate,DueDate,Total,InvoiceAmountPaid,InvoiceAmountDue,Currency,Type,Status\n"
        "Žluťoučký kůň s.r.o.,ucetni@kun.example,FA-1,01/09/2026,27/10/2026,1000.00,0.00,1000.00,CZK,Sales invoice,AUTHORISED\n")


def test_czech_default_language_reaches_reminders_from_a_xero_export(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['default_language'] = 'cs'
    import_file(ctx, hermes_fakes, 'x.csv', XERO, source='xero', date_order='dmy')
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert d['lang'] == 'cs'


def test_a_language_column_still_wins(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['default_language'] = 'cs'
    import_file(ctx, hermes_fakes, 'c.csv', "customer_id,name,email,contact_name,language\nC1,Example Client Ltd,ap@client.example,Ada,en\n", kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00\n")
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    assert d['lang'] == 'en'
