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
from .money import Amount, parse_amount, normalise_currency

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

    ``amount`` is the remaining credit as a nonnegative number. ``status``
    distinguishes an open question from a paid/void update. ``original_amount``
    preserves the exported gross value when the remaining credit becomes zero.
    Never chased; a person decides which invoice an open credit settles.
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
    status: str = "open"
    original_amount: Decimal | None = None


@dataclass(frozen=True)
class InvoiceClosure:
    """Terminal evidence without a due date; never a new chaseable invoice."""

    number: str
    customer_id: str
    issue_date: dt.date
    amount: Decimal
    currency: str
    status: str
    paid_date: dt.date | None = None
    exported_balance: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class InvoiceSurplus:
    number: str
    customer_id: str
    issue_date: dt.date
    amount: Decimal
    currency: str
    balance: Decimal
    row: int
    raw: str


@dataclass(frozen=True)
class InvoiceImportIssue:
    number: str
    customer_id: str
    row: int
    raw: str
    detail: str
    customer_origin: str = ""


@dataclass(frozen=True)
class CustomerIdentity:
    number: str
    customer_id: str
    origin: str
    name: str
    email: str
    row: int
    raw: str


@dataclass(frozen=True)
class BankObservation:
    payment: Payment | None
    raw_id: str
    account: str
    status: str
    raw: str
    row: int
    evidence: dict[str, object] | None = None


@dataclass(frozen=True)
class BankEvidenceIssue:
    row: int
    raw: str
    detail: str
    evidence: str


# Compatibility for callers of the earlier unreleased alias sidecar.
BankAliasIssue = BankEvidenceIssue


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
    credit_updates: tuple[CreditNote, ...] = ()
    invoice_closures: tuple[InvoiceClosure, ...] = ()
    bank_observations: tuple[BankObservation, ...] = ()
    invoice_issues: tuple[InvoiceImportIssue, ...] = ()
    invoice_surpluses: tuple[InvoiceSurplus, ...] = ()
    customer_identities: tuple[CustomerIdentity, ...] = ()
    bank_alias_issues: tuple[BankEvidenceIssue, ...] = ()
    bank_currency_issues: tuple[BankEvidenceIssue, ...] = ()
    customer_contact_fields: tuple[str, ...] | None = None
    invoice_planned: tuple[tuple[str, dt.date], ...] = ()  # (number, expected payment date) recorded in the source
    invoice_not_sent: tuple[str, ...] = ()  # numbers the source says were never sent to the customer

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
    "balance": ("balance", "open balance", "outstanding", "amount remaining", "invoiceamountdue", "amountdue", "amount due", "zbývá",
                "balance due", "amount outstanding", "outstanding amount", "outstanding balance", "open amount", "due amount",
                "unpaid amount", "amount unpaid", "remaining", "remaining amount", "remaining balance"),
    "paid_amount": ("invoiceamountpaid", "amountpaid", "amount paid", "paid amount"),
    "credited": ("amountcredited", "amount credited", "credited amount", "creditedamount", "invoiceamountcredited"),
    "status": ("status", "stav"),
    "reference": ("reference", "variable symbol", "variabilní symbol", "variabilni symbol", "vs", "payment reference"),
    "iban": ("iban", "bank account", "account iban"),
    "paid_date": ("paid date", "paiddate", "paid at", "datum úhrady"),
    "planned_date": ("planneddate", "planned date", "expected payment date", "expected date", "planned payment date"),
    "sent": ("sent", "sent status", "invoice sent"),
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
                   "deposited": "paid", "voided": "void", "void": "void", "deleted": "void", "disputed": "disputed",
                   # credit memo / payment application states
                   "unapplied": "open", "partially applied": "open", "applied": "paid"},
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
    found = STATUS_MAPS[source].get(low)
    if found is None and source in ("quickbooks", "generic") and re.match(r"^(overdue( \d+ days?)?|due (in \d+ days?|today|tomorrow))$", low):
        return "open"  # QuickBooks sales lists write "Overdue 14 days" / "Due in 5 days"
    return found


def _qbo_grouped(text: str) -> str:
    """QuickBooks Online "Open Invoices" / "Invoice List" grouped by customer: the customer is a
    heading row above its invoices and the first column has no header. Name that column
    Customer and carry each heading down to its rows; subtotal and TOTAL rows are left for
    _report_row to skip. Physical line numbers are kept (heading rows stay, blanked)."""
    lines = text.lstrip("\ufeff").splitlines()
    out, header_at = [], None
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        cells = next(csv.reader([line], delimiter=_delimiter(line)), [])
        keys = [_norm_header(c) for c in cells]
        if cells and not cells[0].strip() and _has(keys, A["number"]) and _has(keys, A["amount"]) and not _has(keys, A["customer_name"]):
            header_at = i
            break
    if header_at is None:
        return text
    delim = _delimiter(lines[header_at])
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delim, lineterminator="\n")
    current = ""
    for i, line in enumerate(lines):
        if i < header_at or not line.strip():
            buf.write(line + "\n")
            continue
        cells = next(csv.reader([line], delimiter=delim), [])
        if i == header_at:
            writer.writerow(["Customer", *cells[1:]])
            continue
        filled = [c for c in cells if c.strip()]
        if len(filled) == 1 and cells and cells[0].strip() and not _TOTAL_ROW.match(cells[0].strip()):
            current = cells[0].strip()
            writer.writerow([current] + [""] * (len(cells) - 1))  # stays a heading: one cell, skipped as a report row
            continue
        if cells and not cells[0].strip() and current:
            cells = [current, *cells[1:]]
        writer.writerow(cells)
    return buf.getvalue()


def _report_row(values: list[str], row: Mapping[str, str]) -> bool:
    """A QuickBooks report line that is not a transaction: a group heading, a subtotal, TOTAL, the footer."""
    cells = [v.strip() for v in values if v.strip()]
    if len(cells) <= 1 or _TOTAL_ROW.match(cells[0]):
        return True
    date_txt, amount_txt = _pick(row, A["issue_date"]), _pick(row, A["amount"])
    return not amount_txt and parse_date(date_txt) is None and parse_date(date_txt, "cs") is None


class _Refuse(ValueError):
    """A row error."""


_NOT_OPEN_BALANCE = {"starting balance", "ending balance", "customer balance", "account balance", "balance date"}


def _balance_like(key: str) -> bool:
    """A header that names an amount still owed, which Cash Desk must read or refuse."""
    if key in _NOT_OPEN_BALANCE:
        return False
    return any(word in key.split() or key.endswith(word) for word in ("balance", "outstanding", "remaining", "unpaid"))


def _paid_date_aliases(source):
    # Stripe Paid is a boolean. Other supported exports historically use Paid
    # as a date; retain that compatibility without feeding a flag to inference.
    return A["paid_date"] + (() if source == "stripe" else ("paid",))


def _invoice_paid_date(source, row, order):
    dates = []
    for key in _paid_date_aliases(source):
        text = row.get(key, "").strip()
        if not text:
            continue
        paid = parse_date(text.replace("T", " ").split(" ")[0], order=order)
        if paid is None:
            raise _Refuse("terminal invoice update conflicts: paid date is invalid; check the source export")
        dates.append(paid)
    if len(set(dates)) > 1:
        raise _Refuse("terminal invoice update conflicts: paid date aliases disagree; check the source export")
    return dates[0] if dates else None


def _lifecycle_refuse(source, row, detail):
    credit = any(row.get(k, "").strip().lower() in CREDIT_TYPES | (QBO_PAYMENT_TYPES if source=="quickbooks" else set()) for k in A["type"])
    prefix = "credit update cannot be trusted: " if credit else ""
    raise _Refuse("invoice lifecycle evidence conflicts: " + prefix + detail + "; verify all lifecycle columns in the original export")


def _status_family(source, value):
    low=value.strip().lower()
    if low in SKIP_STATUSES[source]:return "skip:"+SKIP_STATUSES[source][low]
    return _parse_status(source,low,Decimal("1"))


def _type_family(source, value):
    low=value.strip().lower()
    if not low:return ""
    if low in INVOICE_TYPES:return "sale"
    if low in CREDIT_TYPES:return "credit"
    if low in QBO_PAYMENT_TYPES and source=="quickbooks":return "unapplied_payment"
    if low in BILL_TYPES:return "supplier_credit" if "credit" in low else "supplier_bill"
    return "other:"+low if source=="quickbooks" else None


def _invoice_lifecycle(source, row):
    """Reconcile every populated alias before skips, money or line collapse.

    Empty aliases supply no evidence. Equivalent issued states/types agree;
    unissued, supplier, credit and receipt states are never silently combined.
    Raw source rows remain separate and unchanged in import observations.
    """
    statuses=[row[k].strip() for k in A["status"] if row.get(k, "").strip()]
    families={_status_family(source,v) for v in statuses}
    if None in families:_lifecycle_refuse(source,row,"unknown status " + repr(next(v for v in statuses if _status_family(source,v) is None)))
    if len(families)>1:_lifecycle_refuse(source,row,"status aliases disagree")
    types=[row[k].strip() for k in A["type"] if row.get(k, "").strip()]
    kinds={_type_family(source,v) for v in types}
    if None in kinds:_lifecycle_refuse(source,row,"unsupported document type " + repr(next(v for v in types if _type_family(source,v) is None)))
    if len(kinds)>1:_lifecycle_refuse(source,row,"document type aliases disagree")
    out=dict(row)
    if statuses:
        family=next(iter(families));status=statuses[0] if family.startswith("skip:") else family
        for key in A["status"]:
            if out.get(key, "").strip():out[key]=status
        out["status"]=status
    if types:out["transaction type"]=types[0]
    if source=="stripe":
        billing={row[k].strip().lower() for k in A["billing"] if row.get(k, "").strip()}
        if len(billing)>1 or any(v not in {"send_invoice","charge_automatically"} for v in billing):
            _lifecycle_refuse(source,row,"collection method aliases disagree or are unknown")
        if billing:out["billing"]=next(iter(billing))
    return out


def _stripe_paid_status(source, row):
    text = row.get("paid", "").strip().lower() if source == "stripe" else ""
    if not text:
        return row
    flags = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
    if text not in flags:
        raise _Refuse("terminal invoice update conflicts: Stripe Paid must be a boolean; check the source export")
    statuses = [row[k].strip().lower() for k in A["status"] if row.get(k, "").strip()]
    if len(set(statuses)) > 1:
        raise _Refuse("terminal invoice update conflicts: Stripe status aliases disagree; check the source export")
    status = statuses[0] if statuses else ""
    paid = flags[text]
    if status and ((paid and status != "paid") or (not paid and status == "paid")):
        raise _Refuse("terminal invoice update conflicts: Stripe Paid and status disagree; check the source export")
    if not paid and any(row.get(k, "").strip() for k in A["paid_date"]):
        raise _Refuse("terminal invoice update conflicts: Stripe unpaid flag has a paid date; check the source export")
    if paid:
        row = dict(row, status="paid")
    elif not status:
        row = dict(row, status="open")
    return row


CUSTOMER_IDS = ("customer id", "customerid", "contact id", "contactid")


def _invoice_customer(source, row):
    aliases = CUSTOMER_IDS + (("customer",) if source == "stripe" else ())
    values = {row[k].strip() for k in aliases if row.get(k, "").strip()}
    if len(values) > 1:
        raise _Refuse("customer identity conflicts: explicit customer ID aliases disagree; verify and re-export the original IDs")
    if values:
        return next(iter(values)), "explicit"
    if source == "generic":
        fallback={row[k].strip() for k in A["customer_id"] if row.get(k, "").strip()}
        if len(fallback)>1:
            raise _Refuse("customer identity conflicts: fallback customer ID aliases disagree; verify and re-export original IDs")
        return next(iter(fallback), ""), "explicit"
    names={" ".join(row[k].split()).casefold() for k in A["customer_name"] if row.get(k, "").strip()}
    if len(names)>1:
        raise _Refuse("customer identity conflicts: name-derived customer aliases disagree; re-export an unambiguous original identity")
    return customer_key(_pick(row, A["customer_name"])), "derived"


def _invoice_issue(source, row, line, raw, detail):
    numbers = {row[k].strip() for k in A["number"] if row.get(k, "").strip()}
    number = ((_pick(row, ("number",)) or _pick(row, ("id",))) if source == "stripe" else
              next(iter(numbers)) if len(numbers) == 1 else "")
    try:
        customer, origin = _invoice_customer(source, row)
    except _Refuse:
        customer, origin = "", "unknown"
    if row.get("__lifecycle_scope_unknown"):
        customer,origin="","unknown"
    return InvoiceImportIssue(number, customer, line, raw, detail, origin)


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
    if source == "quickbooks":
        text = _qbo_grouped(text)
    required = {"number": A["number"], "issue_date": A["issue_date"], "amount": A["amount"]}
    required["customer"] = A["customer_id"] if source == "generic" else A["customer_name"]
    table = _table(text, required.values())
    keys, fp = table.keys, fingerprint(text)
    missing = [name for name, aliases in required.items() if not _has(keys, aliases)]
    if missing:
        return ImportResult("invoices", source, errors=(RowError(table.header_line, f"missing column(s): {', '.join(missing)}", ",".join(keys)),),
                            fingerprint=fp, columns=tuple(keys))
    financial = set(A["amount"] + A["balance"] + A["paid_amount"] + A["credited"] + A["currency"])
    repeated = sorted(k for k, count in Counter(keys).items() if count > 1 and k in financial)
    if repeated:
        return ImportResult("invoices", source, errors=(RowError(table.header_line,
                            "invoice monetary evidence conflicts: repeated financial headers " + ", ".join(repeated) +
                            "; re-export with unique column names", ",".join(keys)),), fingerprint=fp, columns=tuple(keys))
    customer_headers = set(CUSTOMER_IDS + (A["customer_id"] if source=="generic" else A["customer_name"]) + (("customer",) if source=="stripe" else ()))
    lifecycle = customer_headers | set(A["number"] + A["status"] + A["type"] + _paid_date_aliases(source) + (("paid", *A["billing"]) if source == "stripe" else ()))
    duplicate = sorted(k for k, count in Counter(keys).items() if count > 1 and k in lifecycle)
    if duplicate:
        message = ("customer identity conflicts: repeated customer ID headers; re-export unique original IDs" if any(k in customer_headers for k in duplicate) else "invoice lifecycle evidence conflicts: repeated lifecycle headers; re-export unique columns" if any(k in A["status"]+A["type"]+(("paid", *A["billing"]) if source=="stripe" else ()) for k in duplicate) else "terminal invoice update conflicts: repeated identity/status/date headers; re-export with unique column names")
        return ImportResult("invoices", source, errors=(RowError(table.header_line, message, text),), fingerprint=fp, columns=tuple(keys))
    unread = [k for k in keys if _balance_like(k) and k not in A["balance"] + BALANCE[source] + A["paid_amount"]]
    if unread:
        return ImportResult("invoices", source, errors=(RowError(table.header_line,
                            f"balance column {unread[0]!r} is not read: Cash Desk would chase the full amount without it; "
                            "rename it to balance (or open balance) and re-import", ",".join(keys)),), fingerprint=fp, columns=tuple(keys))
    header_currencies = {_norm_header(h): _header_currency(h) for h in table.raw_header if _header_currency(h)}
    rows = [(n, r, raw) for n, r, raw, values in table.rows if not (source == "quickbooks" and _report_row(values, r))]
    try:
        inferred = None if explicit else infer_date_order(_pick(r, aliases) for _, r, _ in rows for aliases in (A["issue_date"], A["due_date"], _paid_date_aliases(source)))
    except DateOrderError as exc:
        return ImportResult("invoices", source, errors=(RowError(table.header_line, str(exc), ",".join(keys)),),
                            fingerprint=fp, rows=len(rows), columns=tuple(keys))
    order = explicit or inferred or "dmy"  # without a decided order no date reads differently either way

    records: list[Invoice] = []
    invoice_planned: list[tuple[str, dt.date]] = []
    invoice_not_sent: list[str] = []
    credits: list[CreditNote] = []
    credit_updates: list[CreditNote] = []
    invoice_closures: list[InvoiceClosure] = []
    invoice_issues: list[InvoiceImportIssue] = []
    invoice_surpluses: list[InvoiceSurplus] = []
    customer_identities: list[CustomerIdentity] = []
    customers: dict[str, Customer] = {}
    errors: list[RowError] = []
    skipped: list[RowError] = []
    warnings: list[RowError] = []
    seen: set[str] = set()
    for line_no, row, raw, group_error in _collapse(source, keys, rows):
        if group_error:
            errors.append(RowError(line_no, group_error, raw))
            invoice_issues.append(_invoice_issue(source, row, line_no, raw, group_error))
            continue
        try:
            outcome = _invoice_row(source, row, line_no, raw, order, default_currency, header_currencies)
        except _Refuse as exc:
            errors.append(RowError(line_no, str(exc), raw))
            invoice_issues.append(_invoice_issue(source, row, line_no, raw, str(exc)))
            continue
        if isinstance(outcome, RowError):
            skipped.append(outcome)
            if outcome.code in {"draft","deleted","bill","not_invoice","no_due_date"}:
                invoice_issues.append(_invoice_issue(source,row,line_no,raw,
                    "invoice lifecycle evidence conflicts: skipped source observation may contradict a stored issued document"))
            continue
        if isinstance(outcome, InvoiceClosure):
            if outcome.number in seen:
                errors.append(RowError(line_no, f"duplicate invoice number {outcome.number}; counted once", raw))
                invoice_issues.append(_invoice_issue(source, row, line_no, raw, "duplicate invoice number"))
                continue
            seen.add(outcome.number)
            invoice_closures.append(outcome)
            if outcome.amount >= 0 and outcome.exported_balance < 0:
                invoice_surpluses.append(InvoiceSurplus(outcome.number, outcome.customer_id, outcome.issue_date, outcome.amount, outcome.currency, outcome.exported_balance, line_no, raw))
                warnings.append(RowError(line_no, f"invoice {outcome.number} is overpaid: balance {outcome.currency} {outcome.exported_balance}", raw, "overpaid"))
            billing = _pick(row, A["billing"]).lower()
            why = "charged automatically" if billing == "charge_automatically" else "no due date"
            skipped.append(RowError(line_no, f"invoice {outcome.number} has no due date ({why}); "
                                    "not chaseable as a new invoice; closure update retained", raw, "no_due_date"))
            customer_id = outcome.customer_id
        elif isinstance(outcome, CreditNote):
            if outcome.status in ("paid", "void"):
                # Terminal observations are not new open credit questions, but
                # their stable identity must reach the store to close old ones.
                code = "not_invoice" if outcome.kind == "unapplied_payment" else "settled_credit"
                message = (f"transaction type {_pick(row, A['type'])} is not an invoice" if code == "not_invoice"
                           else f"credit {outcome.number} is fully applied or void; closure update retained" if outcome.number
                           else "credit is fully applied or void; no stable document number identifies an existing credit")
                skipped.append(RowError(line_no, message, raw, code))
                if not outcome.number:
                    warnings.append(RowError(line_no, "credit update needs a stable document number; no existing credit was cleared; "
                                             "re-export with the original credit or payment number", raw, "credit_identity"))
                    continue
                credit_updates.append(outcome)
            else:
                credits.append(outcome)
            customer_id = outcome.customer_id
        else:
            inv, warning = outcome
            if inv.amount >= 0 and inv.balance < 0:
                invoice_surpluses.append(InvoiceSurplus(inv.number, inv.customer_id, inv.issue_date, inv.amount, inv.currency, inv.balance, line_no, raw))
            if inv.number in seen:
                errors.append(RowError(line_no, f"duplicate invoice number {inv.number}; counted once", raw))
                invoice_issues.append(_invoice_issue(source, row, line_no, raw, "duplicate invoice number"))
                continue
            seen.add(inv.number)
            records.append(inv)
            planned_txt = _pick(row, A["planned_date"])
            if planned_txt:
                planned = parse_date(planned_txt.replace("T", " ").split(" ")[0], order=order)
                if planned is None:
                    warnings.append(RowError(line_no, f"planned payment date {planned_txt!r} is not a date; ignored", raw, "planned_date"))
                else:
                    invoice_planned.append((inv.number, planned))
            if _pick(row, A["sent"]).strip().lower() in NOT_SENT:
                invoice_not_sent.append(inv.number)
            if warning:
                warnings.append(RowError(line_no, warning, raw, "overpaid"))
            customer_id = inv.customer_id
        _, origin = _invoice_customer(source, row)
        number = outcome.number if isinstance(outcome, (CreditNote, InvoiceClosure)) else inv.number
        customer_identities.append(CustomerIdentity(number, customer_id, origin, _pick(row, A["customer_name"]),
                                                     _pick(row, A["email"]), line_no, raw))
        if source != "generic":
            name = _pick(row, A["customer_name"]) or customer_id
            language, supported = _language(_pick(row, A["language"]))
            existing = customers.get(customer_id)
            if not supported and existing is None:
                warnings.append(RowError(line_no, f"language {_pick(row, A['language'])!r} is not supported (en, cs); {name} is written to in English", raw, "language"))
            new = Customer(customer_id, name, language, _pick(row, A["email"]), _pick(row, A["contact"]))
            if existing is None:
                customers[customer_id] = new
            elif not existing.email and new.email or not existing.contact_name and new.contact_name:
                customers[customer_id] = Customer(customer_id, existing.name, existing.language, existing.email or new.email, existing.contact_name or new.contact_name)
    return ImportResult("invoices", source, records=tuple(records), customers=tuple(customers.values()), errors=tuple(errors),
                        skipped=tuple(skipped), fingerprint=fp, rows=len(rows), columns=tuple(keys), credit_notes=tuple(credits),
                        warnings=tuple(warnings), date_order=explicit or inferred, credit_updates=tuple(credit_updates),
                        invoice_closures=tuple(invoice_closures), invoice_issues=tuple(invoice_issues), invoice_surpluses=tuple(invoice_surpluses), customer_identities=tuple(customer_identities),
                        invoice_planned=tuple(invoice_planned), invoice_not_sent=tuple(invoice_not_sent))


INVOICE_LEVEL = ("customer_name", "email", "issue_date", "due_date", "amount", "paid_amount", "currency", "status", "type", "reference", "planned_date", "sent")
NOT_SENT = {"not sent", "unsent", "no", "false", "0", "draft not sent"}


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
    columns += [k for k in keys if k in set(CUSTOMER_IDS)]
    columns += [k for k in keys if k in set(A["amount"] + A["balance"] + A["paid_amount"] + A["credited"] + A["currency"])]
    for entry in order:
        if not isinstance(entry, str):
            line_no, row, raw = entry  # type: ignore[misc]
            yield line_no, row, raw, ""
            continue
        items = groups[entry]
        line_no, row, raw = items[0]
        raw="\n".join(item[2] for item in items)
        try:
            normalized=[(n,_invoice_lifecycle(source,item),original) for n,item,original in items]
        except _Refuse as exc:
            if len(items)>1:row=dict(row,__lifecycle_scope_unknown="1")
            yield line_no,row,raw,str(exc)
            continue
        row=dict(normalized[0][1]);items=normalized
        conflict = ""
        lifecycle_columns=set(A["status"]+A["type"])
        for column in [*columns,*lifecycle_columns]:
            if column is None:
                continue
            raw_values=[r.get(column, "").strip() for _,r,_ in items]
            if column in lifecycle_columns:
                populated=[v for v in raw_values if v]
                classify=_status_family if column in A["status"] else _type_family
                values={classify(source,v) for v in populated}
                if populated:row[column]=populated[0]
            else:values=set(raw_values)
            if len(values) > 1:
                shown = " vs ".join(sorted(repr(v) for v in values))
                prefix="invoice lifecycle evidence conflicts: " if column in lifecycle_columns else ""
                if prefix and len(items)>1:row["__lifecycle_scope_unknown"]="1"
                conflict = (prefix+f"invoice {entry} has {len(items)} rows that disagree on {column} ({shown}); "
                            f"not imported - re-export it from Xero")
                break
        yield line_no, row, raw, conflict


def _invoice_evidence(source, row, amount, balance, default_currency, headers):
    """Reconcile all recognized financial evidence in major currency units."""
    def refuse(detail):
        raise _Refuse("invoice monetary evidence conflicts: " + detail + "; verify gross, paid, remaining and currency in the source export")
    observed = []
    def group(aliases, label):
        values = []
        for key in dict.fromkeys(aliases):
            text = row.get(key, "").strip()
            if not text:
                continue
            parsed = _amount(text)
            if parsed is None:
                refuse(f"{label} is not a number in {key}: {text!r}")
            values.append(parsed.value)
            observed.append((key, parsed))
        if len(set(values)) > 1:
            refuse(f"{label} aliases disagree")
        return values[0] if values else None
    gross = group(A["amount"], "gross amount")
    # Stripe Amount Due is the billing obligation when Amount Remaining exists;
    # otherwise retain its documented remaining-balance fallback semantics.
    stripe_due = source == "stripe" and bool(row.get("amount remaining", "").strip())
    remaining_aliases = tuple(k for k in A["balance"] if not (stripe_due and k == "amount due"))
    remaining = group(remaining_aliases, "remaining balance")
    paid = group(A["paid_amount"], "amount paid")
    credited = group(A["credited"], "amount credited")
    billing = group(("amount due",), "Stripe billing amount") if stripe_due else None
    currencies = set()
    for key in A["currency"]:
        if row.get(key, "").strip():
            cur = normalise_currency(row[key])
            if cur is None: refuse(f"currency is invalid in {key}")
            currencies.add(cur)
            if headers.get(key): currencies.add(headers[key])
    for key, parsed in observed:
        if parsed.currency: currencies.add(parsed.currency)
        if headers.get(key): currencies.add(headers[key])
    if len(currencies) > 1:
        refuse("currency column, financial header or embedded amount currencies disagree")
    currency = next(iter(currencies)) if currencies else default_currency.upper()
    void = _parse_status(source, _pick(row, A["status"]), remaining if remaining is not None else gross) == "void"
    kind = _pick(row, A["type"]).lower()
    credit = gross < 0 or kind in CREDIT_TYPES or (source == "quickbooks" and kind in QBO_PAYMENT_TYPES)
    if credited is not None:
        # Credit documents may export every figure with either sign; an invoice's credit is a magnitude.
        if credited < 0 and not credit: refuse("amount credited is negative")
        credited = abs(credited)
    if paid is not None or credited:
        if paid is not None and paid < 0: refuse("amount paid is negative")
        obligation = billing if billing is not None else gross
        # Credits may be exported with either sign; paid is a magnitude. Xero's
        # AmountCredited (credit notes allocated to the invoice) also reduces it.
        expected = abs(obligation) - (paid or 0) - (credited or 0)
        if credit and expected < 0: refuse("paid credit exceeds original credit")
        if not void and remaining is not None and (abs(remaining) if credit else remaining) != expected:
            refuse("gross/billing minus paid does not equal remaining")
    if billing is not None and (billing < 0 or billing > abs(gross)):
        refuse("Stripe billing amount exceeds gross or is negative")
    if remaining is not None and remaining > abs(gross):
        refuse("remaining balance exceeds gross")
    if remaining is not None and gross < 0 and abs(remaining) > abs(gross):
        refuse("remaining credit exceeds original credit")
    effective = remaining if remaining is not None else (balance.value if balance is not None else None)
    status = _pick(row, A["status"])
    if _parse_status(source, status, effective if effective is not None else gross) == "paid" and effective is not None and effective > 0:
        # Credit updates retain their existing specific refusal below.
        kind = _pick(row, A["type"]).lower()
        if not (kind in CREDIT_TYPES or gross < 0 or (source == "quickbooks" and kind in QBO_PAYMENT_TYPES)):
            if not _pick(row, A["due_date"]):
                raise _Refuse("terminal invoice update conflicts: invoice monetary evidence conflicts: paid status has a positive remaining balance; check the export")
            refuse("paid status has a positive remaining balance")
    if remaining is None:
        if (paid is not None or credited) and credit:
            balance = Amount((abs(gross) - (paid or 0) - (credited or 0)) * (-1 if gross < 0 else 1), currency)
        elif credited and balance is None:
            balance = Amount(gross - (paid or 0) - credited, currency)
        return balance, currency
    return Amount(remaining, currency), currency


def _invoice_row(source: str, row: Mapping[str, str], line_no: int, raw: str, order: str, default_currency: str, header_currencies=None):
    """One row: (Invoice, warning) | CreditNote | RowError (skipped). Raises _Refuse for a row error."""
    original_status = _pick(row, A["status"])
    row = _invoice_lifecycle(source, row)
    try:
        row = _stripe_paid_status(source, row)
    except _Refuse as exc:
        raise _Refuse("invoice lifecycle evidence conflicts: " + str(exc)) from exc
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
        prefix = "credit update cannot be trusted: " if kind in CREDIT_TYPES or (source == "quickbooks" and kind in QBO_PAYMENT_TYPES) else ""
        raise _Refuse(f"{prefix}amount is not a number: {amount_txt!r}")
    balance_txt = _pick(row, BALANCE[source])
    balance = _amount(balance_txt) if balance_txt else None
    if balance_txt and balance is None:
        prefix = "credit update cannot be trusted: " if kind in CREDIT_TYPES or amount.value < 0 or (source == "quickbooks" and kind in QBO_PAYMENT_TYPES) else ""
        raise _Refuse(f"{prefix}balance is not a number: {balance_txt!r}")
    credited_amount = _amount(_pick(row, A["credited"])) if _pick(row, A["credited"]) else None
    if balance is None and _pick(row, A["paid_amount"]):
        paid_txt = _pick(row, A["paid_amount"])
        paid_amount = _amount(paid_txt)
        if paid_amount is None:
            raise _Refuse(f"amount paid is not a number: {paid_txt!r}")
        balance = Amount(amount.value - paid_amount.value - (credited_amount.value if credited_amount else 0), amount.currency)
    balance, currency = _invoice_evidence(source, row, amount, balance, default_currency, header_currencies or {})
    balance_txt = _pick(row, A["balance"])
    issue_txt, due_txt = _pick(row, A["issue_date"]), _pick(row, A["due_date"])
    issue = parse_date(issue_txt, order=order)
    if issue is None:
        raise _Refuse(f"issue date is not a date: {issue_txt!r}")
    customer_name = _pick(row, A["customer_name"])
    customer_id, _ = _invoice_customer(source, row)
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
        if state is None:
            raise _Refuse(f"credit update cannot be trusted: unknown credit status {status_text!r}; check the export before changing a credit hold")
        if state == "paid" and (balance_txt or _pick(row, A["paid_amount"])) and remaining != 0:
            raise _Refuse("credit update cannot be trusted: paid credit has a nonzero exported balance; "
                          "check its remaining credit in the source system and re-export")
        if remaining == 0 or state in ("paid", "void"):
            state = "void" if state == "void" else "paid"
            remaining = Decimal("0.00")
            detail = f"{what} {number} is {state}; update the existing credit by its source and document number"
        else:
            detail = (f"payment of {currency} {abs(amount.value)} not applied to an invoice" if is_payment
                      else f"{what} for {currency} {remaining} - apply it to an open invoice")
        return CreditNote(number, customer_id, customer_name, issue, remaining, currency,
                          "unapplied_payment" if is_payment else "credit_note", line_no, detail,
                          state, abs(amount.value))

    if not number:
        raise _Refuse("missing invoice number")
    bal = balance.value if balance is not None else amount.value
    status = _parse_status(source, status_text, bal)
    if status is None:
        raise _Refuse(f"unknown status {status_text!r}")
    if balance is None and status not in ("paid", "void") and re.search(r"\bpart(ial|ially|ly)?\b|teilw|částečn|castecn", original_status.lower()):
        raise _Refuse(f"invoice {number} is marked {original_status!r} but the file has no balance column; Cash Desk does not chase "
                      "the full amount of a part-paid invoice - export it with its open balance (amount due) and re-import")
    if balance is None and status not in ("paid", "void") and any(k in row for k in A["paid_amount"]):
        raise _Refuse(f"amount paid cell is blank for invoice {number}; Cash Desk does not assume nothing was paid - "
                      "fill in the amount paid (0 if unpaid), the open balance or a paid status, then re-import")
    if balance is None and status not in ("paid", "void") and _invoice_paid_date(source, row, order) is not None:
        raise _Refuse(f"invoice {number} has a paid date but no open balance or paid status; Cash Desk does not chase it as fully owed - "
                      "fill in the open balance (0 if paid) or a paid status, then re-import")
    if balance is None and status not in ("paid", "void") and any(k in row for k in BALANCE[source] + A["balance"]):
        # A present balance column whose cell is empty says nothing about what
        # is still owed; reading it as the full amount would chase paid money.
        raise _Refuse(f"balance cell is blank for invoice {number}; Cash Desk does not assume the full amount is still owed - "
                      "fill in the open balance (0 if paid) or a paid status, then re-import")
    if status in ("paid", "void") and balance is None:
        bal = Decimal("0.00")
    if not due_txt:
        if status in ("paid", "void") or bal <= 0:
            if status == "paid" and bal > 0:
                raise _Refuse("terminal invoice update conflicts: paid status has a nonzero balance; check the source export")
            paid = _invoice_paid_date(source, row, order)
            return InvoiceClosure(number, customer_id, issue, amount.value, currency,
                                  "void" if status == "void" else "paid", paid, bal)
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
    paid = _invoice_paid_date(source, row, order)
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
             "date completed", "started date"),
    "amount": ("amount", "částka", "castka", "objem", "value", "betrag", "bedrag", "umsatz"),
    "debit": ("debit", "debit amount", "debits", "money out", "paid out", "withdrawal", "withdrawals", "outflow", "soll", "výdaj", "vydaj"),
    "credit": ("credit", "credit amount", "credits", "money in", "paid in", "deposit", "deposits", "inflow", "haben", "příjem", "prijem"),
    "direction": ("cdtdbtind", "cdt dbt ind", "creditdebitindicator", "credit debit indicator", "debit credit indicator", "credit debit",
                  "debit credit", "dr cr", "cr dr", "drcr", "d c", "c d", "dc", "soll haben", "s h", "af bij", "direction"),
    "status": ("status", "state", "transaction status", "booking status", "buchungsstatus", "umsatzstatus", "transaktionsstatus"),
    # Descriptive columns that some banks use for direction or lifecycle. Only an
    # exact direction/status word is read from them; any other value that still
    # names a debit or a not-booked state refuses the file (P2z), never guessed.
    "soft_direction": ("type", "transaction type", "txn type", "payment type", "entry type", "operation type", "umsatzart", "buchungsart"),
    "soft_status": ("info", "hinweis", "status info", "statusinfo", "umsatzinfo"),
    "currency": ("currency", "měna", "mena", "währung", "waehrung", "account currency", "statement currency", "currency code", "payment currency",
                 "transaction currency", "accountcurrency", "statementcurrency", "currencycode", "transactioncurrency", "ccy",
                 "měna účtu", "mena uctu", "kontowährung", "rekening valuta"),
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
                "successful", "done", "sent", "received", "paid", "gebucht", "zaúčtováno", "zauctovano", "provedeno", "verwerkt", "geboekt",
                "umsatz gebucht", "book"}
NOT_BOOKED_WORDS = {"pending", "failed", "declined", "cancelled", "canceled", "reverted", "reversed", "returned", "rejected",
                    "in progress", "processing", "scheduled", "authorised", "authorized", "on hold", "expired", "refunded",
                    "vorgemerkt", "abgelehnt", "storniert", "upcoming", "blocked", "held", "waiting", "void", "voided",
                    "umsatz vorgemerkt", "pdng", "umsatz storniert", "nicht gebucht", "noch nicht gebucht"}

# Only these exact words in a Type-like column are read as direction; single
# letters and symbols are too ambiguous outside a real direction column.
SOFT_DIRECTION_WORDS = {"debit", "credit", "dbit", "crdt", "dr", "cr", "soll", "haben", "deposit", "withdrawal", "incoming",
                        "outgoing", "inflow", "outflow", "in", "out", "bij", "af", "příjem", "prijem", "výdaj", "vydaj"}
_OUT_TOKENS = {"out", "pos", "atm", "fee", "fees", "charge", "charges", "dd", "sent", "check", "cheque", "dr", "dbit", "chk"}
_OUT_STEMS = ("debit", "withdraw", "payout", "lastschrift", "abbuchung", "belastung", "kartenzahl", "dauerauftrag", "card payment",
              "outgoing", "outflow", "purchase", "výdaj", "vydaj", "platba kartou", "payment to", "transfer to")
_INFO_PENDING = re.compile(r"\b(vorgemerkt|vormerkung|pending|reservierung|reserviert|autorisierung|authori[sz]ation|authori[sz]ed|"
                           r"nicht gebucht|not booked|unbooked|storniert|on hold|in bearbeitung)\b", re.I)


def _looks_outgoing(value: str) -> bool:
    low = value.lower().replace("_", " ")
    return bool(set(re.split(r"[^0-9a-zäöüßěščřžýáíéůú]+", low)) & _OUT_TOKENS) or any(stem in low for stem in _OUT_STEMS)


NONBOOKED_STATUS_GROUPS = (
    {"pending", "in progress", "processing", "scheduled", "authorised", "authorized", "on hold", "vorgemerkt", "upcoming", "blocked", "held", "waiting",
     "umsatz vorgemerkt", "pdng", "nicht gebucht", "noch nicht gebucht"},
    {"failed", "declined", "rejected", "expired", "abgelehnt"},
    {"cancelled", "canceled", "storniert", "void", "voided", "umsatz storniert"},
    {"reverted", "reversed", "returned"},
    {"refunded"},
)


def _bank_party_reference(keys, values):
    """Reconcile original cells, including repeated normalized headers.

    Transaction IDs and free text deliberately have no payer/remittance role.
    Equivalent spellings can agree; fuzzy names and distinct reference values
    require bank verification, even if a later ledger lookup might connect them.
    """
    from .matching import normalise_name
    cells = list(zip(keys, values))
    fields = {}
    selected = []
    conflict = False
    for role, aliases in (("payers", B["counterparty"]), ("references", B["reference"])):
        observed = [str(value).strip() for key, value in cells if key in aliases and str(value).strip()]
        fields[role] = observed
        normalized = [normalise_name(value) if role == "payers" else
                      "".join(c for c in clean_reference(value).casefold() if c.isalnum()) for value in observed]
        if observed and (not all(normalized) or len(set(normalized)) != 1):
            conflict = True
        if role == "payers":
            # A missing legal suffix is common bank abbreviation. Two explicitly
            # different legal forms, however, are conflicting identity evidence.
            suffixes = set()
            for value, base in zip(observed, normalized):
                full = normalise_name(value + " cashdesksentinel").removesuffix(" cashdesksentinel")
                suffix = "".join(full.removeprefix(base).split()) if base and full.startswith(base) else ""
                suffix = {"limited":"ltd", "spolsro":"sro"}.get(suffix,suffix)
                if suffix:
                    suffixes.add(suffix)
            if len(suffixes) > 1:
                conflict = True
        # Retain alias priority for compatible files, without losing repeated cells.
        choices = [str(value).strip() for alias in aliases for key, value in cells if key == alias and str(value).strip()]
        selected.append(choices[0] if choices else "")
    fields["cells"] = [[key, value] for key, value in cells]
    return selected[0], clean_reference(selected[1]), fields, conflict


def _bank_status(row, columns, *, info_is_status=False, may_be_receipt=True):
    """Require concordant populated lifecycle fields, retaining raw row evidence.

    Blank aliases may accompany a known field; a present but entirely empty
    lifecycle is unknown. Alias priority is stable across column reordering.
    An Info/Hinweis column is a status column only in a file where it carries
    exact statuses (Sparkasse 'Umsatz gebucht'/'Umsatz vorgemerkt'); elsewhere
    it is free text, except that pending wording beside a possible receipt
    refuses the file rather than counting it as cash (P2z).
    """
    strict = [key for key in B["status"] if key in columns]
    observed = [(key, row[key].strip().lower()) for key in strict if row.get(key, "").strip()]
    for key in B["soft_status"]:
        value = " ".join(row.get(key, "").split()).lower() if key in columns else ""
        if not value:
            continue
        if info_is_status:
            if value not in BOOKED_WORDS and value not in NOT_BOOKED_WORDS:
                raise ValueError(f"bank status evidence conflicts: the {key!r} column names a lifecycle Cash Desk cannot read ({value!r}); "
                                 "the file is refused rather than counted as money received; re-export with a booked/pending status column")
            observed.append((key, value))
        elif may_be_receipt and _INFO_PENDING.search(value):
            raise ValueError(f"bank status evidence conflicts: the {key!r} column names a lifecycle Cash Desk cannot read ({value!r}); "
                             "the file is refused rather than counted as money received; re-export with a booked/pending status column")
    if not strict and not observed:
        return "booked"
    if not observed:
        raise ValueError("bank status evidence conflicts: all status fields are empty; the file is refused rather than guessed")
    categories = set()
    for key, value in observed:
        if value in BOOKED_WORDS:
            categories.add("booked")
        elif value in NOT_BOOKED_WORDS:
            categories.add(next(i for i, group in enumerate(NONBOOKED_STATUS_GROUPS) if value in group))
        else:
            raise ValueError(f"bank status evidence conflicts: the {key!r} status column has values Cash Desk does not understand ({value!r}); "
                             "the file is refused rather than guessed; verify and correct the bank export")
    if len(categories) > 1:
        detail = ", ".join(f"{key}={value!r}" for key, value in observed)
        raise ValueError(f"bank status evidence conflicts: populated lifecycle fields disagree ({detail}); "
                         "the file is refused; verify the bank and re-export consistent statuses")
    return observed[0][1]


# These identify the statement account, never its counterparty account.
ACCOUNT_FIELDS = ("account", "account number", "account no", "account id", "own account", "iban", "own iban", "account iban", "statement iban")
BANK_FIELDS = ("bank", "bank name", "bic", "sort code", "routing number", "bank code")


COUNTERPARTY_ACCOUNT_KEYS = ("protiúčet", "protiucet", "číslo protiúčtu", "cislo protiuctu", "counterparty account",
                             "counter account", "counterparty iban", "payer account", "beneficiary account number", "gegenkonto")


def _bank_account(row, preamble):
    evidence = {}
    for index, raw in enumerate(preamble):
        try:
            cells = next(csv.reader([raw], delimiter=_delimiter(raw)))
        except (csv.Error, StopIteration):
            continue
        keys = [_norm_header(value) for value in cells]
        if len(cells)>1 and all(key in ACCOUNT_FIELDS+BANK_FIELDS+B["currency"] for key in keys):
            values = next(csv.reader([preamble[index+1]],delimiter=_delimiter(preamble[index+1])),[]) if index+1<len(preamble) else []
            for column,key in enumerate(keys):
                if key in ACCOUNT_FIELDS+BANK_FIELDS and column<len(values) and values[column].strip():
                    evidence.setdefault(key,set()).add(re.sub(r"[\s-]+","",values[column]).casefold())
            continue
        if len(cells) >= 2 and _norm_header(cells[0]) in ACCOUNT_FIELDS + BANK_FIELDS:
            key = _norm_header(cells[0]); value = cells[1].strip()
        else:
            match = re.match(r"^\s*(Account(?: number| no| id)?|IBAN|Bank|BIC|Sort code|Routing number)\s*:\s*(.+?)\s*$", raw, re.I)
            if not match: continue
            key, value = _norm_header(match[1]), match[2]
        if value: evidence.setdefault(key, set()).add(re.sub(r"[\s-]+", "", value).casefold())
    # Row-level bank columns (BIC, bank code) belong to the payer when the row also names the payer's
    # account (Fio: "Protiúčet" + "BIC"); otherwise (UK sort code + account number) they are our own.
    counterparty_bank = any(row.get(k, "").strip() for k in COUNTERPARTY_ACCOUNT_KEYS)
    for key in ACCOUNT_FIELDS + (() if counterparty_bank else BANK_FIELDS):
        if key != "iban" and row.get(key, "").strip():
            evidence.setdefault(key, set()).add(re.sub(r"[\s-]+", "", row[key]).casefold())
    if any(len(values) > 1 for values in evidence.values()):
        raise ValueError("bank account evidence conflicts: row and statement metadata disagree; verify the account")
    iban_keys = ("iban", "own iban", "account iban", "statement iban")
    ibans = {next(iter(v)) for k,v in evidence.items() if k in iban_keys}
    if len(ibans)>1: raise ValueError("bank account evidence conflicts: own IBAN aliases disagree")
    accounts = {next(iter(v)) for k,v in evidence.items() if k in ACCOUNT_FIELDS and k not in iban_keys}
    if ibans: return "iban:" + next(iter(ibans))
    if len(accounts) > 1:
        raise ValueError("bank account evidence conflicts: account identifiers disagree; verify the statement")
    if not accounts: return ""
    bank_evidence = {}
    for key, values in evidence.items():
        if key in BANK_FIELDS:
            canonical = "bank" if key in ("bank", "bank name") else ("bic" if key == "bic" else "routing")
            bank_evidence.setdefault(canonical,set()).update(values)
    if any(len(values)>1 for values in bank_evidence.values()):
        raise ValueError("bank account evidence conflicts: routing or bank aliases disagree")
    bank = "|".join(k+":"+next(iter(v)) for k,v in sorted(bank_evidence.items()))
    return "account:" + next(iter(accounts)) + ("|"+bank if bank else "")


def _bank_statement_currencies(preamble):
    """Read explicitly labelled statement currency, never infer it from an IBAN."""
    found = []
    rows = [next(csv.reader([raw], delimiter=_delimiter(raw)), []) for raw in preamble]
    for index, cells in enumerate(rows):
        keys = [_norm_header(value) for value in cells]
        # Horizontal account metadata has headings on one line, values on the next.
        if (len(cells) > 1 and all(key in B["currency"] + ACCOUNT_FIELDS + BANK_FIELDS for key in keys)
                and any(key in B["currency"] for key in keys)):
            values = rows[index+1] if index+1 < len(rows) else []
            for column,key in enumerate(keys):
                if key in B["currency"]:
                    found.append((f"preamble line {index+1} / {key}", values[column].strip() if column < len(values) else ""))
            continue
        if len(cells) >= 2 and keys[0] in B["currency"]:
            found.append((f"preamble line {index+1} / {keys[0]}", cells[1].strip()))
            found.extend((f"preamble line {index+1} / extra currency value",value.strip()) for value in cells[2:] if value.strip())
            continue
        for cell in cells:
            match = re.fullmatch(r"\s*(.+?)\s*[:=]\s*(.*?)\s*", cell)
            if match and _norm_header(match[1]) in B["currency"]:
                found.append((f"preamble line {index+1} / {_norm_header(match[1])}", match[2].strip()))
                continue
            match = re.fullmatch(r"\s*(.+?)\s*\(([A-Za-z]{3})\)\s*", cell)
            if match and _norm_header(match[1]) in B["currency"]+ACCOUNT_FIELDS:
                found.append((f"preamble line {index+1} / {_norm_header(match[1])}",match[2]))
                continue
            normalized = _norm_header(cell)
            for label in sorted(B["currency"],key=len,reverse=True):
                if normalized==label or normalized.startswith(label+" "):
                    found.append((f"preamble line {index+1} / {label}",normalized[len(label):].strip()))
                    break
    return found


def _bank_currency_basis(keys, headers, values, preamble, lang, default_currency):
    """The Amount's currency must agree with explicit statement/native evidence.

    A balance's currency cannot establish an unlabelled transaction amount's
    currency. Different native/account roles need a mapped export, not guessing
    or implicit FX. The configured fallback applies only without such evidence.
    """
    money = B["amount"] + B["debit"] + B["credit"]
    sources = [("statement",where,value) for where,value in _bank_statement_currencies(preamble)]
    unrelated = []
    for key,header,value in zip(keys,headers,values):
        value = str(value).strip()
        header_code = _header_currency(header)
        if header_code:
            if key in money+B["currency"]+ACCOUNT_FIELDS:
                sources.append(("amount header",header,header_code))
            else:
                unrelated.append((header,header_code))
        if key in B["currency"] and value:
            sources.append(("row",header,value))
        if key in money and value:
            parsed = _amount(value,lang)
            # Preserve explicit end tokens even if the quantity is malformed or
            # parse_amount refuses contradictory prefix/suffix currencies.
            prefix = re.match(r"^\s*[+\-(]?\s*([A-Za-z]{3}|Kč|zł|[€£$])(?=\s|\d|[+\-(])",value,re.I)
            suffix = re.search(r"(?<=[\d\s)])([A-Za-z]{3}|Kč|zł|[€£$])\s*[)\-]?\s*$",value,re.I)
            tokens = [match[1] for match in (prefix,suffix) if match]
            if tokens:
                sources.extend(("inline amount",header,"$" if token=="$" else normalise_currency(token) or token) for token in tokens)
            elif parsed and parsed.currency:
                sources.append(("inline amount",header,parsed.currency))
    codes = set()
    unresolved = []
    for role,where,value in sources:
        token = value.strip()
        code = token.upper() if re.fullmatch(r"[A-Za-z]{3}",token) else {"£":"GBP","€":"EUR","Kč":"CZK","Kc":"CZK"}.get(token)
        if code:codes.add(code)
        elif token and token!="$":unresolved.append(where+"="+repr(value))
    if any(value.strip()=="$" for _,_,value in sources) and not (len(codes)==1 and codes <= {"USD","CAD","AUD","NZD"}):
        unresolved.append("dollar symbol requires an explicit USD, CAD, AUD or NZD currency code")
    detail = ""
    if unresolved or len(codes)>1:
        detail = "explicit statement, amount-header, row or inline currency evidence disagrees or is unrecognized"
    elif not codes and (sources or unrelated):
        detail = "statement currency is unresolved; a balance/header currency does not establish the receipt amount currency"
    fallback = not codes and not detail
    resolved = next(iter(codes)) if len(codes)==1 else ""
    if fallback:
        resolved = default_currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}",resolved):
            detail = "configured default is not a three-letter currency code"
    basis = {"resolved":"" if detail else resolved, "basis":"unresolved" if detail else "configured_default" if fallback else "explicit", "sources":sources,
             "other_header_currencies":unrelated, "configured_default":default_currency,
             "headers":headers, "cells":[list(cell) for cell in zip(keys,values)], "preamble":preamble}
    if detail:
        detail = ("bank currency evidence conflicts: "+detail+"; verify the native transaction currency in the original bank "
                  "statement and re-export consistent currency fields; partial imports retain the raw question and pause all authority")
    return basis,detail


def read_bank_csv(text_or_path: str | os.PathLike[str], *, default_currency: str = "EUR", lang: str | None = None,
                  date_order: str | None = None) -> ImportResult:
    """Read a bank statement export into Payments.

    Headers may be English, Czech, German or Dutch; ``;`` or tab delimiters, a
    BOM, decimal commas, trailing minus signs and lines above the table are
    handled. The amount is a signed column, or separate Debit/Credit (Money
    in/Money out, Soll/Haben) columns; a direction column (``CdtDbtInd``
    CRDT/DBIT, DR/CR, S/H, Af/Bij) signs an unsigned amount, so only incoming
    credits are positive. Every populated amount representation must agree:
    Debit/Credit are nonnegative magnitudes; conflicting signs, values,
    currencies or direction columns are errors, never converted with abs().
    An explicit positive sign cannot be overridden by a debit indicator.
    With a status column only booked, completed, posted
    or settled lines count; pending and failed lines are skipped. Type and
    Transaction Type columns sign an unsigned amount only with an exact
    DEBIT/CREDIT word, and Info/Buchungsstatus 'Umsatz vorgemerkt' is pending;
    other debit or lifecycle wording there refuses the file. A direction
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
    rows = table.rows

    status_columns = [key for key in keys if key in B["status"] + B["soft_status"]]
    if len(status_columns) != len(set(status_columns)):
        return ImportResult("bank", "bank", errors=(RowError(table.header_line,
                            "bank status evidence conflicts: repeated status headers; the file is refused; re-export with unique lifecycle column names",
                            ",".join(table.raw_header), "bank_status_conflict"),), fingerprint=fp, rows=len(rows), columns=tuple(keys))
    info_is_status = any(" ".join(r.get(k, "").split()).lower() in BOOKED_WORDS | NOT_BOOKED_WORDS
                         for _, r, _, _ in rows for k in B["soft_status"] if k in keys)

    def negative(r):
        for k in B["amount"]:
            parsed = _amount(r.get(k, "").strip(), lang) if r.get(k, "").strip() else None
            if parsed is not None and parsed.value < 0:
                return True
        return False

    def strict_direction(r):
        return any(r.get(k, "").strip() for k in B["direction"] + B["debit"] + B["credit"])

    def may_be_receipt(r):
        if negative(r) or any(r.get(k, "").strip().lower() in DEBIT_WORDS for k in B["direction"]):
            return False
        debits = [_amount(r.get(k, "").strip(), lang) for k in B["debit"] if r.get(k, "").strip()]
        return not any(d is not None and d.value > 0 for d in debits)

    # A statement with any negative amount, or Debit/Credit columns, is signed:
    # its Type column is descriptive. Otherwise a Type column must carry the
    # direction (P2z), never be assumed to mean money received.
    signed_file = any(k in keys for k in B["debit"] + B["credit"]) or any(negative(r) for _, r, _, _ in rows)
    soft_modes = {line: ("check" if signed_file or strict_direction(r) else "sign") for line, r, _, _ in rows}
    statuses = {}
    status_errors = []
    for line, row, raw, _ in rows:
        try:
            statuses[line] = _bank_status(row, status_columns, info_is_status=info_is_status, may_be_receipt=may_be_receipt(row))
        except ValueError as exc:
            status_errors.append(RowError(line, str(exc), raw, "bank_status_conflict"))
    if status_errors:
        return ImportResult("bank", "bank", errors=tuple(status_errors), fingerprint=fp, rows=len(rows), columns=tuple(keys))

    import json
    alias_issues = []
    currency_issues = []
    currency_bases = {}
    preamble = text.splitlines()[:table.header_line-1]
    parties = {}
    for line, row, raw, values in rows:
        payer, reference, evidence, conflict = _bank_party_reference(keys, values)
        parties[line] = (payer, reference)
        basis, currency_detail = _bank_currency_basis(keys,table.raw_header,values,preamble,lang,default_currency)
        currency_bases[line] = basis
        if currency_detail:
            currency_evidence = {**evidence,"kind":"currency","currency_basis":basis,"headers":table.raw_header,"preamble":preamble}
            currency_issues.append(BankEvidenceIssue(line,raw,currency_detail,json.dumps(currency_evidence,ensure_ascii=False)))
        if conflict:
            detail = ("bank payer/reference evidence conflicts: populated aliases or repeated headers disagree or cannot identify "
                      "equivalent evidence; verify the original bank export, then correct and re-import; "
                      "partial imports retain this question and pause all allocation, collection and acknowledgements")
            evidence["headers"] = table.raw_header
            evidence["preamble"] = text.splitlines()[:table.header_line-1]
            alias_issues.append(BankAliasIssue(line, raw, detail, json.dumps(evidence, ensure_ascii=False)))
    alias_errors = [RowError(issue.row, issue.detail, issue.raw, "bank_alias_conflict") for issue in alias_issues]
    currency_errors = [RowError(issue.row,issue.detail,issue.raw,"bank_currency_conflict") for issue in currency_issues]
    alias_lines = {issue.row for issue in [*alias_issues,*currency_issues]}

    def refuse(message: str) -> ImportResult:
        return ImportResult("bank", "bank", errors=(*alias_errors, *currency_errors, RowError(table.header_line, message, ",".join(keys))), fingerprint=fp,
                            rows=len(rows), columns=tuple(keys), bank_alias_issues=tuple(alias_issues), bank_currency_issues=tuple(currency_issues))

    # Dict rows cannot retain two columns with the same normalized name.
    # Refuse ambiguous financial headers before any row can lose evidence.
    financial_keys = set(money_cols + B["direction"] + B["soft_direction"])
    repeated = sorted(k for k, count in Counter(keys).items() if count > 1 and k in financial_keys)
    if repeated:
        return refuse("bank amount evidence conflicts: repeated money, currency or direction headers "
                      f"{', '.join(repeated)}; check the bank export and use unique column names")
    direction_cols = [k for k in keys if k in B["direction"]]
    checks = [(column, CREDIT_WORDS | DEBIT_WORDS, "direction") for column in direction_cols]
    for column, known, what in checks:
        if column is None:
            continue
        unknown = sorted({r.get(column, "").strip() for _, r, _, _ in rows if r.get(column, "").strip().lower() not in known})
        if unknown:
            shown = ", ".join(repr(v) for v in unknown[:5])
            rule = ("only credits (CRDT, CR, H, Bij) are money received" if what == "direction"
                    else "only booked, completed, posted or settled lines count as money received")
            return refuse(f"the {column!r} {what} column has values Cash Desk does not understand ({shown}); {rule}, "
                          "so the file is refused rather than guessed")
    soft_cols = [k for k in keys if k in B["soft_direction"]]
    unread = []
    for line, r, _, _ in rows:
        if soft_modes[line] != "sign":
            continue
        for column in soft_cols:
            value = " ".join(r.get(column, "").split())
            if value and value.lower() not in SOFT_DIRECTION_WORDS and (_looks_outgoing(value) or value.lower() in {"d", "s", "c", "h", "+", "-"}):
                unread.append((line, column, value))
    if unread:
        shown = ", ".join(f"line {line} {column}={value!r}" for line, column, value in unread[:5])
        return refuse(f"bank direction evidence is unread: an unsigned positive amount sits beside a column that names a debit ({shown}); "
                      "the file is refused rather than counting money paid out as received; re-export with signed amounts or a "
                      "Debit/Credit or CdtDbtInd column")
    try:
        inferred = None if explicit else infer_date_order(_pick(r, B["date"]) for _, r, _, _ in rows)
    except DateOrderError as exc:
        return refuse(str(exc))
    order = explicit or inferred or "dmy"

    records: list[Payment] = []
    observations: list[BankObservation] = []
    preamble = text.splitlines()[:table.header_line-1]
    errors: list[RowError] = [*alias_errors,*currency_errors]
    warnings: list[RowError] = []
    skipped: list[RowError] = []
    seen: dict[str, int] = {}
    for line_no, row, raw, _values in rows:
        if line_no in alias_lines:
            continue  # Never derive or commit a payment identity from conflicting evidence.
        try:
            counterparty, reference = parties[line_no]
            basis = currency_bases[line_no]
            account = _bank_account(row, preamble)
            raw_id = _pick(row, B["id"])
            observed_status = statuses[line_no]
            if observed_status in NOT_BOOKED_WORDS:
                # Non-booked observations never become cash. Extract only usable
                # hints for conservative return review; missing/conflicting fields
                # remain unknown and widen the review instead of inventing facts.
                evidence = {"payer": counterparty, "reference": reference,
                            "memo": _pick(row, B["memo"]), "currency": _pick(row, B["currency"]).upper(), "amount": "", "date": ""}
                try:
                    hint = _bank_amount(row, lang, header_currency, resolved_currency=basis["resolved"] if basis["basis"]=="explicit" else None, soft=soft_modes[line_no])
                    evidence["amount"] = str(hint.value)
                    evidence["currency"] = evidence["currency"] or hint.currency or (basis["resolved"] if basis["basis"]=="explicit" else "")
                except (ValueError, TypeError):
                    pass
                try:
                    hint_date = parse_date(_pick(row, B["date"]), order=order)
                    evidence["date"] = hint_date.isoformat() if hint_date else ""
                except (ValueError, TypeError):
                    pass
                observations.append(BankObservation(None, raw_id, account, observed_status, raw, line_no, {**evidence,"currency_basis":basis}))
                skipped.append(RowError(line_no, f"status {observed_status} is not a booked transaction; lifecycle evidence retained", raw, "not_booked"))
                continue
            date_txt = _pick(row, B["date"])
            compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", date_txt)  # ING and camt exports write 20261103
            date = parse_date("-".join(compact.groups()) if compact else date_txt, order=order)
            if date is None:
                raise ValueError(f"date is not a date: {date_txt!r}")
            amount = _bank_amount(row, lang, header_currency, resolved_currency=basis["resolved"], soft=soft_modes[line_no])
            currency = basis["resolved"]
            memo = _pick(row, B["memo"])
            if not counterparty:
                # US ACH descriptions (Chase and others) carry the originator explicitly.
                orig = re.search(r"ORIG CO NAME:\s*(.+?)\s+(?:ORIG ID|DESC DATE|CO ENTRY|SEC|TRACE|EED|IND ID|IND NAME)\b", memo or "", re.IGNORECASE)
                if orig:
                    counterparty = orig.group(1).strip()
            pid = _pick(row, B["id"])
            origin = "provided" if pid else "derived"
            if not pid:
                digest = hashlib.sha256(f"{date}|{amount.value}|{currency}|{counterparty}|{reference}|{memo}".encode("utf-8")).hexdigest()[:12]
                pid = f"bank-{digest}"
                if pid in seen:
                    seen[pid] += 1
                    pid = f"{pid}-{seen[pid]}"
            identity_key = account + "|" + pid if account and origin == "provided" else pid
            if identity_key in seen:
                raise ValueError(f"duplicate payment id {pid}; counted once")
            seen.setdefault(identity_key, 1)
            payment = Payment(pid, date, amount.value, currency, reference, counterparty, memo, _pick(row, B["source"]) or "bank", origin)
            records.append(payment)
            observations.append(BankObservation(payment, raw_id, account, observed_status or "booked", raw, line_no, {"currency_basis":basis}))
            if basis["basis"] == "configured_default":
                warnings.append(RowError(line_no,f"bank currency: no explicit receipt currency; configured default {currency} used; verify the statement account currency before relying on this import",raw,"bank_currency_default"))
        except (ValueError, TypeError) as exc:
            errors.append(RowError(line_no, str(exc), raw))
    return ImportResult("bank", "bank", records=tuple(records), errors=tuple(errors), skipped=tuple(skipped), fingerprint=fp,
                        rows=len(rows), columns=tuple(keys), date_order=explicit or inferred, bank_observations=tuple(observations),
                        bank_alias_issues=tuple(alias_issues), bank_currency_issues=tuple(currency_issues), warnings=tuple(warnings))


def _bank_amount(row: Mapping[str, str], lang: str | None, header_currency: Mapping[str, str], *, resolved_currency=None, soft="sign"):
    """Reconcile all populated bank amount evidence, never erase its sign.

    Debit/Credit are nonnegative magnitudes. Amount is signed unless a
    direction column supplies the sign of an unsigned positive value.
    """
    def conflict(reason: str):
        raise ValueError(f"bank amount evidence conflicts: {reason}; check the bank export "
                         "and supply consistent Amount, Debit/Credit and direction values")

    directions = {1 if row[k].strip().lower() in CREDIT_WORDS else -1
                  for k in B["direction"] if row.get(k, "").strip()}
    # An exact DEBIT/CREDIT (DR/CR, Soll/Haben) in a Type column signs an unsigned
    # amount; in a signed file or beside real direction evidence it must agree (P2z).
    soft_directions = {1 if row[k].strip().lower() in CREDIT_WORDS else -1
                       for k in B["soft_direction"] if row.get(k, "").strip().lower() in SOFT_DIRECTION_WORDS}
    if len(soft_directions) > 1:
        conflict("direction columns disagree about incoming versus outgoing")
    if soft == "sign":
        directions |= soft_directions
    if len(directions) > 1:
        conflict("direction columns disagree about incoming versus outgoing")
    direction = next(iter(directions), None)
    currencies = {resolved_currency if row[k].strip()=="$" and resolved_currency in {"USD","CAD","AUD","NZD"} else
                  normalise_currency(row[k].strip()) or row[k].strip().upper() for k in B["currency"] if row.get(k, "").strip()}
    values: dict[str, list[Decimal]] = {"amount": [], "debit": [], "credit": []}
    for kind, group in values.items():
        for column in B[kind]:
            text = row.get(column, "").strip()
            if not text:
                continue
            parsed = _amount(text, lang)
            if parsed is None:
                conflict(f"{column} is not a number: {text!r}")
            parsed_currency = (resolved_currency if "$" in text and not re.search(r"[A-Za-z]{3}",text) and resolved_currency in {"USD","CAD","AUD","NZD"} else parsed.currency)
            currencies.update(c for c in (parsed_currency, header_currency.get(column)) if c)
            value = parsed.value
            if "+" in text and value < 0:
                conflict(f"{column} {text!r} contains both positive and negative signs")
            if kind in ("debit", "credit"):
                if value < 0:
                    conflict(f"{column} {text!r} is negative; Debit and Credit require nonnegative magnitudes")
            elif direction == 1 and value < 0:
                conflict(f"amount {text!r} is negative but the line is marked credit")
            elif direction == -1 and value > 0:
                if "+" in text:
                    conflict(f"amount {text!r} is explicitly positive but the line is marked debit")
                value = -value  # direction signs an unsigned magnitude only
            group.append(value)
        if len(set(group)) > 1:
            conflict(f"{kind} columns disagree")
    if len(currencies) > 1:
        conflict("currencies in the amount columns or currency fields disagree")
    amount, debit, credit = (values[k][0] if values[k] else None for k in ("amount", "debit", "credit"))
    if debit and credit:
        conflict("both debit and credit are nonzero")
    split = credit if credit else -debit if debit else Decimal("0.00")
    has_split = debit is not None or credit is not None
    if has_split and split and direction is not None and (split > 0) != (direction > 0):
        conflict("Debit/Credit columns disagree with the direction")
    if amount is not None and has_split and amount != split:
        conflict("Amount and Debit/Credit columns disagree in sign or magnitude")
    if amount is None and not has_split:
        raise ValueError("no amount: the amount, debit and credit columns are all empty")
    final = amount if amount is not None else split
    if soft != "sign" and soft_directions and final and (final > 0) != (next(iter(soft_directions)) > 0):
        conflict("the Type column disagrees with the signed amount or direction")
    return Amount(final, next(iter(currencies), None))


# ------------------------------------------------------------------- customers

C = {
    "id": ("customer id", "customerid", "contact id", "contactid", "id", "customer"),
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
        id_aliases, name_aliases = (*CUSTOMER_IDS, "id"), ("customer",)
    repeated = [k for k,count in Counter(keys).items() if count>1 and k in id_aliases]
    if repeated:
        return ImportResult("customers", "generic", errors=(RowError(table.header_line,
                            "customer identity conflicts: repeated customer ID headers; re-export unique original IDs",text,"customer_identity_conflict"),),fingerprint=fp,columns=tuple(keys))
    contact_aliases=set(C["email"])|set(C["contact"])
    if any(count>1 and key in contact_aliases for key,count in Counter(keys).items()):
        return ImportResult("customers","generic",errors=(RowError(table.header_line,"customer contact evidence conflicts: repeated contact headers; re-export unique columns",text,"customer_contact_conflict"),),fingerprint=fp,columns=tuple(keys))
    derive_ids = not _has(keys, id_aliases)
    missing = [] if _has(keys, name_aliases) else ["name"]
    if missing:
        return ImportResult("customers", "generic", errors=(RowError(table.header_line, f"missing column(s): {', '.join(missing)}", ",".join(keys)),),
                            fingerprint=fp, columns=tuple(keys))
    identities: list[CustomerIdentity] = []
    records: list[Customer] = []
    errors: list[RowError] = []
    warnings: list[RowError] = []
    seen: set[str] = set()
    for line_no, row, raw, _values in table.rows:
        try:
            if any(index>=len(_values) for index,key in enumerate(keys) if key in contact_aliases):
                raise ValueError("customer contact evidence conflicts: contact cell is missing from this shortened row; an explicit empty CSV cell is required to clear it")
            for aliases in (C["email"],C["contact"]):
                if len({row[key].strip() for key in aliases if row.get(key,"").strip()})>1:
                    raise ValueError("customer contact evidence conflicts: populated contact aliases disagree; verify the current recipient and re-export")
            name = _pick(row, name_aliases)
            values={row[k].strip() for k in id_aliases if row.get(k, "").strip()}
            if not derive_ids and len(values)>1:
                raise ValueError("customer identity conflicts: explicit customer ID aliases disagree; verify and re-export the original IDs")
            cid = customer_key(name) if derive_ids else next(iter(values), "")
            if not cid:
                raise ValueError("missing customer id")
            if not name:
                raise ValueError("missing customer name")
            if cid in seen:
                raise ValueError(f"duplicate customer id {cid}; counted once")
            language, supported = _language(_pick(row, C["language"]))
            on_hold = _pick(row, C["on_hold"]).strip().lower() in TRUE_WORDS
            records.append(Customer(cid, name, language, _pick(row, C["email"]), _pick(row, C["contact"]), on_hold, _pick(row, C["hold_reason"])))
            identities.append(CustomerIdentity("", cid, "derived" if derive_ids else "explicit", name, _pick(row, C["email"]), line_no, raw))
            seen.add(cid)
            if not supported:
                warnings.append(RowError(line_no, f"language {_pick(row, C['language'])!r} is not supported (en, cs); {name} is written to in English", raw, "language"))
        except (ValueError, TypeError) as exc:
            errors.append(RowError(line_no, str(exc), raw))
    return ImportResult("customers", "generic", records=tuple(records), errors=tuple(errors), fingerprint=fp, rows=len(table.rows),
                        columns=tuple(keys), warnings=tuple(warnings), customer_identities=tuple(identities),
                        customer_contact_fields=tuple(field for field,aliases in (("email",C["email"]),("contact_name",C["contact"])) if _has(keys,aliases)))


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
