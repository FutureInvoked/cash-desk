"""r7 review repros: the still_open line in thank-you notes, and default_language."""
from tests.plugin.test_eligibility_lifecycle import import_file

CUST = "customer_id,name,email,contact_name,language\nC1,Example Client Ltd,ap@client.example,Ada,{lang}\n"
INV = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\nINV-0201,C1,2026-09-01,2026-10-27,1000.00,EUR,1000.00\n"
BANK = "id,date,amount,currency,counterparty,reference\nP1,2026-11-03,400.00,EUR,Example Client Ltd,INV-0201\n"


def thanks_for(ctx):
    out = ctx.call('cashdesk_draft')
    assert out['ok'], out
    return out, [d for d in out['drafts'] if d['kind'] == 'thanks']


def test_rest_paid_in_review_still_listed_as_open(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST.format(lang='en'), kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'b.csv', BANK + "P2,2026-11-05,600.00,EUR,Example Client Ltd,balance thanks\n", kind='bank')
    print('apply:', ctx.command('apply P1 INV-0201'))
    out, th = thanks_for(ctx)
    print('held:', [h['reason'] for h in out['held']])
    for d in th: print(d['body'])
    assert not any('600.00 is still open' in d['body'] for d in th), "customer whose remaining 600 sits in review is told 600 is still open"


def test_still_open_figure_is_locked_and_czech_text(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST.format(lang='cs'), kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    import_file(ctx, hermes_fakes, 'b.csv', BANK, kind='bank')
    ctx.command('apply P1 INV-0201')
    out, th = thanks_for(ctx)
    t = th[0]; print(repr(t['body']))
    for bad in (t['body'].replace('600,00', '60,00'),
                '\n'.join(l for l in t['body'].splitlines() if 'zbývá' not in l)):
        r = ctx.call('cashdesk_revise', draft_id=t['id'], new_text=bad)
        print('revise ok:', r['ok'], r.get('error', '')[:120])


def test_customers_file_without_language_overwrites_explicit_language(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['default_language'] = 'cs'
    import_file(ctx, hermes_fakes, 'c.csv', CUST.format(lang='en'), kind='customers')
    import_file(ctx, hermes_fakes, 'c2.csv', "customer_id,name,email\nC1,Example Client Ltd,new@client.example\n", kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    print('lang after email-only customers file:', d['lang'])
    assert d['lang'] == 'en'


def test_blank_language_cell_ignores_default(plugin, hermes_fakes):
    mod, ctx = plugin
    ctx.config['default_language'] = 'cs'
    import_file(ctx, hermes_fakes, 'c.csv', CUST.format(lang=''), kind='customers')
    import_file(ctx, hermes_fakes, 'i.csv', INV)
    d = next(x for x in ctx.call('cashdesk_draft')['drafts'] if x['kind'].startswith('stage'))
    print('lang with blank cell:', d['lang'])
