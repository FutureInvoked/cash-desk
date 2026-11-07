"""Known payer identity authorizes money; fuzzy names only rank review candidates."""
import datetime as dt
from decimal import Decimal as D
import unicodedata
import pytest
from cashdesk.model import Customer, Invoice, Payment
from cashdesk.matching import match_payments, normalise_name, payer_fits
from cashdesk.importers import read_bank_csv
from cashdesk.store import Store, ConflictError
from cashdesk.chasing import plan_followups

DAY = dt.date(2026, 11, 10)
ISSUE = dt.date(2026, 10, 1)


def invoice():
    return Invoice('INV-1001', 'C1', ISSUE, dt.date(2026, 10, 20), D('100'), 'EUR', D('100'))


def payment(payer, day=DAY):
    return Payment('P1', day, D('100'), 'EUR', 'INV-1001', payer)


BAD_NAMES = [('Smith Design Ltd', 'Jones Design Ltd'), ('Acme Ltd', 'Acne Ltd'),
             ('Alpha Trading Ltd', 'Beta Trading Ltd'), ('Riverside Roofing Ltd', 'Riverside Dental Ltd'),
             ('Northfield Design Ltd', 'Southbank Design Ltd'), ('Acme Test Studio', 'Acme'),
             ('Bluebell Design Studio', 'Bluebell Des'), ('Bluebell Design Studio', ''),
             ('Bluebell Design Studio', 'Design Bluebell Studio'), ('Co Alpha Ltd', 'Alpha Ltd'),
             ('ガーデン株式会社', 'カーデン株式会社'), ('Alpha Ltd', 'Аlpha Ltd')]


@pytest.mark.parametrize('name,payer', BAD_NAMES)
def test_payer_identity_needs_review_for_missing_shared_or_similar_names(name, payer):
    r = match_payments([invoice()], [payment(payer)], [Customer('C1', name)])
    assert not r.applied
    assert r.review[0].kind == 'PAYER'
    assert 'payer identity needs review' in r.review[0].detail
    assert 'INV-1001' in r.held_invoices
    assert not payer_fits(name, payer)


@pytest.mark.parametrize('name,payer', [('Bluebell Design Studio Ltd', 'BLUEBELL DESIGN ST'),
    ('Bluebell Design Studio Ltd', 'BLUEBELL DESIGN'), ('Müller GmbH', 'MULLER'),
    ('Nová Ukázka s.r.o.', 'NOVA UKAZKA SRO'), ('北京测试有限公司', '北京测试有限公司'),
    ('株式会社テスト', '株式会社テスト'), ('ООО Ромашка', 'ооо ромашка'),
    ('شركة الاختبار', 'شركة الاختبار'), ('परीक्षण कंपनी', 'परीक्षण कंपनी'),
    ('ガーデン株式会社', unicodedata.normalize('NFD', 'ガーデン株式会社'))])
def test_exact_unicode_and_bounded_truncated_names_apply(name, payer):
    assert payer_fits(name, payer)
    r = match_payments([invoice()], [payment(payer)], [Customer('C1', name)])
    assert len(r.applied) == 1 and not r.review
    assert 'payer identity matches one known customer' in r.applied[0].reason


@pytest.mark.parametrize('names,payer', [(['Bluebell Design Studio', 'Bluebell Design Services'], 'Bluebell Design'),
    (['Bluebell Design', 'Bluebell Design Studio'], 'Bluebell Design'),
    (['Acme Ltd', 'Acme GmbH'], 'Acme')])
def test_ambiguous_known_customer_identity_never_auto_applies(names, payer):
    r = match_payments([invoice()], [payment(payer)], [Customer('C1', names[0]), Customer('C2', names[1])])
    assert not r.applied and r.review[0].kind == 'PAYER'


def test_no_customer_record_is_not_evidence_of_payer_identity():
    r = match_payments([invoice()], [payment('C1')])
    assert not r.applied and r.review[0].kind == 'PAYER'


@pytest.mark.parametrize('offset,applied', [(-4, False), (-3, True), (-2, True), (-1, True), (0, True), (1, True)])
def test_documented_three_day_booking_grace_has_exact_boundary(offset, applied):
    r = match_payments([invoice()], [payment('Alpha Ltd', ISSUE + dt.timedelta(days=offset))], [Customer('C1', 'Alpha Ltd')])
    assert bool(r.applied) is applied
    if not applied:
        assert r.review[0].kind == 'EARLY' and 'check the dates before applying' in r.review[0].detail


@pytest.mark.parametrize('payer', ['', 'Southbank Design Ltd'])
def test_unverified_payer_preserves_debt_revokes_named_approval_and_never_thanks(payer):
    with Store(':memory:', today=lambda: DAY) as s:
        s.upsert_customers([Customer('C1', 'Northfield Design Ltd', email='ap@northfield.example')])
        s.upsert_invoices([invoice()])
        plan = plan_followups(s.list_invoices(), s.list_customers(), (), s.chase_state(), DAY, sender='Demo Studio')
        d = plan.drafts[0]; s.create_draft(d); s.approve_draft(d.id, d.hash)
        s.add_payments([payment(payer)])
        r = s.match_result(DAY)
        for m in r.applied:
            s.apply_match(m)
        assert not r.applied and r.review[0].kind == 'PAYER'
        assert s.get_invoice('INV-1001').balance == D('100') and not s.list_matches()
        assert s.approval(d.id) is None
        with pytest.raises(ConflictError): s.reserve_send(d.id, d.hash, 'eml')
        plan = plan_followups(s.list_invoices(), s.list_customers(), s.verified_matches(), s.chase_state(), DAY,
                             sender='Demo Studio', holds=s.reminder_holds(DAY))
        assert not plan.thanks and not plan.drafts and not s.list_sends()


def test_bank_description_remains_evidence_not_an_invented_verified_payer():
    csv = ('Transaction Date,Transaction Description,Debit Amount,Credit Amount,Balance\n'
           '03/11/2026,Beta GmbH INV-1001,,100.00,500.00\n')
    imported = read_bank_csv(csv, date_order='dmy')
    assert imported.ok
    p = imported.records[0]
    assert p.counterparty == '' and p.memo == 'Beta GmbH INV-1001'
    r = match_payments([invoice()], [p], [Customer('C1', 'Alpha Ltd')])
    assert not r.applied and r.review[0].kind == 'PAYER'
    assert r.review[0].payment.memo == p.memo


def test_structured_payer_alias_column_remains_usable():
    r = read_bank_csv('Date,Amount,Currency,Payer Name,Reference\n2026-11-03,100,EUR,Alpha Ltd,INV-1001\n')
    assert r.ok and r.records[0].counterparty == 'Alpha Ltd'
    assert len(match_payments([invoice()], r.records, [Customer('C1', 'Alpha Ltd')]).applied) == 1


def test_non_latin_normalization_preserves_significant_marks():
    assert normalise_name('ガーデン株式会社') != normalise_name('カーデン株式会社')
    assert normalise_name('परीक्षण कंपनी')
