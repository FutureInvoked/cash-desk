"""The morning brief names payments waiting for review and the reminders they pause (shots 1.2.0)."""
import datetime as dt
from decimal import Decimal as D
from cashdesk import Customer, Invoice, Store
from cashdesk.brief import morning_brief
from cashdesk.importers import read_bank_csv

DAY = dt.date(2026, 11, 6)


def store():
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('B', 'Birchfield Demo GmbH', email='b@birch.example'),
                        Customer('N', 'Northgate Demo Ltd', email='n@north.example')])
    s.upsert_invoices([Invoice('INV-31', 'B', dt.date(2026, 9, 1), dt.date(2026, 10, 1), D('3600'), 'EUR', D('3600')),
                       Invoice('INV-44', 'B', dt.date(2026, 9, 1), dt.date(2026, 10, 1), D('720'), 'EUR', D('720')),
                       Invoice('INV-41', 'N', dt.date(2026, 9, 1), dt.date(2026, 10, 30), D('980'), 'EUR', D('980'))])
    r = read_bank_csv('id,date,amount,currency,counterparty,reference\nB1-02,2026-11-02,700.00,EUR,BIRCHFIELD DEMO GMBH,project fee\n')
    assert r.ok, r.errors
    s.ingest(r, file_name='bank.csv')
    return s


def test_brief_lists_payment_to_review_and_paused_reminders():
    text = morning_brief(store(), DAY, 'en')
    assert '## Payments to review (1)' in text
    assert 'EUR 700.00 from BIRCHFIELD DEMO GMBH (B1-02)' in text
    paused = text.split('## Reminders paused')[1].split('##')[0]
    assert 'Birchfield Demo GmbH: INV-31, INV-44' in paused
    assert 'Northgate' not in paused


def test_brief_without_review_has_no_extra_sections():
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers([Customer('N', 'Northgate Demo Ltd', email='n@north.example')])
    s.upsert_invoices([Invoice('INV-41', 'N', dt.date(2026, 9, 1), dt.date(2026, 10, 30), D('980'), 'EUR', D('980'))])
    text = morning_brief(s, DAY, 'en')
    assert 'Payments to review' not in text and 'Reminders paused' not in text


def test_czech_brief_has_the_sections():
    text = morning_brief(store(), DAY, 'cs')
    assert 'Platby ke kontrole (1)' in text and 'Upomínky pozastaveny' in text
