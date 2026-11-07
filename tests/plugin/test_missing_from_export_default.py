"""An invoice the next export leaves out is held by default, not chased (bookkeeper rank 1)."""
from tests.plugin.test_eligibility_lifecycle import import_file

HEAD = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance\n"
CUST = "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada\nC2,Other Client Ltd,ap@other.example,Alex\n"
A = "INV-1,C1,2026-09-01,2026-10-27,100.00,EUR,100.00\n"
B = "INV-2,C2,2026-09-01,2026-10-27,200.00,EUR,200.00\n"


def test_invoice_left_out_of_next_export_is_held_by_default(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'week1.csv', HEAD + A + B)
    r = import_file(ctx, hermes_fakes, 'week2.csv', HEAD + B)
    assert r['imported']['not_in_export'] == 1
    assert any('not in the latest export' in w['message'] for w in r['warnings'])
    ctx.call('cashdesk_draft')
    customers = {d['customer_id'] for d in ctx.call('cashdesk_queue', status='pending')['drafts']}
    assert customers == {'C2'}


def test_partial_file_with_open_list_false_holds_nothing(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'week1.csv', HEAD + A, open_list=False)
    r = import_file(ctx, hermes_fakes, 'week2.csv', HEAD + B, open_list=False)
    assert not r['imported'].get('not_in_export')
    ctx.call('cashdesk_draft')
    assert {d['customer_id'] for d in ctx.call('cashdesk_queue', status='pending')['drafts']} == {'C1', 'C2'}


def test_still_open_releases_the_hold_with_a_reason(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'week1.csv', HEAD + A + B)
    import_file(ctx, hermes_fakes, 'week2.csv', HEAD + B)
    assert ctx.command('still-open INV-1').startswith('Usage')
    out = ctx.command('still-open INV-1 checked Xero, it is still unpaid')
    assert out.startswith('Recorded') and 'INV-1' in out
    ctx.call('cashdesk_draft')
    assert {d['customer_id'] for d in ctx.call('cashdesk_queue', status='pending')['drafts']} == {'C1', 'C2'}


def test_still_open_refuses_an_invoice_that_is_not_held(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'week1.csv', HEAD + A + B)
    out = ctx.command('still-open INV-1 sure')
    assert 'is not held as missing from an export' in out


def test_byte_identical_reimport_releases_invoices_it_lists(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'week1.csv', HEAD + A + B)
    import_file(ctx, hermes_fakes, 'week2.csv', HEAD + B)          # filtered by mistake: INV-1 held
    folder = hermes_fakes / 'plugin-data' / 'cash-desk'
    (folder / 'week3.csv').write_text(HEAD + A + B)                 # byte-identical to week 1
    out = ctx.call('cashdesk_import', kind='invoices', path=str(folder / 'week3.csv'))
    assert out['ok'] is False and out.get('duplicate') and out.get('released') == ['INV-1']
    ctx.call('cashdesk_draft')
    assert {d['customer_id'] for d in ctx.call('cashdesk_queue', status='pending')['drafts']} == {'C1', 'C2'}


MORE = "".join(f"INV-{n},C1,2026-09-01,2026-10-27,10.00,EUR,10.00\n" for n in range(3, 8))


def test_export_leaving_out_most_open_invoices_asks_which_kind_of_file_it_is(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'week1.csv', HEAD + A + B + MORE)
    folder = hermes_fakes / 'plugin-data' / 'cash-desk'
    (folder / 'new_only.csv').write_text(HEAD + "INV-9,C2,2026-11-01,2026-12-01,50.00,EUR,50.00\n")
    out = ctx.call('cashdesk_import', kind='invoices', path=str(folder / 'new_only.csv'))
    assert out['ok'] is False and 'leaves out 7 of the 7 open invoices' in out['error'] and 'If it lists every open invoice' in out['error']
    assert ctx.call('cashdesk_import', kind='invoices', path=str(folder / 'new_only.csv'), open_list=False)['ok']


def test_guard_does_not_recount_invoices_already_held_as_missing(plugin, hermes_fakes):
    mod, ctx = plugin
    import_file(ctx, hermes_fakes, 'c.csv', CUST, kind='customers')
    import_file(ctx, hermes_fakes, 'w1.csv', HEAD + A + B + MORE)
    import_file(ctx, hermes_fakes, 'w2.csv', HEAD + B, open_list=True)            # confirmed: complete list
    r = import_file(ctx, hermes_fakes, 'w3.csv', HEAD + B + "INV-9,C2,2026-11-01,2026-12-01,50.00,EUR,50.00\n")
    assert r['ok']
