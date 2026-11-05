"""render.py and templates/: en and cs drafts for four stages, the thank-you
and the escalation note, with no placeholder left and the inserted figures
returned as the draft's required figure set."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

import pytest
from helpers import english_words

from cashdesk.model import DRAFT_KINDS, Customer, Figure
from cashdesk.render import KINDS, DraftLine, RenderContext, TemplateError, fill_template, invoice_line, load_template, render_draft

D = Decimal
NBSP = " "
AS_OF = dt.date(2026, 11, 10)
CREATED = dt.datetime(2026, 11, 10, 7, 0)
TEMPLATES = Path(__file__).resolve().parent.parent / "cashdesk" / "templates"

CS = Customer("C002", "Modrá Dílna Demo s.r.o.", "cs", "petr@modra.example", "Petr Ukázka")
EN = Customer("C004", "Birchfield Example Consulting GmbH", "en", "klaus@birchfield.example", "Klaus Muster")
LINE = DraftLine("INV-2026-0042", dt.date(2026, 10, 27), D("1250.00"), "EUR", 14, "20260042", "DE89370400440532013000")
LINE2 = DraftLine("INV-2026-0036", dt.date(2026, 10, 20), D("48400.00"), "CZK", 21, "20260036", "CZ6508000000192000145399")
LINE3 = DraftLine("INV-2026-0044", dt.date(2026, 10, 31), D("980.00"), "EUR", 10, "20260044", "DE89370400440532013000")
SENDER = "Example Studio s.r.o."


def ctx(customer=EN, lines=(LINE,), **kw):
    kw.setdefault("sender", SENDER)
    kw.setdefault("as_of", AS_OF)
    kw.setdefault("created_at", CREATED)
    return RenderContext(customer, tuple(lines), **kw)


def test_template_catalogue_is_complete():
    assert KINDS == DRAFT_KINDS
    for kind in KINDS:
        for lang in ("en", "cs"):
            text = load_template(kind, lang)
            assert text.splitlines()[0].startswith(("Subject: ", "Předmět: "))
    assert sorted(p.name for p in TEMPLATES.glob("*.txt")) == sorted(f"{k}{'' if lang == 'en' else '_cs'}.txt" for k in KINDS for lang in ("en", "cs"))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("lang", ["en", "cs"])
def test_every_kind_renders_without_a_placeholder_left(kind, lang):
    customer = EN if lang == "en" else CS
    draft = render_draft(kind, lang, ctx(customer, (LINE, LINE3), stages_drafted={"INV-2026-0042": "stage14"}))
    assert "{" not in draft.subject + draft.body and "}" not in draft.subject + draft.body
    assert not draft.subject.startswith(("Subject:", "Předmět:"))
    assert draft.kind == kind and draft.language == lang and draft.customer_id == customer.id
    assert draft.invoice_numbers == ("INV-2026-0042", "INV-2026-0044")
    assert draft.status == "pending" and re.fullmatch(r"[0-9a-f]{64}", draft.hash)
    assert draft.created_at == CREATED
    assert draft.body.rstrip().endswith(SENDER)
    if kind == "escalation":
        assert draft.recipients == ()
    else:
        assert draft.recipients == (customer.email,)


def test_draft_id_is_deterministic_per_kind_customer_day_and_invoices():
    a = render_draft("stage14", "en", ctx())
    b = render_draft("stage14", "en", ctx())
    c = render_draft("stage14", "en", ctx(lines=(LINE, LINE3)))
    d = render_draft("stage14", "en", ctx(as_of=AS_OF + dt.timedelta(days=1)))
    assert a.id == b.id and a.id != c.id and a.id != d.id
    assert a.id == "birchfield-reminder14-0042-nov10" and c.id == "birchfield-reminder14-0042-0044-nov10"


def test_english_stage14_text():
    draft = render_draft("stage14", "en", ctx())
    assert draft.subject == "Reminder: invoice INV-2026-0042 is 14 days overdue (EUR 1,250.00)"
    assert draft.body.startswith("Hi Klaus,\n\nWe have not yet received payment")
    assert "  - INV-2026-0042 due 2026-10-27: EUR 1,250.00 (14 days overdue), reference 20260042" in draft.body
    assert "IBAN DE89 3704 0044 0532 0130 00" in draft.body
    assert "It is now 14 days past its due date" in draft.body
    sent = render_draft("stage14", "en", ctx(reminders_sent=1))
    assert sent.subject == "Second reminder: invoice INV-2026-0042 is 14 days overdue (EUR 1,250.00)"
    assert "Hi Klaus,\n\nThis follows our earlier reminder. We have not yet received payment" in sent.body
    assert sent.figures == draft.figures, "history wording adds no figure"


def test_czech_stage14_text():
    draft = render_draft("stage14", "cs", ctx(CS))
    assert draft.subject == f"Připomínka: faktura INV-2026-0042 je 14 dní po splatnosti (1{NBSP}250,00{NBSP}EUR)"
    assert "Nejstarší položka je již 14 dní po splatnosti. Prosíme" in draft.body and "předchozí" not in draft.body
    sent = render_draft("stage14", "cs", ctx(CS, reminders_sent=1))
    assert sent.subject.startswith("Druhá připomínka: ") and "Navazujeme tím na naši předchozí připomínku. Prosíme" in sent.body
    assert english_words(sent.body) == [] and english_words(sent.subject) == []
    assert draft.body.startswith("Dobrý den,\n")
    assert f"  - faktura INV-2026-0042, splatná 27. 10. 2026: 1{NBSP}250,00{NBSP}EUR (14 dní po splatnosti), variabilní symbol 20260042" in draft.body
    assert "IBAN DE89 3704 0044 0532 0130 00" in draft.body
    assert english_words(draft.body) == [], english_words(draft.body)
    assert english_words(draft.subject) == []


@pytest.mark.parametrize("lang", ["en", "cs"])
def test_czech_and_english_templates_claim_only_the_oldest_invoice_is_late(lang):
    for kind in ("stage14", "stage21", "stage28"):
        text = load_template(kind, lang)
        assert "oldest" in text.lower() or "nejstarší" in text.lower(), kind
        assert "are now more than 30 days" not in text and "které jsou více než 30 dní" not in text


def test_czech_templates_contain_no_english_and_no_count_placeholder():
    for kind in KINDS:
        text = load_template(kind, "cs")
        assert english_words(text) == [], (kind, english_words(text))
        assert "{count}" not in text and "faktur(y)" not in text


def test_thank_you_lists_what_was_paid_without_overdue_or_bank_details():
    draft = render_draft("thanks", "en", ctx(lines=(DraftLine("INV-2026-0045", dt.date(2026, 11, 12), D("1980.00"), "EUR", 0, "20260045", "DE89370400440532013000"),)))
    assert draft.subject == "Thank you for your payment (EUR 1,980.00)"
    assert "  - INV-2026-0045 due 2026-11-12: EUR 1,980.00, reference 20260045" in draft.body
    assert "overdue" not in draft.body and "IBAN" not in draft.body
    cs = render_draft("thanks", "cs", ctx(CS, (DraftLine("INV-2026-0043", dt.date(2026, 11, 7), D("30250.00"), "CZK", 0, "20260043", ""),)))
    assert cs.subject == f"Děkujeme za úhradu (30{NBSP}250,00{NBSP}Kč)"
    assert "po splatnosti" not in cs.body and english_words(cs.body) == []


def test_escalation_is_an_internal_note_with_stages_drafted():
    line = DraftLine("INV-2026-0027", dt.date(2026, 10, 5), D("121000.00"), "CZK", 36, "20260027", "CZ6508000000192000145399")
    en = render_draft("escalation", "en", ctx(EN, (line,), stages_drafted={"INV-2026-0027": "stage28"}))
    assert "Escalation" in en.subject and EN.name in en.subject and "CZK 121,000.00" in en.subject
    assert "Internal note" in en.body and "INV-2026-0027: reminder 28" in en.body and "36 days overdue" in en.body
    none = render_draft("escalation", "en", ctx(EN, (line,)))
    assert "none recorded" in none.body
    cs = render_draft("escalation", "cs", ctx(CS, (line,), stages_drafted={"INV-2026-0027": "stage28"}))
    assert "Eskalace" in cs.subject and "INV-2026-0027: připomínka 28" in cs.body and english_words(cs.body) == []


def test_two_currencies_are_listed_apart_with_an_iban_each():
    en = render_draft("stage28", "en", ctx(EN, (LINE, LINE2)))
    assert "(CZK 48,400.00 + EUR 1,250.00)" in en.subject
    assert "49,650" not in en.body and "49,650" not in en.subject
    assert "IBAN DE89 3704 0044 0532 0130 00" in en.body and "IBAN CZ65 0800 0000 1920 0014 5399" in en.body
    cs = render_draft("stage28", "cs", ctx(CS, (LINE, LINE2)))
    assert f"(48{NBSP}400,00{NBSP}Kč + 1{NBSP}250,00{NBSP}EUR)" in cs.subject


def test_figures_are_the_exact_inserted_values():
    draft = render_draft("stage14", "en", ctx(EN, (LINE, LINE3)))
    figures = set(draft.figures)
    assert Figure("money", "EUR 1250.00") in figures and Figure("money", "EUR 980.00") in figures
    assert Figure("money", "EUR 2230.00") in figures, "the per-currency total in the subject is an inserted figure"
    assert Figure("date", "2026-10-27") in figures and Figure("date", "2026-10-31") in figures
    assert Figure("invoice_no", "INV20260042") in figures and Figure("invoice_no", "INV20260044") in figures
    assert Figure("reference", "20260042") in figures and Figure("reference", "20260044") in figures
    assert Figure("iban", "DE89370400440532013000") in figures
    assert all(isinstance(f, Figure) for f in draft.figures)
    thanks = render_draft("thanks", "cs", ctx(CS, (LINE,)))
    assert Figure("iban", "DE89370400440532013000") not in set(thanks.figures), "no bank details in a thank-you, so none required"


def test_invoice_line_singular_days():
    one = DraftLine("X-1", dt.date(2026, 11, 9), D("5.00"), "EUR", 1)
    assert invoice_line(one, "en") == "  - X-1 due 2026-11-09: EUR 5.00 (1 day overdue)"
    assert invoice_line(one, "cs") == f"  - faktura X-1, splatná 9. 11. 2026: 5,00{NBSP}EUR (1 den po splatnosti)"


def test_missing_first_name_greets_hello_missing_sender_or_lines_fails_instead_of_blank(monkeypatch):
    nameless = Customer("C9", "No Name Ltd", "en", "x@y.example", "")
    for kind in ("stage07", "stage14", "stage21", "stage28", "thanks"):
        assert render_draft(kind, "en", ctx(nameless)).body.startswith("Hello,\n\n"), kind
    assert render_draft("stage21", "en", ctx()).body.startswith("Dear Klaus,\n")
    import cashdesk.render as render_module
    monkeypatch.setitem(render_module._templates, ("stage07", "en"), "Subject: x\n\nHi {first_name},\n\n{invoice_lines}\n{sender}\n")
    with pytest.raises(TemplateError):
        render_draft("stage07", "en", ctx(nameless))
    assert render_draft("stage07", "cs", ctx(Customer("C9", "Bez Jména s.r.o.", "cs", "x@y.example", ""))).body.startswith("Dobrý den")
    with pytest.raises(TemplateError):
        render_draft("stage07", "en", ctx(sender=""))
    with pytest.raises(TemplateError):
        render_draft("stage07", "en", ctx(lines=()))
    with pytest.raises(TemplateError):
        render_draft("stage99", "en", ctx())
    with pytest.raises(TemplateError):
        render_draft("stage07", "de", ctx())


def test_fill_template_refuses_unknown_or_leftover_placeholders():
    assert fill_template("Hi {first_name}", {"first_name": "Jana"}) == "Hi Jana"
    with pytest.raises(TemplateError):
        fill_template("Hi {nobody}", {"first_name": "Jana"})
    with pytest.raises(TemplateError):
        fill_template("Hi {first_name}", {"first_name": "{oops}"})


def test_a_stage28_subject_stays_true_when_a_newer_invoice_rides_along():
    """Birchfield's demo reminder lists INV-2026-0031 (28 days) and INV-2026-0044 (10 days):
    the subject may only say the oldest is four weeks overdue, never that all of them are."""
    newer = DraftLine("INV-2026-0044", dt.date(2026, 10, 31), D("720.00"), "EUR", 10, "20260044", "DE89370400440532013000")
    oldest = DraftLine("INV-2026-0031", dt.date(2026, 10, 13), D("3600.00"), "EUR", 28, "20260031", "DE89370400440532013000")
    en = render_draft("stage28", "en", ctx(EN, (oldest, newer)))
    assert "four weeks overdue (" not in en.subject and "oldest" in en.subject, en.subject
    cs = render_draft("stage28", "cs", ctx(CS, (oldest, newer)))
    assert "platba čtyři týdny po splatnosti" not in cs.subject and "nejstarší" in cs.subject, cs.subject


@pytest.mark.parametrize("kind", ["stage07", "stage14", "stage21", "stage28", "escalation"])
def test_english_drafts_say_invoice_or_invoices_never_invoice_s(kind):
    """'1 invoice(s)' reads like a form letter; one invoice and two invoices each read properly."""
    one = render_draft(kind, "en", ctx(EN, (LINE,)))
    two = render_draft(kind, "en", ctx(EN, (LINE, LINE3)))
    for draft in (one, two):
        assert "(s)" not in draft.subject and "(s)" not in draft.body, (kind, draft.subject)
    assert "invoice INV-2026-0042 is 14 days overdue (EUR 1,250.00)" in one.subject, one.subject
    assert "2 invoices overdue, the oldest by 14 days (EUR 2,230.00)" in two.subject, two.subject
    if kind != "escalation":
        assert "The oldest" not in one.body and "The oldest" in two.body or kind == "stage07", one.body


def test_draft_ids_are_readable_customer_kind_invoice_day():
    """Owner: 'stage07-C001-20261110-ab5628' is confusing; ids read like northgate-reminder7-0041-nov10."""
    nov10 = dt.date(2026, 11, 10)
    assert render_draft("stage14", "en", ctx(EN, (LINE,))).id == "birchfield-reminder14-0042-nov10" if AS_OF == nov10 else True
    assert render_draft("stage14", "cs", ctx(CS, (LINE,))).id.startswith("modra-reminder14-0042-")
    assert render_draft("thanks", "en", ctx(EN, (LINE,))).id.startswith("birchfield-thanks-0042-")
    assert render_draft("escalation", "en", ctx(EN, (LINE,))).id.startswith("birchfield-escalation-0042-")
