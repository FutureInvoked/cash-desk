"""Draft rendering: templates in ``templates/`` filled with ledger figures.

A template is a text file whose first line is the subject (``Subject:`` or
``Předmět:``) and whose body uses ``{placeholders}``. ``render_draft`` fills
them, refuses to leave any placeholder unfilled, and returns a :class:`Draft`
whose ``figures`` are exactly the amounts, dates, invoice numbers, references
and IBANs it inserted: the required figure set Figure Lock later enforces.

Czech drafts open with "Dobrý den," because a first name would need the
vocative; English drafts greet the contact's first name ("Hi Jana,", "Dear
Jana,") or, without one, "Hello,".

History is never assumed: ``RenderContext.reminders_sent`` is how many
reminders were really sent for these invoices, and only when it is above
zero does a draft say "Second reminder" or "This follows our earlier
reminder". Ages are the real days overdue, so a first contact at day 45
says 45 days, not "four weeks".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .approval import with_hash
from .dates import format_date
from .model import DRAFT_KINDS, LANGUAGES, Customer, Draft, Figure
from .money import cs_days, format_money, format_totals, sum_by_currency

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
KINDS = DRAFT_KINDS
INTERNAL_KINDS = ("escalation",)
BANK_DETAIL_KINDS = ("stage07", "stage14", "stage21", "stage28")
STAGE_LABELS = {"en": "Reminder {n}", "cs": "Připomínka {n}"}
KIND_LABELS = {"en": {"thanks": "Thank-you", "escalation": "Escalation"}, "cs": {"thanks": "Poděkování", "escalation": "Eskalace"}}
NONE_RECORDED = {"en": "none recorded", "cs": "zatím žádná"}
_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
_SUBJECT_PREFIX = re.compile(r"^(Subject|Předmět):\s*")
_templates: dict[tuple[str, str], str] = {}


class TemplateError(ValueError):
    """A draft could not be rendered completely; nothing partial is returned."""


@dataclass(frozen=True)
class DraftLine:
    """One invoice as it appears in a draft."""

    number: str
    due_date: dt.date
    amount: Decimal
    currency: str
    days_overdue: int = 0
    reference: str = ""
    iban: str = ""


@dataclass(frozen=True)
class RenderContext:
    customer: Customer
    lines: tuple[DraftLine, ...]
    sender: str
    as_of: dt.date
    stages_drafted: Mapping[str, str] = field(default_factory=dict)
    created_at: dt.datetime | None = None
    draft_id: str | None = None
    reminders_sent: int = 0


def load_template(kind: str, lang: str) -> str:
    """The raw template text for ``kind`` in ``lang`` (package data)."""
    if kind not in KINDS:
        raise TemplateError(f"unknown draft kind {kind!r}; expected one of {', '.join(KINDS)}")
    if lang not in LANGUAGES:
        raise TemplateError(f"unknown language {lang!r}; expected one of {', '.join(LANGUAGES)}")
    key = (kind, lang)
    if key not in _templates:
        name = f"{kind}.txt" if lang == "en" else f"{kind}_{lang}.txt"
        path = TEMPLATE_DIR / name
        try:
            _templates[key] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TemplateError(f"template {name} is missing: {exc}") from exc
    return _templates[key]


class _Strict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def fill_template(text: str, mapping: Mapping[str, object]) -> str:
    """Fill ``{placeholders}``; an unknown or unfilled placeholder is a TemplateError."""
    try:
        out = text.format_map(_Strict(mapping))
    except KeyError as exc:
        raise TemplateError(f"unknown placeholder {{{exc.args[0]}}}") from exc
    except (ValueError, IndexError) as exc:
        raise TemplateError(f"malformed template: {exc}") from exc
    left = _PLACEHOLDER.search(out)
    if left:
        raise TemplateError(f"placeholder left in output: {left.group(0)}")
    return out


def format_iban(iban: str) -> str:
    compact = "".join(iban.split()).upper()
    return " ".join(compact[i:i + 4] for i in range(0, len(compact), 4))


def invoice_line(line: DraftLine, lang: str) -> str:
    """``  - INV-1 due 2026-10-27: EUR 1,250.00 (14 days overdue), reference 20260042`` (cs: faktura ..., splatná ...)."""
    due = format_date(line.due_date, lang)
    amount = format_money(line.amount, line.currency, lang)
    if lang == "cs":
        late = f" ({cs_days(line.days_overdue)} po splatnosti)" if line.days_overdue > 0 else ""
        ref = f", variabilní symbol {line.reference}" if line.reference else ""
        return f"  - faktura {line.number}, splatná {due}: {amount}{late}{ref}"
    days = line.days_overdue
    late = f" ({days} day{'s' if days != 1 else ''} overdue)" if days > 0 else ""
    ref = f", reference {line.reference}" if line.reference else ""
    return f"  - {line.number} due {due}: {amount}{late}{ref}"


def payment_details(lines: Sequence[DraftLine], lang: str) -> str:
    """Bank details line: one IBAN per currency; empty when no line carries an IBAN."""
    ibans: dict[str, str] = {}
    for line in lines:
        if line.iban:
            ibans.setdefault("".join(line.iban.split()).upper(), line.currency)
    if not ibans:
        return ""
    if lang == "cs":
        parts = [f"IBAN {format_iban(i)} ({c})" if len(ibans) == 1 else f"IBAN {format_iban(i)} pro {c}" for i, c in ibans.items()]
        return f"Bankovní spojení: {', '.join(parts)}. Při platbě prosím uveďte variabilní symbol."
    parts = [f"IBAN {format_iban(i)} ({c})" if len(ibans) == 1 else f"IBAN {format_iban(i)} for {c}" for i, c in ibans.items()]
    return f"Bank details: {', '.join(parts)}. Please quote the payment reference with your transfer."


def en_days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def cs_invoices(n: int) -> str:
    """Czech invoice count: 1 faktura, 2-4 faktury, otherwise faktur."""
    if n == 1:
        return "1 faktura"
    if 2 <= n <= 4:
        return f"{n} faktury"
    return f"{n} faktur"


def overdue_summary(lines: Sequence[DraftLine], lang: str) -> str:
    """Subject wording: ``invoice INV-1 is 7 days overdue`` or ``2 invoices overdue, the oldest by 9 days``."""
    n = len(lines)
    days = max(l.days_overdue for l in lines)
    if lang == "cs":
        if n == 1:
            return f"faktura {lines[0].number} je {cs_days(days)} po splatnosti"
        return f"{cs_invoices(n)} po splatnosti, nejstarší {cs_days(days)}"
    if n == 1:
        return f"invoice {lines[0].number} is {en_days(days)} overdue"
    return f"{n} invoices overdue, the oldest by {en_days(days)}"


HISTORY = {
    "en": ("", "This follows our earlier reminder. ", "This follows our earlier reminders. "),
    "cs": ("", "Navazujeme tím na naši předchozí připomínku. ", "Navazujeme tím na naše předchozí připomínky. "),
}
REMINDER_LEAD = {"en": ("Reminder", "Second reminder", "Further reminder"), "cs": ("Připomínka", "Druhá připomínka", "Další připomínka")}


def _by_history(table: Mapping[str, tuple[str, str, str]], lang: str, sent: int) -> str:
    return table[lang][min(max(sent, 0), 2)]


def stage_label(stage: str, lang: str) -> str:
    if stage.startswith("stage"):
        return STAGE_LABELS[lang].format(n=int(stage[5:]))
    return KIND_LABELS[lang].get(stage, stage)


def line_figures(lines: Sequence[DraftLine], include_iban: bool) -> list[Figure]:
    out: list[Figure] = []
    for line in lines:
        out.append(Figure.money(line.amount, line.currency))
        out.append(Figure.date(line.due_date))
        out.append(Figure.invoice(line.number))
        if line.reference:
            out.append(Figure.reference(line.reference))
        if include_iban and line.iban:
            out.append(Figure.iban(line.iban))
    for currency, total in sum_by_currency((l.currency, l.amount) for l in lines).items():
        out.append(Figure.money(total, currency))
    return list(dict.fromkeys(out))


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _ascii_word(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return next((w for w in re.split(r"[^a-z0-9]+", folded) if w), "")


def _draft_id(kind: str, customer_id: str, as_of: dt.date, numbers: Sequence[str], customer_name: str = "") -> str:
    """A name a person can read and find: customer, kind, first invoice, day.
    e.g. northgate-reminder7-0041-nov10, fernhill-thanks-0045-nov10, stonebridge-escalation-0027-nov10.
    Every invoice is in it (birchfield-reminder28-0031-0044-nov10), so a different set of invoices on
    the same day is a different draft; past three invoices, the first two plus a short hash."""
    who = _ascii_word(customer_name) or _ascii_word(customer_id) or "customer"
    what = f"reminder{int(kind[5:])}" if kind.startswith("stage") and kind[5:].isdigit() else kind
    ordered = sorted(numbers)
    tails = [re.sub(r"[^A-Za-z0-9]+", "", n)[-4:].lower() for n in ordered]
    if len(tails) > 3:
        tails = tails[:2] + [hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()[:4]]
    which = "-".join(t for t in tails if t) or hashlib.sha256(customer_id.encode("utf-8")).hexdigest()[:4]
    return f"{who}-{what}-{which}-{_MONTHS[as_of.month - 1]}{as_of.day}"


def render_draft(kind: str, lang: str, context: RenderContext) -> Draft:
    """Fill the template for ``kind``/``lang`` and return a hashed Draft.

    Fails (TemplateError) rather than render a blank: no lines, no sender, an
    English template without a contact first name, or a placeholder left over.
    """
    template = load_template(kind, lang)
    lines = tuple(context.lines)
    if not lines:
        raise TemplateError("nothing to render: the draft has no invoice lines")
    sender = (context.sender or "").strip()
    if not sender:
        raise TemplateError("a sender is required: every draft is signed by the business that issued the invoices")
    customer = context.customer
    first_name = customer.first_name
    if "{first_name}" in template and not first_name:
        raise TemplateError(f"customer {customer.id} has no contact name; the {lang} template greets by first name (use {{greeting_hi}} or {{greeting_dear}})")
    totals = sum_by_currency((l.currency, l.amount) for l in lines)
    details = payment_details(lines, lang) if kind in BANK_DETAIL_KINDS else ""
    stages = ", ".join(f"{l.number}: {stage_label(context.stages_drafted[l.number], lang).lower()}" for l in lines if context.stages_drafted.get(l.number)) or NONE_RECORDED[lang]
    n = len(lines)
    days_max = max(l.days_overdue for l in lines)
    sent = context.reminders_sent
    mapping = {
        "count": n,
        # English agreement, so a one-invoice reminder never reads "1 invoice(s)".
        "invoices": f"{n} invoice" if n == 1 else f"{n} invoices",
        "invoice_s": "invoice" if n == 1 else "invoices",
        "is_are": "is" if n == 1 else "are",
        "has_have": "has" if n == 1 else "have",
        "its_their": "its" if n == 1 else "their",
        "remain_s": "remains" if n == 1 else "remain",
        "oldest_is": "It is" if n == 1 else "The oldest is",
        "age28": "four weeks overdue" if n == 1 else "overdue, the oldest by four weeks",
        "total": format_totals(totals, lang),
        "first_name": first_name,
        "invoice_lines": "\n".join(invoice_line(l, lang) for l in lines),
        "payment_details": details,
        "sender": sender,
        "customer": customer.name,
        "stages": stages,
        "days_max": days_max,
        "age": cs_days(days_max) if lang == "cs" else en_days(days_max),
        "overdue_summary": overdue_summary(lines, lang),
        "greeting_hi": f"Hi {first_name}," if first_name else "Hello,",
        "greeting_dear": f"Dear {first_name}," if first_name else "Hello,",
        "history": _by_history(HISTORY, lang, sent),
        "reminder_lead": _by_history(REMINDER_LEAD, lang, sent),
    }
    subject_line, _, body_template = template.partition("\n")
    subject = fill_template(_SUBJECT_PREFIX.sub("", subject_line.strip()), mapping).strip()
    body = fill_template(body_template, mapping)
    body = re.sub(r"\n{3,}", "\n\n", body).strip("\n") + "\n"
    recipients: tuple[str, ...] = () if kind in INTERNAL_KINDS else ((customer.email,) if customer.email else ())
    numbers = tuple(l.number for l in lines)
    draft = Draft(
        id=context.draft_id or _draft_id(kind, customer.id, context.as_of, numbers, customer.name),
        kind=kind,
        customer_id=customer.id,
        invoice_numbers=numbers,
        language=lang,
        subject=subject,
        body=body,
        recipients=recipients,
        created_at=context.created_at or dt.datetime.now(dt.timezone.utc),
        figures=tuple(line_figures(lines, include_iban=bool(details))),
    )
    return with_hash(draft)


__all__ = [
    "KINDS",
    "TEMPLATE_DIR",
    "TemplateError",
    "DraftLine",
    "RenderContext",
    "load_template",
    "fill_template",
    "format_iban",
    "invoice_line",
    "payment_details",
    "stage_label",
    "overdue_summary",
    "line_figures",
    "render_draft",
]
