"""The same line in two overlapping weekly downloads without IDs is one receipt, no question (judge 11 rank 4)."""
import datetime as dt
from decimal import Decimal as D
from cashdesk import Customer, Invoice
from cashdesk.importers import read_bank_csv
from cashdesk.store import Store

DAY = dt.date(2026, 11, 12)
HEAD = "Date,Counter Party,Reference,Type,Amount (EUR),Balance (EUR)\n"
A = "03/11/2026,Bluebell Design Studio,INV-100,FASTER PAYMENT,1250.00,5000.00\n"
B = "06/11/2026,Oakridge Partners,INV-200,FASTER PAYMENT,300.00,5300.00\n"


def desk():
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('C1', 'Bluebell Design Studio'), Customer('C2', 'Oakridge Partners')])
    s.upsert_invoices([Invoice('INV-100', 'C1', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('1250'), 'EUR', D('1250')),
                       Invoice('INV-200', 'C2', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('300'), 'EUR', D('300'))])
    return s


def load(s, name, *rows):
    r = read_bank_csv(HEAD + ''.join(rows), date_order='dmy'); assert r.ok, r.errors
    s.ingest(r, file_name=name)
    for m in s.match_result(DAY).applied:
        s.apply_match(m)


def test_repeated_line_in_next_download_is_the_same_receipt():
    s = desk()
    load(s, 'week1.csv', D0, A)
    load(s, 'week2.csv', D0, A, B)
    res = s.match_result(DAY)
    assert not [i for i in res.review if i.kind == 'POSSIBLE_DUPLICATE']
    assert not s.payment_overlaps()
    assert s.get_invoice('INV-100').status == 'paid' and s.get_invoice('INV-200').status == 'paid'
    assert sum(m.amount for m in s.list_matches() if m.invoice_number == 'INV-100') == D('1250')


def test_two_identical_lines_in_one_file_still_ask():
    s = desk()
    load(s, 'week1.csv', A, A)
    assert s.payment_overlaps()


C = "09/11/2026,Oakridge Partners,INV-200,FASTER PAYMENT,10.00,5310.00\n"
D0 = "01/11/2026,Someone,misc,FASTER PAYMENT,1.00,3750.00\n"


def test_third_rolling_download_still_imports_and_keeps_one_receipt():
    s = desk()
    load(s, 'w1.csv', D0, A)
    load(s, 'w2.csv', D0, A, B)
    load(s, 'w3.csv', D0, A, B, C)
    assert not s.payment_overlaps()
    assert sum(m.amount for m in s.list_matches() if m.invoice_number == 'INV-100') == D('1250')


def test_equal_payments_with_different_running_balances_still_ask():
    s = desk()
    load(s, 'w1.csv', D0, A)
    load(s, 'w2.csv', D0, A.replace('5000.00', '5500.00'))
    assert s.payment_overlaps()


def test_back_to_back_downloads_sharing_only_the_boundary_day_still_ask():
    s = desk()
    load(s, 'w1.csv', D0, A)
    load(s, 'w2.csv', A, B)
    assert s.payment_overlaps()


def test_three_customers_paying_the_same_amount_on_one_day_are_not_duplicates():
    s = desk()
    load(s, 'w1.csv', "03/11/2026,Alpha Ltd,SUB-1,FASTER PAYMENT,99.00,1000.00\n",
                      "03/11/2026,Beta Ltd,SUB-2,FASTER PAYMENT,99.00,1099.00\n",
                      "03/11/2026,Gamma Ltd,SUB-3,FASTER PAYMENT,99.00,1198.00\n")
    assert not s.payment_overlaps()
