"""QuickBooks Online 'Open Invoices' grouped by customer imports (judge 11 rank 3)."""
from decimal import Decimal as D
from cashdesk.importers import read_invoices

QBO = '''"Riverside Studio LLC"
"Open Invoices"
"As of November 10, 2026"

"","Date","Transaction Type","Num","Term","Due Date","Past Due","Amount","Open Balance"
"Acme Corp","","","","","","","",""
"","10/01/2026","Invoice","1001","Net 30","10/31/2026","10","$1,200.00","$1,200.00"
"","10/15/2026","Invoice","1004","Net 15","10/30/2026","11","$450.00","$200.00"
"Total for Acme Corp","","","","","","","$1,650.00","$1,400.00"
"Zoë's Café","","","","","","","",""
"","09/20/2026","Invoice","998","Net 30","10/20/2026","21","$800.00","$800.00"
"","10/02/2026","Credit Memo","CM-12","","10/02/2026","","-$100.00","-$100.00"
"Total for Zoë's Café","","","","","","","$700.00","$700.00"
"TOTAL","","","","","","","$2,350.00","$2,100.00"

"Monday, Nov 10, 2026 09:12 AM GMT-05:00"
'''


def test_grouped_open_invoices_report_reads_each_customer_heading():
    r = read_invoices('quickbooks', QBO, default_currency='USD')
    assert r.ok, r.errors
    got = {i.number: (i.customer_id.split('-')[0], i.balance) for i in r.records}
    assert got == {'1001': ('acme', D('1200.00')), '1004': ('acme', D('200.00')), '998': ('zoe', D('800.00'))}
    assert [c.number for c in r.credit_notes] == ['CM-12']
    assert sorted(c.name for c in r.customers) == ['Acme Corp', "Zoë's Café"]


def test_ungrouped_invoice_list_is_unchanged():
    text = ('Date,Transaction type,Num,Customer full name,Due date,Amount,Open balance\n'
            '10/13/2026,Invoice,1042,Bluebell Design Studio Ltd,10/27/2026,"1,250.00","1,250.00"\n')
    r = read_invoices('quickbooks', text, default_currency='USD')
    assert r.ok and r.records[0].number == '1042'


def test_qbo_sales_list_relative_statuses_and_unapplied_credit_memo_import():
    text = ('Date,Transaction type,Num,Customer full name,Due date,Amount,Open balance,Status\n'
            '10/01/2026,Invoice,1050,Acme Corp,10/20/2026,"500.00","500.00",Overdue 14 days\n'
            '10/25/2026,Invoice,1051,Acme Corp,11/14/2026,"300.00","300.00",Due in 5 days\n'
            '10/26/2026,Credit Memo,CM-3,Acme Corp,10/26/2026,-25.00,-25.00,Unapplied\n')
    r = read_invoices('quickbooks', text, default_currency='USD')
    assert r.ok, r.errors
    assert {i.number: i.status for i in r.records} == {'1050': 'open', '1051': 'open'}
    assert [c.number for c in r.credit_notes] == ['CM-3']
