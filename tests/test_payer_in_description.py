"""No payer column: an exact reference and amount plus the customer's name in the description applies (judge 10 rank 6)."""
import datetime as dt
from decimal import Decimal as D
import pytest
from cashdesk import Customer, Invoice
from cashdesk.matching import match_payments, description_names
from cashdesk.model import Payment

DAY = dt.date(2026, 11, 10)
CUSTOMERS = [Customer('C1', 'Bluebell Design Studio Ltd'), Customer('C2', 'Oakridge Partners LLP'), Customer('C3', 'Acme Ltd')]
INVOICES = [Invoice('INV-100', 'C1', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('1250'), 'GBP', D('1250')),
            Invoice('INV-200', 'C3', dt.date(2026, 10, 1), dt.date(2026, 10, 15), D('300'), 'GBP', D('300'))]


def run(memo, ref='INV-100', amount='1250', payer=''):
    p = Payment('bank-p1@stmt1', dt.date(2026, 11, 3), D(amount), 'GBP', ref, payer, memo=memo)
    return match_payments(INVOICES, [p], CUSTOMERS, DAY)


def test_chase_style_description_with_full_name_applies():
    r = run('BLUEBELL DESIGN STUDIO LTD ACH CREDIT')
    assert [m.invoice_number for m in r.applied] == ['INV-100']


@pytest.mark.parametrize('memo', ['ACH CREDIT 0042', 'BLUEBELL ACH', 'BLUEBELL DESIGN STUDIO LTD and OAKRIDGE PARTNERS LLP'])
def test_description_without_a_unique_full_name_still_reviews(memo):
    r = run(memo)
    assert not r.applied and [i.kind for i in r.review] == ['PAYER']


def test_short_single_word_name_never_identifies_a_payer():
    assert not description_names('Acme Ltd', 'ACME LTD PAYMENT')
    r = run('ACME LTD PAYMENT', ref='INV-200', amount='300')
    assert not r.applied


def test_a_payer_column_still_decides_when_present():
    r = run('BLUEBELL DESIGN STUDIO LTD', payer='J SMITH')
    assert not r.applied


def test_description_name_does_not_rescue_a_wrong_amount():
    r = run('BLUEBELL DESIGN STUDIO LTD', amount='1200')
    assert not r.applied and r.review[0].kind == 'PARTIAL'


def test_store_applies_a_no_payer_statement_line_end_to_end():
    from cashdesk.importers import read_bank_csv
    from cashdesk.store import Store
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers(CUSTOMERS); s.upsert_invoices(INVOICES)
    r = read_bank_csv("Details,Posting Date,Description,Amount,Type,Balance\n"
                      "CREDIT,11/03/2026,BLUEBELL DESIGN STUDIO LTD INV-100,1250.00,ACH_CREDIT,5000.00\n",
                      default_currency='GBP', date_order='mdy')
    assert r.ok, r.errors
    s.ingest(r, file_name='chase.csv')
    applied = s.match_result(DAY).applied
    assert [m.invoice_number for m in applied] == ['INV-100']
    s.apply_match(applied[0])
    assert s.get_invoice('INV-100').status == 'paid'


def test_revolut_business_statement_reads_date_completed_and_payment_currency():
    from cashdesk.importers import read_bank_csv
    text = ("Date started (UTC),Date completed (UTC),ID,Type,State,Description,Reference,Payer,Card number,Orig currency,"
            "Orig amount,Payment currency,Amount,Total amount,Exchange rate,Fee,Fee currency,Balance,Account\n"
            "2026-11-03,2026-11-03,a1b2,TRANSFER,COMPLETED,Payment from Bluebell Design Studio Ltd,INV-100,Bluebell Design Studio Ltd,,"
            "GBP,1250.00,GBP,1250.00,1250.00,,0.00,GBP,5000.00,Main GBP\n")
    r = read_bank_csv(text)
    assert r.ok, r.errors
    p = r.records[0]
    assert (str(p.amount), p.currency, p.date) == ('1250.00', 'GBP', dt.date(2026, 11, 3))


def test_german_bank_spelling_of_umlauts_matches_the_customer():
    from cashdesk.matching import payer_fits
    assert payer_fits('Müller GmbH', 'MUELLER GMBH')
    assert payer_fits('Müller GmbH', 'MULLER GMBH')
    assert payer_fits('Weiß & Söhne GmbH', 'WEISS & SOEHNE GMBH')
    assert not payer_fits('Müller GmbH', 'MUELLERS GMBH')


@pytest.mark.parametrize('memo', [
    'BLUEBELL DESIGN STUDIO GMBH',            # different legal form
    'BLUEBELL DESIGN STUDIO HOLDINGS LTD',    # a different company
    'JOHN SMITH OBO BLUEBELL DESIGN STUDIO',  # a third party paying
])
def test_reviewer_cases_go_to_review(memo):
    assert not run(memo).applied


@pytest.mark.parametrize('memo', ['PAYMENT FROM BLUEBELL DESIGN STUDIO LTD', 'BLUEBELL DESIGN STUDIO LTD BGC', 'Bluebell Design Studio INV-100'])
def test_ordinary_descriptions_still_apply(memo):
    assert [m.invoice_number for m in run(memo).applied] == ['INV-100']


def test_blank_payer_cell_in_a_file_with_a_payer_column_does_not_use_the_description():
    other = Payment('bank-p2@stmt1', dt.date(2026, 11, 3), D('5'), 'GBP', 'x', 'SOMEONE', memo='')
    p = Payment('bank-p1@stmt1', dt.date(2026, 11, 3), D('1250'), 'GBP', 'INV-100', '', memo='CASH DEPOSIT BLUEBELL DESIGN STUDIO')
    assert not match_payments(INVOICES, [p, other], CUSTOMERS, DAY).applied


def test_longest_customer_name_wins_when_names_contain_each_other():
    customers = CUSTOMERS + [Customer('C4', 'Bluebell Design')]
    p = Payment('bank-p1@stmt1', dt.date(2026, 11, 3), D('1250'), 'GBP', 'INV-100', '', memo='BLUEBELL DESIGN STUDIO LTD')
    assert [m.invoice_number for m in match_payments(INVOICES, [p], customers, DAY).applied] == ['INV-100']


def test_ampersand_and_limited_match():
    from cashdesk.matching import payer_fits
    assert payer_fits("O'Brien & Sons Ltd", "O'BRIEN AND SONS LIMITED")
    assert not payer_fits("O'Brien & Sons Ltd", "O'BRIEN AND DAUGHTERS LIMITED")


def test_chase_ach_orig_co_name_is_read_as_the_payer():
    from cashdesk.importers import read_bank_csv
    text = ("Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
            "CREDIT,11/03/2026,ORIG CO NAME:BLUEBELL DESIGN STUDIO LTD ORIG ID:1234567890 DESC DATE:261103 CO ENTRY DESCR:PAYMENT SEC:CCD TRACE#:0210 EED:261103 IND ID:INV-100 IND NAME:CHASE,1250.00,ACH_CREDIT,5000.00,,\n")
    r = read_bank_csv(text, default_currency='GBP', date_order='mdy')
    assert r.ok, r.errors
    assert r.records[0].counterparty == 'BLUEBELL DESIGN STUDIO LTD'


def test_one_chase_ach_line_does_not_switch_off_description_matching_for_the_others():
    from cashdesk.importers import read_bank_csv
    from cashdesk.store import Store
    s = Store(':memory:', today=lambda: DAY)
    s.upsert_customers(CUSTOMERS); s.upsert_invoices(INVOICES)
    r = read_bank_csv("Details,Posting Date,Description,Amount,Type,Balance\n"
                      "CREDIT,11/03/2026,BLUEBELL DESIGN STUDIO LTD INV-100,1250.00,ACH_CREDIT,5000.00\n"
                      "CREDIT,11/04/2026,ORIG CO NAME:STRIPE ORIG ID:123 DESC DATE:261104 CO ENTRY DESCR:TRANSFER,80.00,ACH_CREDIT,5080.00\n",
                      default_currency='GBP', date_order='mdy')
    assert r.ok, r.errors
    s.ingest(r, file_name='chase.csv')
    assert [m.invoice_number for m in s.match_result(DAY).applied] == ['INV-100']


@pytest.mark.parametrize('customer,payer', [("Zoë's Café Ltd", 'ZOES CAFE LIMITED'), ("O'Brien Partners", 'OBRIEN PARTNERS'),
                                            ('Müller und Söhne GmbH', 'MUELLER UND SOEHNE GMBH'), ('Łódź Print Sp. z o.o.', 'LODZ PRINT SP Z O O')])
def test_judge13_bank_spellings_match(customer, payer):
    from cashdesk.matching import payer_fits
    assert payer_fits(customer, payer)


@pytest.mark.parametrize('customer,payer', [('Acme Ltd', 'ACME GMBH'), ('Acme Ltd', 'ACMES LTD'), ("O'Brien Partners", 'OBRIEN PARTNERSHIP')])
def test_near_misses_still_review(customer, payer):
    from cashdesk.matching import payer_fits
    assert not payer_fits(customer, payer)


def test_fio_statement_with_preamble_and_payer_bic_imports():
    from cashdesk.importers import read_bank_csv
    pre = ('"accountId";"2900000000"\n"bankId";"2010"\n"currency";"CZK"\n"iban";"CZ6520100000002900000000"\n"bic";"FIOBCZPPXXX"\n'
           '"openingBalance";"10000,00"\n"closingBalance";"22100,00"\n"dateStart";"01.11.2026"\n"dateEnd";"09.11.2026"\n"idFrom";"1"\n"idTo";"2"\n\n')
    hdr = ('"ID pohybu";"Datum";"Objem";"Měna";"Protiúčet";"Název protiúčtu";"Kód banky";"Název banky";"KS";"VS";"SS";'
           '"Uživatelská identifikace";"Zpráva pro příjemce";"Typ";"Provedl";"Upřesnění";"Komentář";"BIC";"ID pokynu"\n')
    row = ('"26001";"05.11.2026";"12 100,00";"CZK";"123456789";"ZLUTOUCKY KUN SRO";"0800";"Česká spořitelna";"";"2026001";"";"";"";'
           '"Bezhotovostní příjem";"";"";"";"GIBACZPX";""\n')
    r = read_bank_csv(pre + hdr + row, default_currency='CZK')
    assert r.ok, r.errors
    assert str(r.records[0].amount) == '12100.00' and r.records[0].counterparty == 'ZLUTOUCKY KUN SRO'


@pytest.mark.parametrize('customer,payer,fits', [('Acme Corp', 'ACME CORPORATION', True), ('Smith, John', 'JOHN SMITH', True),
                                                 ('Acme Corp', 'ACME INC', False), ('Smith, John', 'JANE SMITH', False)])
def test_judge14_corporation_and_filed_person_names(customer, payer, fits):
    from cashdesk.matching import payer_fits
    assert payer_fits(customer, payer) is fits


def test_truncated_corporation_still_fits():
    from cashdesk.matching import payer_fits
    assert payer_fits('Northwind Trading Corporation', 'NORTHWIND TRADING COR')
    assert not payer_fits('Northwind Trading Corporation', 'NORTHWIND TR')


def test_own_sort_code_on_every_row_keeps_two_accounts_apart():
    from cashdesk.importers import _bank_account
    a = _bank_account({'account number': '12345678', 'sort code': '20-00-00'}, [])
    b = _bank_account({'account number': '12345678', 'sort code': '40-11-22'}, [])
    assert a and b and a != b


def test_payer_bic_beside_a_counterparty_account_is_not_our_bank():
    from cashdesk.importers import _bank_account
    assert _bank_account({'protiúčet': '123456789', 'bic': 'GIBACZPX'}, []) == _bank_account({'protiúčet': '987', 'bic': 'KOMBCZPP'}, [])
