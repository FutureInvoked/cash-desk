"""One transfer for several invoices of one customer, exactly, applies each (judge 12)."""
import datetime as dt
from decimal import Decimal as D
from cashdesk import Customer, Invoice
from cashdesk.importers import read_bank_csv
from cashdesk.store import Store

DAY = dt.date(2026, 11, 10)


def desk():
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('C1', 'Bluebell Design Studio Ltd'), Customer('C2', 'Oakridge Partners LLP')])
    s.upsert_invoices([Invoice('INV-1', 'C1', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('1000'), 'EUR', D('1000')),
                       Invoice('INV-2', 'C1', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('250'), 'EUR', D('250')),
                       Invoice('INV-3', 'C2', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('250'), 'EUR', D('250'))])
    return s


def bank(s, amount, ref, payer='Bluebell Design Studio Ltd'):
    r = read_bank_csv(f"id,date,amount,currency,counterparty,reference\nP1,2026-11-03,{amount},EUR,{payer},{ref}\n"); assert r.ok
    s.ingest(r, file_name='b.csv')
    res = s.match_result(DAY)
    for m in res.applied:
        s.apply_match(m)
    return res


def test_exact_batch_applies_each_invoice():
    s = desk(); res = bank(s, '1250.00', 'INV-1 INV-2')
    assert sorted(m.invoice_number for m in res.applied) == ['INV-1', 'INV-2']
    assert s.get_invoice('INV-1').status == 'paid' and s.get_invoice('INV-2').status == 'paid'


def test_batch_that_does_not_add_up_or_spans_customers_is_reviewed():
    s = desk(); assert not bank(s, '1200.00', 'INV-1 INV-2').applied
    s = desk(); assert not bank(s, '1250.00', 'INV-1 INV-3').applied
    s = desk(); assert not bank(s, '1250.00', 'INV-1 INV-2', payer='Someone Else').applied
