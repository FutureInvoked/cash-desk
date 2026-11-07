"""figure_lock.py: every amount, date, invoice number, reference and IBAN in a
text is extracted in canonical form; a rewrite passes only when every
required figure is present and nothing outside the ledger's figures appears."""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

import pytest

from cashdesk.figure_lock import (
    Figures,
    LockResult,
    RevisionResult,
    check,
    extract_figures,
    iban_valid,
    ledger_figures,
    revise,
    spelled_number,
)
from cashdesk.model import Customer, Figure, Invoice
from cashdesk.render import DraftLine, RenderContext, format_iban, render_draft

D = Decimal
NBSP = " "
VALID_IBANS = [
    "CZ6508000000192000145399",
    "DE89370400440532013000",
    "GB82WEST12345698765432",
    "NL91ABNA0417164300",
    "AT611904300234573201",
    "SK3112000000198742637541",
    "FR1420041010050500013M02606",
    "PL61109010140000071219812874",
]


def money(value: str, currency: str) -> Figure:
    return Figure.money(D(value), currency)


def date(iso: str) -> Figure:
    return Figure.date(dt.date.fromisoformat(iso))


# ------------------------------------------------------------------ helpers


def test_iban_checksum():
    for iban in VALID_IBANS:
        assert iban_valid(iban), iban
        assert iban_valid(format_iban(iban))
    assert not iban_valid("DE89370400440532013001")
    assert not iban_valid("CZ6508000000192000145398")
    assert not iban_valid("XX00")
    assert not iban_valid("")


@pytest.mark.parametrize(
    "words, lang, expected",
    [
        ("twelve hundred and fifty", "en", "1250"),
        ("one thousand two hundred and fifty", "en", "1250"),
        ("two thousand", "en", "2000"),
        ("ninety-nine", "en", "99"),
        ("forty two", "en", "42"),
        ("one million", "en", "1000000"),
        ("tisíc dvě stě padesát", "cs", "1250"),
        ("dva tisíce", "cs", "2000"),
        ("pět set", "cs", "500"),
        ("tři sta dvacet jedna", "cs", "321"),
        ("sto", "cs", "100"),
        ("čtyřicet osm tisíc čtyři sta", "cs", "48400"),
        ("", "en", None),
        ("hundred and", "en", "100"),
        ("banana", "en", None),
    ],
)
def test_spelled_number(words, lang, expected):
    result = spelled_number(words, lang)
    assert result == (D(expected) if expected is not None else None)


# -------------------------------------------------------------- extraction


def test_extracts_each_kind_in_canonical_form_en():
    text = ("Hi Klaus,\n\n  - INV-2026-0042 due 2026-10-27: EUR 1,250.00 (14 days overdue), reference 20260042\n\n"
            "Bank details: IBAN DE89 3704 0044 0532 0130 00 (EUR). Please quote the payment reference with your transfer.\n")
    found = extract_figures(text, "en")
    assert isinstance(found, Figures)
    assert set(found) == {money("1250.00", "EUR"), date("2026-10-27"), Figure.invoice("INV-2026-0042"), Figure.reference("20260042"), Figure.iban("DE89370400440532013000")}
    by_kind = {f.kind: f for f in found}
    assert by_kind["money"].line == 3 and by_kind["iban"].line == 5
    assert by_kind["money"].raw == "EUR 1,250.00"


def test_extracts_each_kind_in_canonical_form_cs():
    text = (f"Dobrý den,\n\n  - faktura INV-2026-0042, splatná 27. 10. 2026: 1{NBSP}250,00{NBSP}EUR (14 dní po splatnosti), variabilní symbol 20260042\n\n"
            "Bankovní spojení: IBAN CZ65 0800 0000 1920 0014 5399 (CZK).\n")
    found = set(extract_figures(text, "cs"))
    assert found == {money("1250.00", "EUR"), date("2026-10-27"), Figure.invoice("INV-2026-0042"), Figure.reference("20260042"), Figure.iban("CZ6508000000192000145399")}


@pytest.mark.parametrize(
    "text, lang, expected",
    [
        ("€1,250.00", "en", ("EUR", "1250.00")),
        ("€ 1,250", "en", ("EUR", "1250.00")),
        ("1,250.00 EUR", "en", ("EUR", "1250.00")),
        ("1250.00 EUR", "en", ("EUR", "1250.00")),
        ("EUR 1250", "en", ("EUR", "1250.00")),
        ("1.250,00 €", "en", ("EUR", "1250.00")),
        ("1 250,00 Kč", "cs", ("CZK", "1250.00")),
        ("1.250,00 Kč", "cs", ("CZK", "1250.00")),
        ("CZK 1 250", "cs", ("CZK", "1250.00")),
        ("12 500,- Kč", "cs", ("CZK", "12500.00")),
        ("1,250 euros", "en", ("EUR", "1250.00")),
        ("1 250 korun", "cs", ("CZK", "1250.00")),
        ("1,250.00", "en", (None, "1250.00")),
        ("1 250,00", "cs", (None, "1250.00")),
        ("1.250", "cs", (None, "1250.00")),
        ("1,250", "en", (None, "1250.00")),
    ],
)
def test_money_formats_are_extracted(text, lang, expected):
    found = extract_figures(f"Amount: {text} today.", lang).of("money")
    assert len(found) == 1, found
    assert (found[0].currency, str(found[0].amount)) == expected


@pytest.mark.parametrize(
    "text, lang, iso",
    [
        ("2026-11-05", "en", "2026-11-05"),
        ("5 November 2026", "en", "2026-11-05"),
        ("5th November 2026", "en", "2026-11-05"),
        ("November 5, 2026", "en", "2026-11-05"),
        ("11/05/2026", "en", "2026-11-05"),
        ("05/11/2026", "cs", "2026-11-05"),
        ("5. 11. 2026", "cs", "2026-11-05"),
        ("5.11.2026", "cs", "2026-11-05"),
        ("5. listopadu 2026", "cs", "2026-11-05"),
        ("5. listopadu 2026", "en", "2026-11-05"),
    ],
)
def test_date_formats_are_extracted(text, lang, iso):
    found = extract_figures(f"Due {text}.", lang).of("date")
    assert [f.canonical for f in found] == [iso]


def test_plain_counts_years_and_short_numbers_are_not_figures():
    text = "2 invoice(s), 14 days overdue, within 7 days, in 2026, 30 days, call +420 601 234 567."
    assert len(extract_figures(text, "en")) == 0


def test_long_digit_strings_are_references_and_company_ids_are_flagged():
    found = extract_figures("IČO 12345678, VS 20260042", "cs")
    assert set(found) == {Figure.reference("12345678"), Figure.reference("20260042")}


def test_spelled_out_amounts_count_as_money():
    assert set(extract_figures("That is twelve hundred and fifty euros in total.", "en")) == {money("1250.00", "EUR")}
    assert set(extract_figures("Celkem tisíc dvě stě padesát korun.", "cs")) == {money("1250.00", "CZK")}
    assert set(extract_figures("forty-eight thousand four hundred crowns", "en")) == {money("48400.00", "CZK")}


def test_signed_amounts_keep_their_sign():
    assert set(extract_figures("Balance: EUR -12.50 and -5,00 Kč and −12.50 EUR.", "en")) == {money("-12.50", "EUR"), money("-5.00", "CZK")}
    required = (money("12.50", "EUR"),)
    result = check("Please pay EUR -12.50.", "en", required, required)
    assert not result.ok and result.changed == ((money("12.50", "EUR"), money("-12.50", "EUR")),)
    assert check("Invoice INV-2026-0042, pages 10-12, due in 7-10 days.", "en", (), (Figure.invoice("INV-2026-0042"),)).ok


def test_invalid_iban_is_extracted_as_invalid():
    found = extract_figures("Pay to DE89 3704 0044 0532 0130 01 please.", "en").of("iban")
    assert len(found) == 1 and found[0].valid is False and found[0].canonical == "DE89370400440532013001"


def test_emails_and_urls_are_not_figures():
    assert len(extract_figures("Write to c004@northgate.example or see https://example.test/inv/2026-0042", "en")) == 0


def test_ledger_figures_cover_every_figure_of_the_customer():
    inv = Invoice("INV-2026-0042", "C002", dt.date(2026, 10, 13), dt.date(2026, 10, 27), D("1250"), "EUR", D("1000"), "open", "20260042", "DE89 3704 0044 0532 0130 00")
    allowed = ledger_figures([inv], extra=[Figure.reference("12345678")])
    assert {money("1250.00", "EUR"), money("1000.00", "EUR"), date("2026-10-27"), date("2026-10-13"), Figure.invoice("INV-2026-0042"),
            Figure.reference("20260042"), Figure.iban("DE89370400440532013000"), Figure.reference("12345678")} <= set(allowed)


# ---------------------------------------------------------------- check


REQUIRED = (money("1250.00", "EUR"), date("2026-10-27"), Figure.invoice("INV-2026-0042"), Figure.reference("20260042"), Figure.iban("DE89370400440532013000"))
ALLOWED = REQUIRED + (money("980.00", "EUR"), date("2026-11-03"), Figure.invoice("INV-2026-0041"), Figure.reference("20260041"))
TEXT = ("Second reminder: 1 invoice(s) overdue (EUR 1,250.00)\nHi Petr,\n\n  - INV-2026-0042 due 2026-10-27: EUR 1,250.00 (14 days overdue), reference 20260042\n\n"
        "Bank details: IBAN DE89 3704 0044 0532 0130 00 (EUR). Please quote the payment reference with your transfer.\n\nKind regards,\nExample Studio\n")


def test_rendered_text_passes():
    result = check(TEXT, "en", REQUIRED, ALLOWED)
    assert isinstance(result, LockResult) and result.ok
    assert result.missing == () and result.extra == () and result.changed == () and result.diff == ()


def test_rewording_passes_when_every_figure_stays():
    reworded = ("Dear Petr, just a gentle nudge: invoice INV-2026-0042 for €1,250.00 fell due on 27 October 2026 and is still open. "
                "Please transfer €1,250.00 to IBAN DE89370400440532013000 quoting reference 20260042. Many thanks!")
    assert check(reworded, "en", REQUIRED, ALLOWED).ok


def test_demo_case_changed_amount_and_foreign_iban_are_rejected_with_a_diff():
    text = TEXT.replace("EUR 1,250.00", "€1,520.00") + "\nOr pay to GB82 WEST 1234 5698 7654 32.\n"
    result = check(text, "en", REQUIRED, ALLOWED)
    assert not result.ok
    assert result.changed == ((money("1250.00", "EUR"), money("1520.00", "EUR")),)
    assert result.extra == (Figure.iban("GB82WEST12345698765432"),)
    assert result.missing == ()
    assert any("EUR 1,250.00 -> EUR 1,520.00" in line for line in result.diff), result.diff
    assert any("GB82WEST12345698765432" in line and "IBAN" in line for line in result.diff), result.diff


def test_one_digit_changed_in_amount():
    result = check(TEXT.replace("1,250.00", "1,350.00"), "en", REQUIRED, ALLOWED)
    assert not result.ok and result.changed[0][1] == money("1350.00", "EUR") and result.extra == ()


def test_day_and_month_swapped():
    swapped = TEXT.replace("2026-10-27", "10/27/2026")
    assert check(swapped, "en", REQUIRED, ALLOWED).ok, "month-first with the right month is the same date"
    wrong = TEXT.replace("due 2026-10-27", "due 27/10/2026")  # English reads this month-first: month 27 is not a date
    result = check(wrong, "en", REQUIRED, ALLOWED)
    assert not result.ok and result.missing == ()
    assert result.changed[0][0] == date("2026-10-27") and result.changed[0][1].valid is False
    assert any("not a valid date" in line for line in result.diff), result.diff
    cz = check(TEXT.replace("2026-10-27", "5. 11. 2026"), "en", REQUIRED, ALLOWED)
    assert not cz.ok and cz.changed == ((date("2026-10-27"), date("2026-11-05")),)


def test_invoice_number_one_character_off():
    result = check(TEXT.replace("INV-2026-0042 due", "INV-2026-0043 due"), "en", REQUIRED, ALLOWED)
    assert not result.ok
    assert (Figure.invoice("INV-2026-0042"), Figure.invoice("INV-2026-0043")) in result.changed
    assert any("INV-2026-0042 -> INV-2026-0043" in line for line in result.diff), result.diff


def test_another_ledger_figure_of_the_same_customer_is_allowed_but_required_stays_required():
    text = TEXT + "\nYour earlier invoice INV-2026-0041 for EUR 980.00 due 2026-11-03 is not yet due.\n"
    assert check(text, "en", REQUIRED, ALLOWED).ok
    dropped = TEXT.replace("reference 20260042", "")
    result = check(dropped, "en", REQUIRED, ALLOWED)
    assert not result.ok and result.missing == (Figure.reference("20260042"),)
    assert any("20260042" in line and "missing" in line.lower() for line in result.diff)


def test_amount_split_over_a_line_break_is_rejected():
    for broken in ("EUR 1,250.\n00", "EUR 1,\n250.00", "EUR 1,250\n.00"):
        result = check(TEXT.replace("EUR 1,250.00", broken, 1), "en", REQUIRED, ALLOWED)
        assert not result.ok, broken
        assert any("line break" in line for line in result.diff), (broken, result.diff)


def test_spelled_out_amount_passes_and_wrong_spelled_amount_fails():
    assert check(TEXT.replace("EUR 1,250.00 (14", "twelve hundred and fifty euros (14"), "en", REQUIRED, ALLOWED).ok
    result = check(TEXT.replace("EUR 1,250.00 (14", "twelve hundred and fifteen euros (14"), "en", REQUIRED, ALLOWED)
    assert not result.ok and result.changed[0][1] == money("1215.00", "EUR")
    cs_required = (money("1250.00", "CZK"),)
    assert check("Celkem tisíc dvě stě padesát korun.", "cs", cs_required, cs_required).ok


def test_currency_dropped_or_changed_is_a_change_not_a_pass():
    result = check(TEXT.replace("EUR 1,250.00", "1,250.00"), "en", REQUIRED, ALLOWED)
    assert not result.ok and result.changed[0][0] == money("1250.00", "EUR") and result.changed[0][1].currency is None
    assert any("currency" in line for line in result.diff), result.diff
    result = check(TEXT.replace("EUR 1,250.00", "CZK 1,250.00"), "en", REQUIRED, ALLOWED)
    assert not result.ok and result.changed[0][1] == money("1250.00", "CZK")


def test_thousands_separators_in_any_style_pass():
    for spelled in ("EUR 1.250,00", "€1 250,00", "1,250.00 EUR", "1250.00 EUR", "EUR 1,250"):
        assert check(TEXT.replace("EUR 1,250.00", spelled), "en", REQUIRED, ALLOWED).ok, spelled


def test_phone_number_is_fine_but_a_company_id_is_extra():
    assert check(TEXT + "\nCall us on +420 601 234 567.\n", "en", REQUIRED, ALLOWED).ok
    result = check(TEXT + "\nOur company id (IČO) is 12345678.\n", "en", REQUIRED, ALLOWED)
    assert not result.ok and result.extra == (Figure.reference("12345678"),)
    assert check(TEXT + "\nOur company id (IČO) is 12345678.\n", "en", REQUIRED, ALLOWED + (Figure.reference("12345678"),)).ok


def test_invalid_iban_is_extra_even_when_close_to_the_ledger_iban():
    result = check(TEXT.replace("0130 00", "0130 01"), "en", REQUIRED, ALLOWED)
    assert not result.ok
    assert (Figure.iban("DE89370400440532013000"), Figure.iban("DE89370400440532013001")) in result.changed
    assert any("checksum" in line for line in result.diff), result.diff


def test_diff_lines_are_short_and_name_the_line():
    result = check(TEXT.replace("EUR 1,250.00 (14", "EUR 1,520.00 (14"), "en", REQUIRED, ALLOWED)
    assert len(result.diff) == 1
    line = result.diff[0]
    assert line.startswith("changed:") and "line 4" in line and len(line) < 160


# ----------------------------------------------------------------- revise


def _draft():
    customer = Customer("C002", "Modrá Dílna Demo s.r.o.", "en", "petr@modra.example", "Petr Ukázka")
    line = DraftLine("INV-2026-0042", dt.date(2026, 10, 27), D("1250.00"), "EUR", 14, "20260042", "DE89370400440532013000")
    return render_draft("stage14", "en", RenderContext(customer, (line,), "Example Studio", dt.date(2026, 11, 10), created_at=dt.datetime(2026, 11, 10, 7)))


def test_revise_accepts_a_rewording_and_rehashes():
    draft = _draft()
    allowed = ledger_figures([Invoice("INV-2026-0042", "C002", dt.date(2026, 10, 13), dt.date(2026, 10, 27), D("1250"), "EUR", D("1250"), "open", "20260042", "DE89370400440532013000")])
    new_body = "Hope you are well!\n" + draft.body.replace("Kind regards,", "With thanks,")
    result = revise(draft, new_body, allowed)
    assert isinstance(result, RevisionResult) and result.ok
    assert result.draft.body == new_body and result.draft.hash != draft.hash and result.draft.id == draft.id
    assert result.draft.figures == draft.figures and result.draft.status == "pending"


def test_revise_refuses_a_changed_figure_and_keeps_the_original():
    draft = _draft()
    allowed = ledger_figures([Invoice("INV-2026-0042", "C002", dt.date(2026, 10, 13), dt.date(2026, 10, 27), D("1250"), "EUR", D("1250"), "open", "20260042", "DE89370400440532013000")])
    result = revise(draft, draft.body.replace("EUR 1,250.00", "€1,520.00") + "Or pay to GB82 WEST 1234 5698 7654 32.\n", allowed)
    assert not result.ok and result.draft == draft and result.draft.hash == draft.hash
    assert result.lock.changed and result.lock.extra and result.lock.diff


def test_revise_checks_subject_and_body_together():
    draft = _draft()
    allowed = ledger_figures([Invoice("INV-2026-0042", "C002", dt.date(2026, 10, 13), dt.date(2026, 10, 27), D("1250"), "EUR", D("1250"), "open", "20260042", "DE89370400440532013000")])
    assert revise(draft, draft.body, allowed, new_subject="Payment reminder: INV-2026-0042").ok
    assert not revise(draft, draft.body, allowed, new_subject="Payment reminder: EUR 1,520.00").ok


# -------------------------------------------------------------- generated


def _random_case(rng: random.Random):
    amount = D(rng.randrange(100, 99_999_999)) / 100
    currency = rng.choice(["EUR", "CZK"])
    day = dt.date(2024, 1, 1) + dt.timedelta(days=rng.randrange(0, 1800))
    number = f"{rng.choice(['INV', 'FA', 'GA', 'FI'])}-{rng.randrange(2024, 2029)}-{rng.randrange(1, 9999):04d}"
    reference = str(rng.randrange(100_000, 9_999_999_999))
    iban = rng.choice(VALID_IBANS)
    return amount, currency, day, number, reference, iban


def _spell_money(amount: Decimal, currency: str, lang: str, rng: random.Random) -> str:
    cents = f"{amount:,.2f}"
    if lang == "cs":
        cz = cents.replace(",", "X").replace(".", ",").replace("X", NBSP)
        symbol = "Kč" if currency == "CZK" else currency
        dotted = cents.replace(",", "X").replace(".", ",").replace("X", ".")
        return rng.choice([f"{cz} {symbol}", f"{cz}{NBSP}{symbol}", f"{currency} {cz}", f"{dotted} {symbol}"])
    plain = f"{amount:.2f}"
    return rng.choice([f"{currency} {cents}", f"{cents} {currency}", f"{plain} {currency}", f"€{cents}" if currency == "EUR" else f"CZK {cents}", f"{currency}{cents}"])


def _spell_date(day: dt.date, lang: str, rng: random.Random) -> str:
    from cashdesk.dates import MONTHS_CS, MONTHS_EN
    en_name = next(k for k, v in MONTHS_EN.items() if v == day.month)  # full names come first
    cs_name = next(k for k, v in MONTHS_CS.items() if v == day.month and k.endswith(("a", "u", "e", "í")))
    if lang == "cs":
        return rng.choice([day.isoformat(), f"{day.day}. {day.month}. {day.year}", f"{day.day}.{day.month}.{day.year}", f"{day.day}. {cs_name} {day.year}", f"{day:%d/%m/%Y}"])
    return rng.choice([day.isoformat(), f"{day.day} {en_name.title()} {day.year}", f"{en_name.title()} {day.day}, {day.year}", f"{day:%m/%d/%Y}"])


@pytest.mark.parametrize("seed", range(200))
def test_generated_formats_round_trip_through_the_lock(seed):
    rng = random.Random(seed)
    lang = rng.choice(["en", "cs"])
    amount, currency, day, number, reference, iban = _random_case(rng)
    required = (Figure.money(amount, currency), Figure.date(day), Figure.invoice(number), Figure.reference(reference), Figure.iban(iban))
    spaced = format_iban(iban) if rng.random() < 0.5 else iban
    text = (f"Invoice {number} due {_spell_date(day, lang, rng)}: {_spell_money(amount, currency, lang, rng)}, reference {reference}.\n"
            f"IBAN {spaced}. Thank you.\n")
    found = extract_figures(text, lang)
    assert set(required) <= set(found), (text, sorted(f.canonical for f in found))
    result = check(text, lang, required, required)
    assert result.ok, (text, result.diff)
