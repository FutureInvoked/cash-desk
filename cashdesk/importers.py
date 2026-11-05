"""CSV importers: invoice exports from four systems, bank statements, customers.

Every reader returns an :class:`ImportResult`. A malformed row is reported in
``errors`` with its line number and raw text and never raises; a row that is
legitimately not chaseable (a draft or deleted invoice, an invoice without a
due date, a payment line in an invoice report, a pending bank line) is listed
in ``skipped`` with a ``code`` and does not refuse the file. Credit notes,
credit memos and unapplied payments are listed in ``credit_notes`` for a
person to apply; they are never chased and never dropped silently.
``fingerprint`` is a sha256 of the normalised content so a caller can refuse
to import the same file twice.

Report title lines and bank preamble lines above the table are passed over:
the header is the first line (of the first 40) that carries the columns the
reader needs.

Readers accept either CSV text or a path the caller passes in; nothing else is
read from disk.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .dates import DateOrderError, infer_date_order, parse_date
from .model import INVOICE_STATUSES, LANGUAGES, Customer, Invoice, Payment
from .money import Amount, parse_amount

SOURCES = ("generic", "xero", "quickbooks", "stripe")
DATE_ORDERS = ("auto", "dmy", "mdy")
MAX_PREAMBLE = 40

VOID_WORDS = {"void", "voided", "cancelled", "canceled", "credited", "written-off", "written off", "deleted"}
CREDIT_KINDS = ("credit_note", "unapplied_payment")


@dataclass(frozen=True)
class RowError:
    """A row that was refused (in ``errors``), skipped (in ``skipped``) or warned about (in ``warnings``).

    ``code`` names why a row was skipped: ``draft``, ``deleted``, ``no_due_date``,
    ``not_invoice``, ``bill``, ``settled_credit``, ``not_booked``; for a warning
    ``language`` or ``overpaid``; empty for an error.
    """

    row: int
    message: str
    raw: str = ""
    code: str = ""


@dataclass(frozen=True)
class CreditNote:
    """Money the customer has to their credit: a credit note or credit memo, or a payment not applied to any invoice.

    ``amount`` is the open credit as a positive number. Never chased; a person
    decides which invoice it settles.
    """

    number: str
    customer_id: str
    customer_name: str
    date: dt.date | None
    amount: Decimal
    currency: str
    kind: str = "credit_note"
    row: int = 0
    detail: str = ""


@dataclass(frozen=True)
class ImportResult:
    kind: str
    source: str
    records: tuple = ()
    customers: tuple[Customer, ...] = ()
    errors: tuple[RowError, ...] = ()
    skipped: tuple[RowError, ...] = ()
    fingerprint: str = ""
    rows: int = 0
    columns: tuple[str, ...] = field(default=())
    credit_notes: tuple[CreditNote, ...] = ()
    warnings: tuple[RowError, ...] = ()
    date_order: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def skipped_counts(self) -> dict[str, int]:
        """How many rows were skipped, per ``code``."""
        return dict(Counter(e.code or "other" for e in self.skipped))


# ---------------------------------------------------------------- text and csv


def fingerprint(text: str) -> str:
    """sha256 of the content with BOM, line endings and trailing blanks normalised."""
    lines = [line.rstrip() for line in text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def load_text(text_or_path: str | os.PathLike[str]) -> tuple[str, str | None]:
    """(text, error). A path is read; a string with a newline is CSV text."""
    if isinstance(text_or_path, os.PathLike):
        path: Path | None = Path(text_or_path)
    elif isinstance(text_or_path, str) and "\n" not in text_or_path and "\r" not in text_or_path:
        candidate = Path(text_or_path)
        if candidate.is_file():
            path = candidate
        elif text_or_path.lower().endswith((".csv", ".txt")) or os.sep in text_or_path:
            return "", f"no such file: {text_or_path}"
        else:
            return text_or_path, None
    else:
        return str(text_or_path), None
    try:
        return path.read_text(encoding="utf-8-sig"), None
    except FileNotFoundError:
        return "", f"no such file: {path}"
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="cp1250"), None
        except OSError as exc:  # pragma: no cover - defensive
            return "", f"cannot read {path}: {exc}"
    except OSError as exc:
        return "", f"cannot read {path}: {exc}"


def _delimiter(header_line: str) -> str:
    counts = {d: header_line.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


_HEADER_CURRENCY = re.compile(r"\(\s*([A-Za-z]{3})\s*\)\s*$")
HEADER_CURRENCIES = {"EUR", "USD", "GBP", "CZK", "PLN", "CHF", "SEK", "NOK", "DKK", "HUF", "RON", "BGN", "CAD", "AUD", "NZD", "JPY"}


def _norm_header(name: str) -> str:
    text = (name or "").replace("\ufeff", "").strip().lstrip("*").strip()
    if _header_currency(text):
        text = text[: _HEADER_CURRENCY.search(text).start()]  # "Betrag (EUR)" is the Betrag column, in EUR
    text = text.lower()
    text = re.sub(r"\((?:utc)\)", "", text)
    text = re.sub(r"[\s_\-./:()]+", " ", text).strip()
    return text


def _header_currency(name: str) -> str | None:
    m = _HEADER_CURRENCY.search((name or "").replace("\ufeff", "").strip())
    return m.group(1).upper() if m and m.group(1).upper() in HEADER_CURRENCIES else None


@dataclass
class _Table:
    keys: list[str]
    raw_header: list[str]
    header_line: int
    rows: list[tuple[int, dict[str, str], str, list[str]]]


def _table(text: str, required: Iterable[tuple[str, ...]] = ()) -> _Table:
    """The table in ``text``: the header is the first line carrying every ``required`` alias group.

    Lines above it (a bank's account summary, a report's title) are passed
    over. Without such a line the first non-empty line is the header, so the
    caller reports the missing columns. Row numbers are physical line numbers.
    """
    text = text.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)
    required = list(required)
    first = next((i for i, line in enumerate(lines) if line.strip()), 0)
    start = first
    if required:
        for i, line in enumerate(lines[:MAX_PREAMBLE]):
            if not line.strip():
                continue
            cells = next(csv.reader([line.rstrip("\r\n")], delimiter=_delimiter(line)), [])
            keys = [_norm_header(c) for c in cells]
            if all(_has(keys, aliases) for aliases in required):
                start = i
                break
    body = "".join(lines[start:])
    delimiter = _delimiter(lines[start] if lines else "")
    reader = csv.reader(io.StringIO(body), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        return _Table([], [], start + 1, [])
    keys = [_norm_header(h) for h in header]
    out = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        row: dict[str, str] = {}
        for i, k in enumerate(keys):
            if k:
                row[k] = values[i].strip() if i < len(values) else ""
        out.append((start + reader.line_num, row, delimiter.join(values), values))
    return _Table(keys, header, start + 1, out)


def _pick(row: Mapping[str, str], aliases: tuple[str, ...]) -> str:
    for a in aliases:
        if a in row and row[a] != "":
            return row[a]
    return ""


def _has(keys: list[str], aliases: tuple[str, ...]) -> bool:
    return any(a in keys for a in aliases)


def _col(keys: list[str], aliases: tuple[str, ...]) -> str | None:
    """The first alias that is a column of the file."""
    return next((a for a in aliases if a in keys), None)


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def customer_key(name: str) -> str:
    """A customer id made from a name, for exports without an id column.

    Slug plus eight hex characters of a sha256 of the casefolded name (NFC,
    runs of spaces collapsed): ``Müller GmbH`` and ``Muller GmbH`` stay two
    customers, ``ACME Ltd`` and ``Acme Ltd`` are one, and a name with no Latin
    letters (``北京测试有限公司``) still gets an id. Empty for an empty name.
    """
    exact = unicodedata.normalize("NFC", " ".join((name or "").split())).casefold()
    if not exact:
        return ""
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()[:8]
    slug = slugify(name)[:40].strip("-") or "customer"
    return f"{slug}-{digest}"


def clean_reference(text: str) -> str:
    """``VS 20261104`` / ``VS: 20261104`` / ``variabilní symbol 20261104`` -> ``20261104``."""
    t = (text or "").strip()
    m = re.match(r"^(?:vs|v\.s\.|variabilní symbol|variabilni symbol|variable symbol)\s*[:.]?\s*(.+)$", t, re.IGNORECASE)
    return (m.group(1) if m else t).strip()


def _amount(text: str, lang: str | None = None):
    return parse_amount(text, lang, strict=True)


def _check_date_order(date_order: str | None) -> str | None:
    if date_order is None or date_order == "auto":
        return None
    if date_order not in DATE_ORDERS:
        raise ValueError(f"date_order must be one of {', '.join(DATE_ORDERS)} (or None)")
    return date_order


def _language(text: str) -> tuple[str, bool]:
    """(language, supported): an unsupported language is read as English."""
    low = (text or "").strip().lower()
    if not low:
        return "en", True
    return (low, True) if low in LANGUAGES else ("en", False)


# -------------------------------------------------------------- invoice sources

A = {
    "number": ("invoice no", "invoice number", "invoicenumber", "number", "invoice", "num", "no", "doc number", "invoice id", "id"),
    "customer_id": ("customer id", "customer", "customer full name", "contact", "contactname", "contact name", "name"),
    "customer_name": ("contactname", "contact name", "customer name", "customer full name", "customer", "name"),
    "email": ("emailaddress", "email address", "email", "customer email", "contact email"),
    "contact": ("contactperson", "contact person", "contact name person", "attention"),
    "language": ("language", "lang", "jazyk"),
    "issue_date": ("issue date", "invoicedate", "invoice date", "date", "issued", "transaction date", "finalized at", "datum vystavení"),
    "due_date": ("due date", "duedate", "due", "datum splatnosti"),
    "amount": ("amount", "total", "celkem"),
    "currency": ("currency", "měna", "mena"),
    "balance": ("balance", "open balance", "outstanding", "amount remaining", "invoiceamountdue", "amountdue", "amount due", "zbývá"),
    "paid_amount": ("invoiceamountpaid", "amountpaid", "amount paid", "paid amount"),
    "status": ("status", "stav"),
    "reference": ("reference", "variable symbol", "variabilní symbol", "variabilni symbol", "vs", "payment reference"),
    "iban": ("iban", "bank account", "account iban"),
    "paid_date": ("paid date", "paiddate", "paid at", "paid", "datum úhrady"),
    "note": ("note", "notes", "memo", "memo description", "description", "poznámka"),
    "type": ("transaction type", "type", "document type", "typ"),
    "billing": ("billing", "collection method"),
}
BALANCE = {
    "generic": A["balance"],
    "quickbooks": A["balance"],
    "xero": ("invoiceamountdue", "amountdue", "amount due", "balance", "outstanding"),
    "stripe": ("amount remaining", "amount due", "balance"),
}

STATUS_MAPS: dict[str, dict[str, str]] = {
    "generic": {s: s for s in INVOICE_STATUSES} | {"partial": "open", "overdue": "open", "authorised": "open", "authorized": "open"},
    "xero": {"authorised": "open", "authorized": "open", "awaiting payment": "open", "overdue": "open", "sent": "open",
             "paid": "paid", "voided": "void", "void": "void", "disputed": "disputed", "open": "open"},
    "quickbooks": {"open": "open", "overdue": "open", "partially paid": "open", "partial": "open", "paid": "paid", "closed": "paid",
                   "deposited": "paid", "voided": "void", "void": "void", "deleted": "void", "disputed": "disputed"},
    "stripe": {"open": "open", "paid": "paid", "void": "void", "uncollectible": "void", "disputed": "disputed"},
}
SKIP_STATUSES: dict[str, dict[str, str]] = {
    "generic": {"draft": "draft"},
    "xero": {"draft": "draft", "submitted": "draft", "awaiting approval": "draft", "deleted": "deleted"},
    "quickbooks": {"draft": "draft"},
    "stripe": {"draft": "draft", "deleted": "deleted"},
}
CREDIT_TYPES = {"accreccredit", "credit note", "sales credit note", "credit memo", "credit", "creditnote", "dobropis",
                "opravný daňový doklad", "opravny danovy doklad", "gutschrift"}
BILL_TYPES = {"accpay", "accpaycredit", "bill", "purchase bill", "bill credit note", "purchase credit note"}
INVOICE_TYPES = {"", "accrec", "invoice", "sales invoice", "tax invoice", "faktura"}
QBO_PAYMENT_TYPES = {"payment", "receive payment", "deposit"}
_TOTAL_ROW = re.compile(r"^(?:grand\s+)?total\b", re.IGNORECASE)


def _parse_status(source: str, text: str, balance: Decimal) -> str | None:
    """Normalised status, None for an unknown value."""
    low = (text or "").strip().lower()
    if not low:
        return "paid" if balance <= 0 else "open"
    if low in VOID_WORDS:
        return "void"
    return STATUS_MAPS[source].get(low)


def _report_row(values: list[str], row: Mapping[str, str]) -> bool:
    """A QuickBooks report line that is not a transaction: a group heading, a subtotal, TOTAL, the footer."""
    cells = [v.strip() for v in values if v.strip()]
    if len(cells) <= 1 or _TOTAL_ROW.match(cells[0]):
        return True
    date_txt, amount_txt = _pick(row, A["issue_date"]), _pick(row, A["amount"])
    return not amount_txt and parse_date(date_txt) is None and parse_date(date_txt, "cs") is None


class _Refuse(ValueError):
    """A row error."""


def read_invoices(source: str, text_or_path: str | os.PathLike[str], *, default_currency: str = "EUR",
                  date_order: str | None = None) -> ImportResult:
    """Read an invoice export. ``source`` is generic, xero, quickbooks or stripe.

    ``date_order`` decides numeric slashed dates: ``dmy`` or ``mdy``; ``None``
    (or ``auto``) decides from the whole file and refuses a file whose dates
    all read both ways. Dotted and ISO dates are unambiguous whatever the order.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown invoice source {source!r}; expected one of {', '.join(SOURCES)}")
    explicit = _check_date_order(date_order)
    text, err = load_text(text_or_path)
    if err:
        return ImportResult("invoices", source, errors=(RowError(0, err),))
    required = {"number": A["number"], "issue_date": A["issue_date"], "due_date": A["due_date"], "amount": A["amount"]}
    required["customer"] = A["customer_id"] if source == "generic" else A["customer_name"]
    table = _table(text, required.values())
    keys, fp = table.keys, fingerprint(text)
    missing = [name for name, aliases in required.items() if not _has(keys, aliases)]
    if missing:
        return ImportResult("invoices", source, errors=(RowError(table.header_line, f"missing column(s): {', '.join(missing)}", ",".join(keys)),),
                            fingerprint=fp, columns=tuple(keys))
    rows = [(n, r, raw) for n, r, raw, values in table.rows if not (source == "quickbooks" and _report_row(values, r))]
    try:
        inferred = None if explicit else infer_date_order(_pick(r, A[c]) for _, r, _ in rows for c in ("issue_date", "due_date", "paid_date"))
    except DateOrderError as exc:
        return ImportResult("invoices", source, errors=(RowError(table.header_line, str(exc), ",".join(keys)),),
                            fingerprint=fp, rows=len(rows), columns=tuple(keys))
    order = explicit or inferred or "dmy"  # without a decided order no date reads differently either way

    records: list[Invoice] = []
    credits: list[CreditNote] = []
    customers: dict[str, Customer] = {}
    errors: list[RowError] = []
    skipped: list[RowError] = []
    warnings: list[RowError] = []
    seen: set[str] = set()
    for line_no, row, raw, group_error in _collapse(source, keys, rows):
        if group_error:
            errors.append(RowError(line_no, group_error, raw))
            continue
        try:
            outcome = _invoice_row(source, row, line_no, raw, order, default_currency)
        except _Refuse as exc:
            errors.append(RowError(line_no, str(exc), raw))
            continue
        if isinstance(outcome, RowError):
            skipped.append(outcome)
            continue
        if isinstance(outcome, CreditNote):
            credits.append(outcome)
            continue
        inv, warning = outcome
        if inv.number in seen:
            errors.append(RowError(line_no, f"duplicate invoice number {inv.number}; counted once", raw))
            continue
        seen.add(inv.number)
        records.append(inv)
        if warning:
            warnings.append(RowError(line_no, warning, raw, "overpaid"))
        if source != "generic":
            name = _pick(row, A["customer_name"]) or inv.customer_id
            language, supported = _language(_pick(row, A["language"]))
            existing = customers.get(inv.customer_id)
            if not supported and existing is None:
                warnings.append(RowError(line_no, f"language {_pick(row, A['language'])!r} is not supported (en, cs); {name} is written to in English", raw, "language"))
            new = Customer(inv.customer_id, name, language, _pick(row, A["email"]), _pick(row, A["contact"]))
            if existing is None:
                customers[inv.customer_id] = new
            elif not existing.email and new.email or not existing.contact_name and new.contact_name:
                customers[inv.customer_id] = Customer(inv.customer_id, existing.name, existing.language, existing.email or new.email, existing.contact_name or new.contact_name)
    return ImportResult("invoices", source, records=tuple(records), customers=tuple(customers.values()), errors=tuple(errors),
                        skipped=tuple(skipped), fingerprint=fp, rows=len(rows), columns=tuple(keys), credit_notes=tuple(credits),
                        warnings=tuple(warnings), date_order=explicit or inferred)


INVOICE_LEVEL = ("customer_name", "email", "issue_date", "due_date", "amount", "paid_amount", "currency", "status", "type", "reference")


def _collapse(source: str, keys: list[str], rows):
    """Yield (line, row, raw, error). Xero writes one row per line item: rows of one invoice become one row.

    The invoice-level fields (contact, dates, totals, amount due, currency,
    status, type, reference) must agree across the rows, else the invoice is
    refused with one error naming the field.
    """
    if source != "xero":
        for line_no, row, raw in rows:
            yield line_no, row, raw, ""
        return
    groups: dict[str, list[tuple[int, dict[str, str], str]]] = {}
    order: list[object] = []
    for item in rows:
        number = _pick(item[1], A["number"])
        if not number:
            order.append(item)
            continue
        if number not in groups:
            groups[number] = []
            order.append(number)
        groups[number].append(item)
    columns = [_col(keys, A[name]) for name in INVOICE_LEVEL] + [_col(keys, BALANCE["xero"])]
    for entry in order:
        if not isinstance(entry, str):
            line_no, row, raw = entry  # type: ignore[misc]
            yield line_no, row, raw, ""
            continue
        items = groups[entry]
        line_no, row, raw = items[0]
        conflict = ""
        for column in columns:
            if column is None:
                continue
            values = {r.get(column, "").strip() for _, r, _ in items}
            if len(values) > 1:
                shown = " vs ".join(sorted(repr(v) for v in values))
                conflict = (f"invoice {entry} has {len(items)} rows that disagree on {column} ({shown}); "
                            f"not imported - re-export it from Xero")
                break
        yield line_no, row, raw, conflict


def _invoice_row(source: str, row: Mapping[str, str], line_no: int, raw: str, order: str, default_currency: str):
    """One row: (Invoice, warning) | CreditNote | RowError (skipped). Raises _Refuse for a row error."""
    kind_txt = _pick(row, A["type"])
    kind = kind_txt.strip().lower()
    number = _pick(row, A["number"]) if source != "stripe" else (_pick(row, ("number",)) or _pick(row, ("id",)))
    status_text = _pick(row, A["status"])
    status_low = status_text.strip().lower()
    if status_low in SKIP_STATUSES[source]:
        return RowError(line_no, f"status {status_text} is not an issued invoice", raw, SKIP_STATUSES[source][status_low])
    if kind in BILL_TYPES:
        return RowError(line_no, f"type {kind_txt} is a supplier bill, not a sales invoice", raw, "bill")
    amount_txt = _pick(row, A["amount"])
    amount = _amount(amount_txt)
    if amount is None:
        raise _Refuse(f"amount is not a number: {amount_txt!r}")
    balance_txt = _pick(row, BALANCE[source])
    balance = _amount(balance_txt) if balance_txt else None
    if balance_txt and balance is None:
        raise _Refuse(f"balance is not a number: {balance_txt!r}")
    if balance is None and _pick(row, A["paid_amount"]):
        paid_txt = _pick(row, A["paid_amount"])
        paid_amount = _amount(paid_txt)
        if paid_amount is None:
            raise _Refuse(f"amount paid is not a number: {paid_txt!r}")
        balance = Amount(amount.value - paid_amount.value, amount.currency)
    currency = (_pick(row, A["currency"]) or amount.currency or default_currency).strip().upper()
    issue_txt, due_txt = _pick(row, A["issue_date"]), _pick(row, A["due_date"])
    issue = parse_date(issue_txt, order=order)
    if issue is None:
        raise _Refuse(f"issue date is not a date: {issue_txt!r}")
    customer_name = _pick(row, A["customer_name"])
    if source == "generic":
        customer_id = _pick(row, A["customer_id"])
        if not customer_id:
            raise _Refuse("missing customer id")
    elif source == "stripe":
        customer_id = _pick(row, ("customer",)) or customer_key(customer_name)
    else:
        customer_id = customer_key(customer_name)
    if not customer_id:
        raise _Refuse("missing customer")

    # credits: a credit note or memo, an unapplied payment, any negative row
    is_payment = source == "quickbooks" and kind in QBO_PAYMENT_TYPES
    is_credit = kind in CREDIT_TYPES or amount.value < 0 or is_payment
    if source == "quickbooks" and not is_credit and kind not in INVOICE_TYPES:
        open_value = balance.value if balance is not None else Decimal("0")
        if open_value == 0:
            return RowError(line_no, f"transaction type {kind_txt} is not an invoice", raw, "not_invoice")
        if open_value > 0:
            return RowError(line_no, f"transaction type {kind_txt} is not an invoice; its open balance "
                                     f"{currency} {open_value} is not chased - check it in QuickBooks", raw, "not_invoice")
        is_credit = True
    if is_credit:
        what = "payment" if is_payment else "credit memo" if "memo" in kind else "credit note"
        remaining = abs(balance.value if balance is not None else amount.value)
        state = _parse_status(source, status_text, Decimal("1")) if status_text else "open"
        if remaining == 0 or state in ("paid", "void"):
            if is_payment:
                return RowError(line_no, f"transaction type {kind_txt} is not an invoice", raw, "not_invoice")
            return RowError(line_no, f"{what} {number} is fully applied or void; nothing to apply".replace("  ", " "), raw, "settled_credit")
        detail = (f"payment of {currency} {abs(amount.value)} not applied to an invoice" if is_payment
                  else f"{what} for {currency} {remaining} - apply it to an open invoice")
        return CreditNote(number, customer_id, customer_name, issue, remaining, currency,
                          "unapplied_payment" if is_payment else "credit_note", line_no, detail)

    if not number:
        raise _Refuse("missing invoice number")
    bal = balance.value if balance is not None else amount.value
    status = _parse_status(source, status_text, bal)
    if status is None:
        raise _Refuse(f"unknown status {status_text!r}")
    if status in ("paid", "void") and balance is None:
        bal = Decimal("0.00")
    if not due_txt:
        if source == "stripe" or status in ("paid", "void"):
            billing = _pick(row, A["billing"]).lower()
            why = "charged automatically" if billing == "charge_automatically" else "no due date"
            return RowError(line_no, f"invoice {number} has no due date ({why}); not chaseable", raw, "no_due_date")
        raise _Refuse(f"due date is missing for open invoice {number}")
    due = parse_date(due_txt, order=order)
    if due is None:
        raise _Refuse(f"due date is not a date: {due_txt!r}")
    if due < issue:
        raise _Refuse(f"due date {due.isoformat()} is before issue date {issue.isoformat()}")
    paid_txt = _pick(row, A["paid_date"])
    paid = parse_date(paid_txt.split(" ")[0], order=order) if paid_txt else None
    try:
        inv = Invoice(number, customer_id, issue, due, amount.value, currency, bal, status,
                      clean_reference(_pick(row, A["reference"])), _pick(row, A["iban"]), paid, _pick(row, A["note"]))
    except (ValueError, TypeError) as exc:
        raise _Refuse(str(exc)) from exc
    warning = f"invoice {number} is overpaid: balance {currency} {bal}" if bal < 0 else ""
    return inv, warning


# ------------------------------------------------------------------------ bank

B = {
    "id": ("id", "line id", "transaction id", "txn id", "payment id", "id transakce", "id pohybu", "id operace", "reference number",
           "bank reference", "transferwise id"),
    "date": ("date", "booking date", "value date", "transaction date", "posting date", "posted date", "completed date", "datum",
             "datum zaúčtování", "datum zauctovani", "datum provedení", "buchungstag", "buchungsdatum", "wertstellung", "posted",
             "started date"),
    "amount": ("amount", "částka", "castka", "objem", "value", "betrag", "bedrag", "umsatz"),
    "debit": ("debit", "debit amount", "debits", "money out", "paid out", "withdrawal", "withdrawals", "outflow", "soll", "výdaj", "vydaj"),
    "credit": ("credit", "credit amount", "credits", "money in", "paid in", "deposit", "deposits", "inflow", "haben", "příjem", "prijem"),
    "direction": ("cdtdbtind", "cdt dbt ind", "creditdebitindicator", "credit debit indicator", "debit credit indicator", "credit debit",
                  "debit credit", "dr cr", "cr dr", "drcr", "d c", "c d", "dc", "soll haben", "s h", "af bij", "direction"),
    "status": ("status", "state", "transaction status", "booking status"),
    "currency": ("currency", "měna", "mena", "währung", "waehrung"),
    "counterparty": ("counterparty", "counter party", "counterparty name", "payer", "payer name", "name", "název protiúčtu", "nazev protiuctu",
                     "protiúčet název", "protiucet nazev", "název účtu", "sender", "auftraggeber begünstigter", "auftraggeber empfänger",
                     "begünstigter zahlungspflichtiger", "beguenstigter zahlungspflichtiger", "name zahlungsbeteiligter",
                     "naam omschrijving", "naam", "tegenpartij", "payee"),
    "reference": ("reference", "variable symbol", "variabilní symbol", "variabilni symbol", "vs", "payment reference", "remittance",
                  "remittance information", "end to end", "verwendungszweck", "betalingskenmerk", "kenmerk"),
    "memo": ("memo", "note", "description", "message", "zpráva pro příjemce", "zprava pro prijemce", "poznámka", "poznamka", "popis",
             "transaction description", "buchungstext", "mededelingen", "omschrijving", "bank description", "details"),
    "source": ("source", "channel"),
}
CREDIT_WORDS = {"crdt", "cr", "c", "credit", "h", "haben", "bij", "in", "incoming", "inflow", "+", "příjem", "prijem", "deposit"}
DEBIT_WORDS = {"dbit", "dr", "d", "debit", "s", "soll", "af", "out", "outgoing", "outflow", "-", "výdaj", "vydaj", "withdrawal"}
BOOKED_WORDS = {"booked", "completed", "complete", "posted", "settled", "cleared", "executed", "processed", "success", "succeeded",
                "successful", "done", "sent", "received", "paid", "gebucht", "zaúčtováno", "zauctovano", "provedeno", "verwerkt", "geboekt"}
NOT_BOOKED_WORDS = {"pending", "failed", "declined", "cancelled", "canceled", "reverted", "reversed", "returned", "rejected",
                    "in progress", "processing", "scheduled", "authorised", "authorized", "on hold", "expired", "refunded",
                    "vorgemerkt", "abgelehnt", "storniert", "upcoming", "blocked", "held", "waiting", "void", "voided"}


def read_bank_csv(text_or_path: str | os.PathLike[str], *, default_currency: str = "EUR", lang: str | None = None,
                  date_order: str | None = None) -> ImportResult:
    """Read a bank statement export into Payments.

    Headers may be English, Czech, German or Dutch; ``;`` or tab delimiters, a
    BOM, decimal commas, trailing minus signs and lines above the table are
    handled. The amount is a signed column, or separate Debit/Credit (Money
    in/Money out, Soll/Haben) columns; a direction column (``CdtDbtInd``
    CRDT/DBIT, DR/CR, S/H, Af/Bij) signs an unsigned amount, so only incoming
    credits are positive. With a status column only booked, completed, posted
    or settled lines count; pending and failed lines are skipped. A direction
    or status value the reader does not understand refuses the whole file
    rather than reading every line as money received.

    A statement without an id column gets stable ids from the row content, so
    the same export imported twice yields the same ids. ``lang`` settles an
    ambiguous amount (``1.250``); ``date_order`` (``dmy`` or ``mdy``) settles
    slashed dates, else the whole file decides and an undecidable file is refused.
    """
    explicit = _check_date_order(date_order)
    text, err = load_text(text_or_path)
    if err:
        return ImportResult("bank", "bank", errors=(RowError(0, err),))
    money_cols = B["amount"] + B["debit"] + B["credit"]
    table = _table(text, (B["date"], money_cols))
    keys, fp = table.keys, fingerprint(text)
    missing = [name for name, aliases in (("date", B["date"]), ("amount", money_cols)) if not _has(keys, aliases)]
    if missing:
        return ImportResult("bank", "bank", errors=(RowError(table.header_line, f"missing column(s): {', '.join(missing)}", ",".join(keys)),),
                            fingerprint=fp, columns=tuple(keys))
    header_currency = {_norm_header(h): _header_currency(h) for h in table.raw_header if _header_currency(h)}
    file_currency = header_currency.get(_col(keys, B["amount"]) or "")
    if file_currency is None and len(set(header_currency.values())) == 1:
        file_currency = next(iter(header_currency.values()))  # "Soll (EUR)" / "Haben (EUR)"
    rows = table.rows

    def refuse(message: str) -> ImportResult:
        return ImportResult("bank", "bank", errors=(RowError(table.header_line, message, ",".join(keys)),), fingerprint=fp,
                            rows=len(rows), columns=tuple(keys))

    direction_col, status_col = _col(keys, B["direction"]), _col(keys, B["status"])
    for column, known, what in ((direction_col, CREDIT_WORDS | DEBIT_WORDS, "direction"), (status_col, BOOKED_WORDS | NOT_BOOKED_WORDS, "status")):
        if column is None:
            continue
        unknown = sorted({r.get(column, "").strip() for _, r, _, _ in rows if r.get(column, "").strip().lower() not in known})
        if unknown:
            shown = ", ".join(repr(v) for v in unknown[:5])
            rule = ("only credits (CRDT, CR, H, Bij) are money received" if what == "direction"
                    else "only booked, completed, posted or settled lines count as money received")
            return refuse(f"the {column!r} {what} column has values Cash Desk does not understand ({shown}); {rule}, "
                          "so the file is refused rather than guessed")
    try:
        inferred = None if explicit else infer_date_order(_pick(r, B["date"]) for _, r, _, _ in rows)
    except DateOrderError as exc:
        return refuse(str(exc))
    order = explicit or inferred or "dmy"

    records: list[Payment] = []
    errors: list[RowError] = []
    skipped: list[RowError] = []
    seen: dict[str, int] = {}
    for line_no, row, raw, _values in rows:
        try:
            if status_col and row.get(status_col, "").strip().lower() in NOT_BOOKED_WORDS:
                skipped.append(RowError(line_no, f"status {row[status_col]} is not a booked transaction", raw, "not_booked"))
                continue
            date_txt = _pick(row, B["date"])
            compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", date_txt)  # ING and camt exports write 20261103
            date = parse_date("-".join(compact.groups()) if compact else date_txt, order=order)
            if date is None:
                raise ValueError(f"date is not a date: {date_txt!r}")
            amount = _bank_amount(row, lang, direction_col)
            currency = (_pick(row, B["currency"]) or amount.currency or file_currency or default_currency).upper()
            counterparty, reference, memo = _pick(row, B["counterparty"]), clean_reference(_pick(row, B["reference"])), _pick(row, B["memo"])
            pid = _pick(row, B["id"])
            if not pid:
                digest = hashlib.sha256(f"{date}|{amount.value}|{currency}|{counterparty}|{reference}|{memo}".encode("utf-8")).hexdigest()[:12]
                pid = f"bank-{digest}"
                if pid in seen:
                    seen[pid] += 1
                    pid = f"{pid}-{seen[pid]}"
            if pid in seen:
                raise ValueError(f"duplicate payment id {pid}; counted once")
            seen.setdefault(pid, 1)
            records.append(Payment(pid, date, amount.value, currency, reference, counterparty, memo, _pick(row, B["source"]) or "bank"))
        except (ValueError, TypeError) as exc:
            errors.append(RowError(line_no, str(exc), raw))
    return ImportResult("bank", "bank", records=tuple(records), errors=tuple(errors), skipped=tuple(skipped), fingerprint=fp,
                        rows=len(rows), columns=tuple(keys), date_order=explicit or inferred)


def _bank_amount(row: Mapping[str, str], lang: str | None, direction_col: str | None):
    """The signed amount of a bank line: incoming positive, outgoing negative."""
    amount_txt = _pick(row, B["amount"])
    if amount_txt:
        amount = _amount(amount_txt, lang)
        if amount is None:
            raise ValueError(f"amount is not a number: {amount_txt!r}")
        if direction_col:
            word = row.get(direction_col, "").strip().lower()
            if word in CREDIT_WORDS:
                if amount.value < 0:
                    raise ValueError(f"amount {amount_txt} is negative but the line is marked {row[direction_col]} (credit)")
                return amount
            if amount.value > 0:
                return Amount(-amount.value, amount.currency)
        return amount
    debit_txt, credit_txt = _pick(row, B["debit"]), _pick(row, B["credit"])
    if not debit_txt and not credit_txt:
        raise ValueError("no amount: the amount, debit and credit columns are all empty")
    debit = _amount(debit_txt, lang) if debit_txt else None
    credit = _amount(credit_txt, lang) if credit_txt else None
    if debit_txt and debit is None:
        raise ValueError(f"debit is not a number: {debit_txt!r}")
    if credit_txt and credit is None:
        raise ValueError(f"credit is not a number: {credit_txt!r}")
    if debit and debit.value and credit and credit.value:
        raise ValueError(f"both debit ({debit_txt}) and credit ({credit_txt}) are filled in")
    if credit and credit.value:
        return Amount(abs(credit.value), credit.currency)
    if debit and debit.value:
        return Amount(-abs(debit.value), debit.currency)
    return credit or debit


# ------------------------------------------------------------------- customers

C = {
    "id": ("customer id", "id", "customer"),
    "name": ("name", "customer name", "company", "contactname", "contact name"),
    "language": ("language", "lang", "jazyk"),
    "email": ("email", "emailaddress", "email address"),
    "contact": ("contact name", "contact", "contactperson", "contact person", "attention", "full name"),
    "on_hold": ("on hold", "hold", "do not contact"),
    "hold_reason": ("hold reason", "reason", "note"),
}
TRUE_WORDS = {"yes", "y", "true", "1", "ano", "x"}


def read_customers(text_or_path: str | os.PathLike[str]) -> ImportResult:
    """Read the generic customers CSV (customer_id, name, language, email, contact_name, on_hold, hold_reason).

    A language other than ``en`` or ``cs`` is read as ``en`` with a warning.
    """
    text, err = load_text(text_or_path)
    if err:
        return ImportResult("customers", "generic", errors=(RowError(0, err),))
    table = _table(text, (C["id"],))
    keys, fp = table.keys, fingerprint(text)
    id_aliases, name_aliases = C["id"], C["name"]
    if not _has(keys, name_aliases) and _has(keys, ("customer",)):
        # a contact list from QuickBooks / Xero: "Customer" is the name; the id is made from it
        # exactly as the invoice importers make it, so both files meet on the same customer
        id_aliases, name_aliases = ("customer id",), ("customer",)
    derive_ids = not _has(keys, id_aliases)
    missing = [] if _has(keys, name_aliases) else ["name"]
    if missing:
        return ImportResult("customers", "generic", errors=(RowError(table.header_line, f"missing column(s): {', '.join(missing)}", ",".join(keys)),),
                            fingerprint=fp, columns=tuple(keys))
    records: list[Customer] = []
    errors: list[RowError] = []
    warnings: list[RowError] = []
    seen: set[str] = set()
    for line_no, row, raw, _values in table.rows:
        try:
            name = _pick(row, name_aliases)
            cid = customer_key(name) if derive_ids else _pick(row, id_aliases)
            if not cid:
                raise ValueError("missing customer id")
            if not name:
                raise ValueError("missing customer name")
            if cid in seen:
                raise ValueError(f"duplicate customer id {cid}; counted once")
            language, supported = _language(_pick(row, C["language"]))
            on_hold = _pick(row, C["on_hold"]).strip().lower() in TRUE_WORDS
            records.append(Customer(cid, name, language, _pick(row, C["email"]), _pick(row, C["contact"]), on_hold, _pick(row, C["hold_reason"])))
            seen.add(cid)
            if not supported:
                warnings.append(RowError(line_no, f"language {_pick(row, C['language'])!r} is not supported (en, cs); {name} is written to in English", raw, "language"))
        except (ValueError, TypeError) as exc:
            errors.append(RowError(line_no, str(exc), raw))
    return ImportResult("customers", "generic", records=tuple(records), errors=tuple(errors), fingerprint=fp, rows=len(table.rows),
                        columns=tuple(keys), warnings=tuple(warnings))


__all__ = [
    "SOURCES",
    "DATE_ORDERS",
    "RowError",
    "CreditNote",
    "ImportResult",
    "fingerprint",
    "load_text",
    "slugify",
    "customer_key",
    "clean_reference",
    "read_invoices",
    "read_bank_csv",
    "read_customers",
]
