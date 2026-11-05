"""CSV importers: invoice exports from four systems, bank statements, customers.

Every reader returns an :class:`ImportResult`. A malformed row is reported in
``errors`` with its line number and raw text and never raises; a row that is
deliberately not imported (a draft invoice, a payment line in an invoice
report) is listed in ``skipped``. ``fingerprint`` is a sha256 of the
normalised content so a caller can refuse to import the same file twice.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .dates import parse_date
from .model import INVOICE_STATUSES, LANGUAGES, Customer, Invoice, Payment
from .money import parse_amount

SOURCES = ("generic", "xero", "quickbooks", "stripe")
DATE_ORDERS = ("auto", "dmy", "mdy")

VOID_WORDS = {"void", "voided", "cancelled", "canceled", "credited", "written-off", "written off", "deleted"}


@dataclass(frozen=True)
class RowError:
    row: int
    message: str
    raw: str = ""


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

    @property
    def ok(self) -> bool:
        return not self.errors


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


def _norm_header(name: str) -> str:
    text = (name or "").replace("\ufeff", "").strip().lstrip("*").strip().lower()
    text = re.sub(r"\((?:utc)\)", "", text)
    text = re.sub(r"[\s_\-./:()]+", " ", text).strip()
    return text


def _rows(text: str) -> tuple[list[str], list[tuple[int, dict[str, str], str]]]:
    """Normalised headers and (line number, {normalised header: value}, raw line) per row."""
    text = text.lstrip("\ufeff")
    first = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(first))
    try:
        header = next(reader)
    except StopIteration:
        return [], []
    keys = [_norm_header(h) for h in header]
    out = []
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        row = {k: (values[i].strip() if i < len(values) else "") for i, k in enumerate(keys) if k}
        out.append((reader.line_num, row, ",".join(values)))
    return keys, out


def _pick(row: Mapping[str, str], aliases: tuple[str, ...]) -> str:
    for a in aliases:
        if a in row and row[a] != "":
            return row[a]
    return ""


def _has(keys: list[str], aliases: tuple[str, ...]) -> bool:
    return any(a in keys for a in aliases)


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def clean_reference(text: str) -> str:
    """``VS 20261104`` / ``VS: 20261104`` / ``variabilní symbol 20261104`` -> ``20261104``."""
    t = (text or "").strip()
    m = re.match(r"^(?:vs|v\.s\.|variabilní symbol|variabilni symbol|variable symbol)\s*[:.]?\s*(.+)$", t, re.IGNORECASE)
    return (m.group(1) if m else t).strip()


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
    "balance": ("balance", "amountdue", "amount due", "amount remaining", "open balance", "outstanding", "zbývá"),
    "status": ("status", "stav"),
    "reference": ("reference", "variable symbol", "variabilní symbol", "variabilni symbol", "vs", "payment reference"),
    "iban": ("iban", "bank account", "account iban"),
    "paid_date": ("paid date", "paiddate", "paid at", "paid", "datum úhrady"),
    "note": ("note", "notes", "memo", "memo description", "description", "poznámka"),
    "type": ("transaction type", "type"),
}

STATUS_MAPS: dict[str, dict[str, str]] = {
    "generic": {s: s for s in INVOICE_STATUSES} | {"partial": "open", "overdue": "open", "authorised": "open", "authorized": "open"},
    "xero": {"authorised": "open", "authorized": "open", "paid": "paid", "voided": "void", "deleted": "void", "disputed": "disputed", "open": "open", "void": "void"},
    "quickbooks": {"open": "open", "overdue": "open", "partially paid": "open", "partial": "open", "paid": "paid", "closed": "paid",
                   "voided": "void", "void": "void", "deleted": "void", "disputed": "disputed"},
    "stripe": {"open": "open", "paid": "paid", "void": "void", "uncollectible": "void", "disputed": "disputed"},
}
SKIP_STATUSES: dict[str, set[str]] = {"generic": set(), "xero": {"draft", "submitted"}, "quickbooks": set(), "stripe": {"draft", "deleted"}}
DEFAULT_ORDER = {"generic": "dmy", "xero": "dmy", "quickbooks": "mdy", "stripe": "dmy"}


def _parse_status(source: str, text: str, balance: Decimal) -> str | None:
    """Normalised status, None for an unknown value."""
    low = (text or "").strip().lower()
    if not low:
        return "paid" if balance <= 0 else "open"
    if low in VOID_WORDS:
        return "void"
    return STATUS_MAPS[source].get(low)


def read_invoices(source: str, text_or_path: str | os.PathLike[str], *, default_currency: str = "EUR", date_order: str = "auto") -> ImportResult:
    """Read an invoice export. ``source`` is generic, xero, quickbooks or stripe.

    ``date_order`` decides numeric slashed dates: ``mdy`` (QuickBooks US),
    ``dmy`` (everything else) or ``auto`` for the source's default. Dotted and
    ISO dates are unambiguous whatever the order.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown invoice source {source!r}; expected one of {', '.join(SOURCES)}")
    if date_order not in DATE_ORDERS:
        raise ValueError(f"date_order must be one of {', '.join(DATE_ORDERS)}")
    text, err = load_text(text_or_path)
    if err:
        return ImportResult("invoices", source, errors=(RowError(0, err),))
    order = DEFAULT_ORDER[source] if date_order == "auto" else date_order
    lang = "en" if order == "mdy" else "cs"
    keys, rows = _rows(text)
    fp = fingerprint(text)
    required = {"number": A["number"], "issue_date": A["issue_date"], "due_date": A["due_date"], "amount": A["amount"]}
    required["customer"] = A["customer_id"] if source == "generic" else A["customer_name"]
    missing = [name for name, aliases in required.items() if not _has(keys, aliases)]
    if missing:
        return ImportResult("invoices", source, errors=(RowError(1, f"missing column(s): {', '.join(missing)}", ",".join(keys)),), fingerprint=fp, columns=tuple(keys))

    records: list[Invoice] = []
    customers: dict[str, Customer] = {}
    errors: list[RowError] = []
    skipped: list[RowError] = []
    seen: set[str] = set()
    for line_no, row, raw in rows:
        kind = _pick(row, A["type"]).lower()
        if source == "quickbooks" and kind and kind != "invoice":
            skipped.append(RowError(line_no, f"transaction type {_pick(row, A['type'])} is not an invoice", raw))
            continue
        number = _pick(row, A["number"]) if source != "stripe" else (_pick(row, ("number",)) or _pick(row, ("id",)))
        status_text = _pick(row, A["status"])
        if status_text.strip().lower() in SKIP_STATUSES[source]:
            skipped.append(RowError(line_no, f"status {status_text} is not an issued invoice", raw))
            continue
        if not number:
            errors.append(RowError(line_no, "missing invoice number", raw))
            continue
        if number in seen:
            errors.append(RowError(line_no, f"duplicate invoice number {number}; counted once", raw))
            continue
        try:
            amount_txt, balance_txt = _pick(row, A["amount"]), _pick(row, A["balance"])
            amount = parse_amount(amount_txt)
            if amount is None:
                raise ValueError(f"amount is not a number: {amount_txt!r}")
            balance = parse_amount(balance_txt) if balance_txt else None
            if balance_txt and balance is None:
                raise ValueError(f"balance is not a number: {balance_txt!r}")
            currency = (_pick(row, A["currency"]) or amount.currency or default_currency).upper()
            issue_txt, due_txt = _pick(row, A["issue_date"]), _pick(row, A["due_date"])
            issue = parse_date(issue_txt, lang)
            due = parse_date(due_txt, lang)
            if issue is None:
                raise ValueError(f"issue date is not a date: {issue_txt!r}")
            if due is None:
                raise ValueError(f"due date is not a date: {due_txt!r}")
            bal = balance.value if balance is not None else amount.value
            status = _parse_status(source, status_text, bal)
            if status is None:
                raise ValueError(f"unknown status {status_text!r}")
            if status in ("paid", "void") and balance is None:
                bal = Decimal("0.00")
            paid_txt = _pick(row, A["paid_date"])
            paid = parse_date(paid_txt.split(" ")[0] if paid_txt else "", lang) if paid_txt else None
            if source == "generic":
                customer_id = _pick(row, A["customer_id"])
                if not customer_id:
                    raise ValueError("missing customer id")
            elif source == "stripe":
                customer_id = _pick(row, ("customer",)) or slugify(_pick(row, A["customer_name"]))
            else:
                customer_id = slugify(_pick(row, A["customer_name"]))
            if not customer_id:
                raise ValueError("missing customer")
            inv = Invoice(number, customer_id, issue, due, amount.value, currency, bal, status,
                          clean_reference(_pick(row, A["reference"])), _pick(row, A["iban"]), paid, _pick(row, A["note"]))
        except (ValueError, TypeError) as exc:
            errors.append(RowError(line_no, str(exc), raw))
            continue
        seen.add(number)
        records.append(inv)
        if source != "generic":
            name = _pick(row, A["customer_name"]) or customer_id
            lang_txt = _pick(row, A["language"]).lower() or "en"
            existing = customers.get(customer_id)
            new = Customer(customer_id, name, lang_txt if lang_txt in LANGUAGES else "en", _pick(row, A["email"]), _pick(row, A["contact"]))
            if existing is None:
                customers[customer_id] = new
            elif not existing.email and new.email or not existing.contact_name and new.contact_name:
                customers[customer_id] = Customer(customer_id, existing.name, existing.language, existing.email or new.email, existing.contact_name or new.contact_name)
    return ImportResult("invoices", source, tuple(records), tuple(customers.values()), tuple(errors), tuple(skipped), fp, len(rows), tuple(keys))


# ------------------------------------------------------------------------ bank

B = {
    "id": ("id", "line id", "transaction id", "txn id", "payment id", "id transakce", "id pohybu", "id operace", "reference number", "bank reference"),
    "date": ("date", "booking date", "value date", "transaction date", "datum", "datum zaúčtování", "datum zauctovani", "datum provedení", "posted"),
    "amount": ("amount", "částka", "castka", "objem", "value"),
    "currency": ("currency", "měna", "mena"),
    "counterparty": ("counterparty", "counterparty name", "payer", "payer name", "name", "název protiúčtu", "nazev protiuctu", "protiúčet název", "protiucet nazev", "název účtu", "sender"),
    "reference": ("reference", "variable symbol", "variabilní symbol", "variabilni symbol", "vs", "payment reference", "remittance", "remittance information", "end to end"),
    "memo": ("memo", "note", "description", "message", "zpráva pro příjemce", "zprava pro prijemce", "poznámka", "poznamka", "popis", "details"),
    "source": ("source", "channel"),
}


def read_bank_csv(text_or_path: str | os.PathLike[str], *, default_currency: str = "EUR", lang: str | None = None, date_order: str = "auto") -> ImportResult:
    """Read a bank statement export into Payments.

    Headers may be English or Czech; ``;`` or tab delimiters, a BOM and decimal
    commas are handled. A statement without an id column gets stable ids from
    the row content, so the same export imported twice yields the same ids.
    ``lang`` settles an ambiguous amount (``1.250``); ``date_order`` settles
    slashed dates (``dmy`` by default, ``mdy`` for a US export).
    """
    if date_order not in DATE_ORDERS:
        raise ValueError(f"date_order must be one of {', '.join(DATE_ORDERS)}")
    date_lang = "en" if date_order == "mdy" else "cs"
    text, err = load_text(text_or_path)
    if err:
        return ImportResult("bank", "bank", errors=(RowError(0, err),))
    keys, rows = _rows(text)
    fp = fingerprint(text)
    missing = [name for name in ("date", "amount") if not _has(keys, B[name])]
    if missing:
        return ImportResult("bank", "bank", errors=(RowError(1, f"missing column(s): {', '.join(missing)}", ",".join(keys)),), fingerprint=fp, columns=tuple(keys))
    records: list[Payment] = []
    errors: list[RowError] = []
    seen: dict[str, int] = {}
    for line_no, row, raw in rows:
        try:
            date_txt, amount_txt = _pick(row, B["date"]), _pick(row, B["amount"])
            date = parse_date(date_txt, date_lang)
            if date is None:
                raise ValueError(f"date is not a date: {date_txt!r}")
            amount = parse_amount(amount_txt, lang)
            if amount is None:
                raise ValueError(f"amount is not a number: {amount_txt!r}")
            currency = (_pick(row, B["currency"]) or amount.currency or default_currency).upper()
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
    return ImportResult("bank", "bank", tuple(records), (), tuple(errors), (), fp, len(rows), tuple(keys))


# ------------------------------------------------------------------- customers

C = {
    "id": ("customer id", "id", "customer"),
    "name": ("name", "customer name", "company", "contactname", "contact name"),
    "language": ("language", "lang", "jazyk"),
    "email": ("email", "emailaddress", "email address"),
    "contact": ("contact name", "contact", "contactperson", "contact person", "attention"),
    "on_hold": ("on hold", "hold", "do not contact"),
    "hold_reason": ("hold reason", "reason", "note"),
}
TRUE_WORDS = {"yes", "y", "true", "1", "ano", "x"}


def read_customers(text_or_path: str | os.PathLike[str]) -> ImportResult:
    """Read the generic customers CSV (customer_id, name, language, email, contact_name, on_hold, hold_reason)."""
    text, err = load_text(text_or_path)
    if err:
        return ImportResult("customers", "generic", errors=(RowError(0, err),))
    keys, rows = _rows(text)
    fp = fingerprint(text)
    missing = [name for name in ("id", "name") if not _has(keys, C[name])]
    if missing:
        return ImportResult("customers", "generic", errors=(RowError(1, f"missing column(s): {', '.join(missing)}", ",".join(keys)),), fingerprint=fp, columns=tuple(keys))
    records: list[Customer] = []
    errors: list[RowError] = []
    seen: set[str] = set()
    for line_no, row, raw in rows:
        try:
            cid, name = _pick(row, C["id"]), _pick(row, C["name"])
            if not cid:
                raise ValueError("missing customer id")
            if not name:
                raise ValueError("missing customer name")
            if cid in seen:
                raise ValueError(f"duplicate customer id {cid}; counted once")
            on_hold = _pick(row, C["on_hold"]).strip().lower() in TRUE_WORDS
            records.append(Customer(cid, name, _pick(row, C["language"]) or "en", _pick(row, C["email"]), _pick(row, C["contact"]), on_hold, _pick(row, C["hold_reason"])))
            seen.add(cid)
        except (ValueError, TypeError) as exc:
            errors.append(RowError(line_no, str(exc), raw))
    return ImportResult("customers", "generic", tuple(records), (), tuple(errors), (), fp, len(rows), tuple(keys))


__all__ = [
    "SOURCES",
    "DATE_ORDERS",
    "RowError",
    "ImportResult",
    "fingerprint",
    "load_text",
    "slugify",
    "clean_reference",
    "read_invoices",
    "read_bank_csv",
    "read_customers",
]
