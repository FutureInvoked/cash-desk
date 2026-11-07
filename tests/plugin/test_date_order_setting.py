"""A date_order setting cannot silently read a both-ways file into months of false lateness (bookkeeper 11 rank 1)."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\n"
US = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,10/02/2026,11/02/2026,100.00,EUR,100.00\n"
EU = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-1,C1,02/10/2026,02/11/2026,100.00,EUR,100.00\n"


def imp(ctx, home, text, **kw):
    folder = home / 'plugin-data' / 'cash-desk'; folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'inv.csv'; path.write_text(text)
    return ctx.call('cashdesk_import', kind='invoices', path=str(path), **kw)


def test_dmy_setting_on_a_us_file_is_refused_with_both_readings(plugin, hermes_fakes):
    mod, ctx = plugin; ctx.config['date_order'] = 'dmy'
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    out = imp(ctx, hermes_fakes, US)
    assert out['ok'] is False and 'every date in this file reads both ways, and your date_order setting' in out['error'] and 'date_order=mdy' in out['error']


def test_explicit_date_order_for_the_file_is_respected(plugin, hermes_fakes):
    mod, ctx = plugin; ctx.config['date_order'] = 'dmy'
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    assert imp(ctx, hermes_fakes, US, date_order='mdy')['ok']


def test_setting_that_gives_the_recent_reading_is_accepted(plugin, hermes_fakes):
    mod, ctx = plugin; ctx.config['date_order'] = 'dmy'
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    assert imp(ctx, hermes_fakes, EU)['ok']


def test_both_ways_file_without_a_setting_says_how_to_fix_it(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    out = imp(ctx, hermes_fakes, US)
    assert out['ok'] is False and 'date_order=dmy' in out['error'] and 'allow_errors' not in out['error']
