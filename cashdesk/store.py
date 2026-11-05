"""SQLite persistence for the desk: customers, invoices, payments, matches,
drafts, approvals, sends, imports, events and the chase state.

Every write runs in a transaction and is idempotent or refuses with a typed
``StoreError``: the same import twice (``DuplicateImportError``), an approval
or a send against a hash that is not the draft's (``HashMismatchError``), a
send without an approval (``NotApprovedError``), a repeated or in-flight send
(``AlreadySentError``) and a conflicting rewrite, a held customer or a stale
reminder (``ConflictError``). Money is stored as text and read back as
Decimal. Timestamps are stored in UTC; a naive datetime is taken as UTC.

The store enforces the approval gate's invariants itself: a customer on hold
or in dispute cannot have a draft approved or sent, a reminder whose invoice
is no longer open cannot go out, a revision drops its approval, and a send is
reserved before the mail leaves and confirmed after, so a crash in between
refuses a blind retry instead of sending twice.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal

from .approval import idempotency_key, verify_hash
from .chasing import ChaseState
from .importers import ImportResult
from .model import Customer, Draft, Figure, Invoice, Match, Payment

BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")
UPSERT_MODES = ("replace", "fill")
REMINDER_KINDS = ("stage07", "stage14", "stage21", "stage28")
_UTC = dt.timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '', on_hold INTEGER NOT NULL DEFAULT 0, hold_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices (
    number TEXT PRIMARY KEY, customer_id TEXT NOT NULL, issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
    amount TEXT NOT NULL, currency TEXT NOT NULL, balance TEXT NOT NULL, status TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '', iban TEXT NOT NULL DEFAULT '', paid_date TEXT, note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS invoices_customer ON invoices (customer_id);
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY, date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '', counterparty TEXT NOT NULL DEFAULT '', memo TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'bank', added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS matches (
    payment_id TEXT NOT NULL, invoice_number TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL,
    method TEXT NOT NULL, confidence TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', applied_at TEXT NOT NULL,
    PRIMARY KEY (payment_id, invoice_number));
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, customer_id TEXT NOT NULL, invoice_numbers TEXT NOT NULL,
    language TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, recipients TEXT NOT NULL,
    created_at TEXT NOT NULL, status TEXT NOT NULL, hash TEXT NOT NULL, figures TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts (status);
CREATE TABLE IF NOT EXISTS approvals (
    draft_id TEXT PRIMARY KEY, hash TEXT NOT NULL, approved_at TEXT NOT NULL, approved_by TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sends (
    idempotency_key TEXT PRIMARY KEY, draft_id TEXT NOT NULL, hash TEXT NOT NULL, reserved_at TEXT NOT NULL,
    sent_at TEXT, mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent', note TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS imports (
    fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, imported_at TEXT NOT NULL,
    rows INTEGER NOT NULL, errors INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS chase_state (invoice_number TEXT PRIMARY KEY, stage TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS briefs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dismissed (payment_id TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draft_basis (draft_id TEXT NOT NULL, invoice_number TEXT NOT NULL, balance TEXT NOT NULL, currency TEXT NOT NULL,
    due_date TEXT NOT NULL, PRIMARY KEY (draft_id, invoice_number));
CREATE TABLE IF NOT EXISTS invoice_sources (number TEXT PRIMARY KEY, source TEXT NOT NULL, file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS missing_from_export (number TEXT PRIMARY KEY, file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snoozes (customer_id TEXT PRIMARY KEY, until TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
"""


class StoreError(Exception):
    """Base class: the store refused a write."""


class DuplicateImportError(StoreError):
    pass


class HashMismatchError(StoreError):
    pass


class NotApprovedError(StoreError):
    pass


class AlreadySentError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class NotFoundError(StoreError):
    pass


@dataclass(frozen=True)
class ImportRecord:
    fingerprint: str
    source: str
    kind: str
    imported_at: dt.datetime
    rows: int
    errors: int


@dataclass(frozen=True)
class Approval:
    draft_id: str
    hash: str
    approved_at: dt.datetime
    approved_by: str


@dataclass(frozen=True)
class SendRecord:
    idempotency_key: str
    draft_id: str
    hash: str
    sent_at: dt.datetime | None
    mode: str
    status: str = "sent"
    reserved_at: dt.datetime | None = None
    note: str = ""


@dataclass(frozen=True)
class Event:
    id: int
    at: dt.datetime
    kind: str
    ref: str
    detail: dict


@dataclass(frozen=True)
class PaymentView:
    """An applied match with the records around it, for the brief."""

    match: Match
    applied_at: dt.datetime
    payment: Payment | None
    invoice: Invoice | None
    customer: Customer | None


@dataclass(frozen=True)
class StatusSnapshot:
    as_of: dt.date
    open_by_bucket: dict[str, dict[str, Decimal]]
    open_count: int
    awaiting_approval: int
    approved_unsent: int
    held_drafts: int
    on_hold: int
    last_import: ImportRecord | None

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "open_by_bucket": {cur: {b: str(v) for b, v in buckets.items()} for cur, buckets in self.open_by_bucket.items()},
            "open_count": self.open_count,
            "awaiting_approval": self.awaiting_approval,
            "approved_unsent": self.approved_unsent,
            "held_drafts": self.held_drafts,
            "on_hold": self.on_hold,
            "last_import": None if self.last_import is None else {
                "fingerprint": self.last_import.fingerprint, "source": self.last_import.source, "kind": self.last_import.kind,
                "imported_at": self.last_import.imported_at.isoformat(), "rows": self.last_import.rows, "errors": self.last_import.errors,
            },
        }


@dataclass(frozen=True)
class BriefSnapshot:
    as_of: dt.date
    previous_brief_at: dt.datetime | None
    payments: tuple[PaymentView, ...]
    pending_drafts: tuple[Draft, ...]
    held_drafts: tuple[Draft, ...]
    customers: dict[str, Customer]
    invoices: dict[str, Invoice]


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=_UTC)


def _now(at: dt.datetime | None) -> dt.datetime:
    return _aware(at) if at is not None else dt.datetime.now(_UTC)


def _ts(at: dt.datetime | None) -> str:
    return _now(at).isoformat()


def _dt(text: str | None) -> dt.datetime | None:
    return _aware(dt.datetime.fromisoformat(text)) if text else None


def _d(text: str | None) -> dt.date | None:
    return dt.date.fromisoformat(text) if text else None


def bucket_for(days: int) -> str:
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


class Store:
    """Thin SQLite store. ``Store(":memory:")`` works for tests."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One transaction; nested calls join the outer one."""
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self._conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------- imports

    def has_import(self, fingerprint: str) -> bool:
        return self._conn.execute("SELECT 1 FROM imports WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None

    def record_import(self, result: ImportResult, at: dt.datetime | None = None, file_name: str = "") -> None:
        """Remember an import by fingerprint; the same content twice is refused."""
        with self._tx() as c:
            row = c.execute("SELECT imported_at FROM imports WHERE fingerprint = ?", (result.fingerprint,)).fetchone()
            if row is not None:
                when = str(row["imported_at"])[:16].replace("T", " ")
                raise DuplicateImportError(f"this {result.kind} file (the same content) was already imported on {when} UTC")
            c.execute("INSERT INTO imports (fingerprint, source, kind, imported_at, rows, errors) VALUES (?, ?, ?, ?, ?, ?)",
                      (result.fingerprint, result.source, result.kind, _ts(at), result.rows, len(result.errors)))
            detail = {"kind": result.kind, "source": result.source, "rows": result.rows, "errors": len(result.errors)}
            if file_name:
                detail["file"] = file_name
            self.log_event("import", result.fingerprint, detail, at)

    def ingest(self, result: ImportResult, at: dt.datetime | None = None, allow_errors: bool = False,
               file_name: str = "", open_list: bool = False) -> dict[str, int]:
        """Record the import and write its records in one transaction.

        Refuses a result with row errors unless ``allow_errors`` is set, so a
        half-read file is never silently half-imported. Customers derived from
        an invoice export only fill blanks (``mode="fill"``); the customers file
        is authoritative (``mode="replace"``).
        """
        if result.errors and not allow_errors:
            raise ConflictError(f"the {result.kind} file has {len(result.errors)} row error(s); fix it or pass allow_errors=True")
        counts: dict[str, int] = {}
        with self._tx():
            self.record_import(result, at, file_name)
            if result.kind == "customers":
                counts["customers"] = self.upsert_customers(result.records, at)
            elif result.kind == "invoices":
                records = list(result.records)
                derived = list(result.customers)
                if derived:
                    # a customer the ledger already knows under another id (a customers file with its own ids)
                    # is the same customer: match on the exact name instead of creating a twin
                    by_name = {" ".join(c.name.split()).casefold(): c.id for c in self.list_customers()}
                    known = {c.id for c in self.list_customers()}
                    remap = {c.id: by_name[" ".join(c.name.split()).casefold()] for c in derived
                             if c.id not in known and " ".join(c.name.split()).casefold() in by_name}
                    if remap:
                        records = [replace(inv, customer_id=remap.get(inv.customer_id, inv.customer_id)) for inv in records]
                        derived = [c for c in derived if c.id not in remap]
                    if derived:
                        counts["customers"] = self.upsert_customers(derived, at, mode="fill")
                counts["invoices"] = self.upsert_invoices(records, at)
                c = self._conn
                c.executemany("INSERT OR REPLACE INTO invoice_sources (number, source, file, at) VALUES (?, ?, ?, ?)",
                              [(inv.number, result.source, file_name, _ts(at)) for inv in records])
                c.executemany("DELETE FROM missing_from_export WHERE number = ?", [(inv.number,) for inv in records])
                if open_list:
                    present = {inv.number for inv in records}
                    gone = [r["number"] for r in c.execute(
                        "SELECT i.number FROM invoices i JOIN invoice_sources s ON s.number = i.number "
                        "WHERE s.source = ? AND i.status IN ('open', 'disputed') AND CAST(i.balance AS REAL) > 0", (result.source,)).fetchall()
                        if r["number"] not in present]
                    c.executemany("INSERT OR REPLACE INTO missing_from_export (number, file, at) VALUES (?, ?, ?)", [(n, file_name, _ts(at)) for n in gone])
                    for n in gone:
                        self.log_event("not_in_export", n, {"file": file_name}, at)
                    counts["not_in_export"] = len(gone)
                credits = tuple(getattr(result, "credit_notes", ()) or ())
                if credits:
                    counts["credit_notes"] = self.upsert_invoices(_credit_invoices(credits, at), at)
            elif result.kind == "bank":
                counts["payments"] = self.add_payments(result.records, at)
            else:
                raise ConflictError(f"unknown import kind {result.kind!r}")
        return counts

    def missing_from_export(self) -> dict[str, str]:
        """Open invoices that the latest open-invoice export of their source no longer lists -> that file."""
        return {r["number"]: r["file"] for r in self._conn.execute("SELECT number, file FROM missing_from_export").fetchall()}

    def snooze(self, customer_id: str, until: dt.date, reason: str = "", at: dt.datetime | None = None) -> None:
        """The customer promised to pay by ``until``: no reminder until then (and a short grace)."""
        self.get_customer(customer_id)
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO snoozes (customer_id, until, reason, at) VALUES (?, ?, ?, ?)", (customer_id, until.isoformat(), reason, _ts(at)))
            self.log_event("snooze", customer_id, {"until": until.isoformat(), "reason": reason}, at)

    def snoozed(self, as_of: dt.date, grace_days: int = 2) -> dict[str, tuple[dt.date, str]]:
        """Customers whose promise to pay has not run out on ``as_of`` -> (promised date, reason)."""
        out = {}
        for r in self._conn.execute("SELECT * FROM snoozes").fetchall():
            until = dt.date.fromisoformat(r["until"])
            if as_of <= until + dt.timedelta(days=grace_days):
                out[r["customer_id"]] = (until, r["reason"])
        return out

    def list_imports(self) -> list[ImportRecord]:
        rows = self._conn.execute("SELECT * FROM imports ORDER BY imported_at, rowid").fetchall()
        return [ImportRecord(r["fingerprint"], r["source"], r["kind"], _dt(r["imported_at"]), r["rows"], r["errors"]) for r in rows]

    # ----------------------------------------------------------- customers

    def upsert_customers(self, customers: Iterable[Customer], at: dt.datetime | None = None, mode: str = "replace") -> int:
        """Insert or update customers.

        ``replace`` (the customers file): name and language are written, a
        blank e-mail or contact never clears a stored one. ``fill`` (customers
        derived from an invoice export): only a missing customer is created and
        only blank e-mail and contact fields are filled. A hold in the store
        survives both; a hold on the incoming record is applied and moves the
        customer's pending and approved drafts to held.
        """
        if mode not in UPSERT_MODES:
            raise ValueError(f"mode must be one of {', '.join(UPSERT_MODES)}")
        if mode == "replace":
            update = """name = excluded.name, language = excluded.language,
                        email = CASE WHEN excluded.email <> '' THEN excluded.email ELSE customers.email END,
                        contact_name = CASE WHEN excluded.contact_name <> '' THEN excluded.contact_name ELSE customers.contact_name END"""
        else:
            update = """email = CASE WHEN customers.email = '' THEN excluded.email ELSE customers.email END,
                        contact_name = CASE WHEN customers.contact_name = '' THEN excluded.contact_name ELSE customers.contact_name END"""
        n = 0
        with self._tx() as c:
            for cu in customers:
                c.execute(
                    f"""INSERT INTO customers (id, name, language, email, contact_name, on_hold, hold_reason, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET {update},
                            on_hold = CASE WHEN excluded.on_hold = 1 THEN 1 ELSE customers.on_hold END,
                            hold_reason = CASE WHEN excluded.on_hold = 1 THEN excluded.hold_reason ELSE customers.hold_reason END,
                            updated_at = excluded.updated_at""",
                    (cu.id, cu.name, cu.language, cu.email, cu.contact_name, 1 if cu.on_hold else 0, cu.hold_reason, _ts(at)),
                )
                if cu.on_hold:
                    self._hold_drafts(cu.id, at)
                n += 1
        return n

    def get_customer(self, customer_id: str) -> Customer:
        row = self._conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no customer {customer_id!r}")
        return _customer(row)

    def list_customers(self) -> list[Customer]:
        return [_customer(r) for r in self._conn.execute("SELECT * FROM customers ORDER BY id").fetchall()]

    def _hold_drafts(self, customer_id: str, at: dt.datetime | None) -> None:
        """Pending and approved drafts of a held customer become held; their approvals are dropped."""
        c = self._conn
        c.execute("DELETE FROM approvals WHERE draft_id IN (SELECT id FROM drafts WHERE customer_id = ? AND status IN ('pending', 'approved'))", (customer_id,))
        c.execute("UPDATE drafts SET status = 'held', updated_at = ? WHERE customer_id = ? AND status IN ('pending', 'approved')", (_ts(at), customer_id))

    def set_hold(self, customer_id: str, reason: str, at: dt.datetime | None = None) -> None:
        """Put a customer on hold; their pending and approved drafts become held (do not approve)."""
        with self._tx() as c:
            self.get_customer(customer_id)
            c.execute("UPDATE customers SET on_hold = 1, hold_reason = ?, updated_at = ? WHERE id = ?", (reason, _ts(at), customer_id))
            self._hold_drafts(customer_id, at)
            self.log_event("hold", customer_id, {"reason": reason}, at)

    def clear_hold(self, customer_id: str, at: dt.datetime | None = None) -> None:
        """Lift a hold; held drafts go back to pending and need a fresh approval."""
        with self._tx() as c:
            self.get_customer(customer_id)
            c.execute("UPDATE customers SET on_hold = 0, hold_reason = '', updated_at = ? WHERE id = ?", (_ts(at), customer_id))
            c.execute("UPDATE drafts SET status = 'pending', updated_at = ? WHERE customer_id = ? AND status = 'held'", (_ts(at), customer_id))
            self.log_event("unhold", customer_id, {}, at)

    def _blocked_reason(self, customer_id: str) -> str | None:
        """Why nothing may be approved or sent to this customer right now."""
        row = self._conn.execute("SELECT on_hold, hold_reason FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is not None and row["on_hold"]:
            return f"customer {customer_id} is on hold ({row['hold_reason'] or 'no reason recorded'})"
        disputed = self._conn.execute(
            "SELECT number FROM invoices WHERE customer_id = ? AND status = 'disputed' AND CAST(balance AS REAL) > 0 ORDER BY number LIMIT 1", (customer_id,)
        ).fetchone()
        if disputed is not None:
            return f"customer {customer_id} has a disputed invoice ({disputed['number']})"
        return None

    def _stale_reason(self, draft: Draft) -> str | None:
        """A reminder must still be true when it is approved and when it is sent: every invoice
        open, and its open balance and due date still the figures the draft states."""
        if draft.kind not in REMINDER_KINDS:
            return None
        money = {f.canonical for f in draft.figures if f.kind == "money"}
        dates = {f.canonical for f in draft.figures if f.kind == "date"}
        basis = {r["invoice_number"]: r for r in self._conn.execute("SELECT * FROM draft_basis WHERE draft_id = ?", (draft.id,)).fetchall()}
        for number in draft.invoice_numbers:
            row = self._conn.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()
            if row is None:
                return f"{number} is not in the ledger"
            inv = _invoice(row)
            if not inv.is_open:
                return f"{number} is {inv.status}; redraft before approving"
            was = basis.get(number)
            if was is not None:
                if (Decimal(was["balance"]), was["currency"]) != (inv.balance, inv.currency):
                    return f"{number} now has {inv.currency} {inv.balance:,.2f} open, not the amount this draft states; redraft before approving"
                if was["due_date"] != inv.due_date.isoformat():
                    return f"{number} is now due {inv.due_date.isoformat()}, not the date this draft states; redraft before approving"
                continue
            if money and f"{inv.currency} {inv.balance:.2f}" not in money:
                return f"{number} now has {inv.currency} {inv.balance:,.2f} open, not the amount this draft states; redraft before approving"
            if dates and inv.due_date.isoformat() not in dates:
                return f"{number} is now due {inv.due_date.isoformat()}, not the date this draft states; redraft before approving"
        return None

    def stale_drafts(self) -> list[tuple[Draft, str]]:
        """Pending or approved reminders that are no longer true, with the reason."""
        out = []
        for d in self.list_drafts():
            if d.status in ("pending", "approved", "held"):
                why = self._stale_reason(d)
                if why:
                    out.append((d, why))
        return out

    def _applied_total(self, c: sqlite3.Connection, number: str) -> Decimal | None:
        rows = c.execute("SELECT amount FROM matches WHERE invoice_number = ?", (number,)).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0")) if rows else None

    # ------------------------------------------------------------ invoices

    def upsert_invoices(self, invoices: Iterable[Invoice], at: dt.datetime | None = None) -> int:
        """Insert or update invoices from an export. Payments Cash Desk already applied are
        never undone by an older export: the open balance kept is the lower of the export's
        balance and the invoice amount minus what was applied here."""
        n = 0
        with self._tx() as c:
            for inv in invoices:
                applied = self._applied_total(c, inv.number)
                if applied is not None and inv.status in ("open", "paid", "disputed"):
                    kept = max(Decimal("0.00"), min(inv.balance, inv.amount - applied)).quantize(Decimal("0.01"))
                    if kept != inv.balance:
                        prior = c.execute("SELECT paid_date FROM invoices WHERE number = ?", (inv.number,)).fetchone()
                        paid = inv.paid_date or (_d(prior["paid_date"]) if prior else None)
                        self.log_event("import_kept_payment", inv.number, {"export_balance": str(inv.balance), "kept_balance": str(kept), "applied": str(applied), "currency": inv.currency}, at)
                        inv = replace(inv, balance=kept, status="paid" if kept <= 0 else inv.status, paid_date=paid if kept <= 0 else inv.paid_date)
                c.execute(
                    """INSERT INTO invoices (number, customer_id, issue_date, due_date, amount, currency, balance, status, reference, iban, paid_date, note, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(number) DO UPDATE SET customer_id = excluded.customer_id, issue_date = excluded.issue_date,
                           due_date = excluded.due_date, amount = excluded.amount, currency = excluded.currency, balance = excluded.balance,
                           status = excluded.status, reference = excluded.reference, iban = excluded.iban, paid_date = excluded.paid_date,
                           note = excluded.note, updated_at = excluded.updated_at""",
                    (inv.number, inv.customer_id, inv.issue_date.isoformat(), inv.due_date.isoformat(), str(inv.amount), inv.currency,
                     str(inv.balance), inv.status, inv.reference, inv.iban, inv.paid_date.isoformat() if inv.paid_date else None, inv.note, _ts(at)),
                )
                n += 1
        return n

    def get_invoice(self, number: str) -> Invoice:
        row = self._conn.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()
        if row is None:
            raise NotFoundError(f"no invoice {number!r}")
        return _invoice(row)

    def list_invoices(self, status: str | None = None, customer_id: str | None = None) -> list[Invoice]:
        sql, args = "SELECT * FROM invoices", []
        where = []
        if status:
            where.append("status = ?")
            args.append(status)
        if customer_id:
            where.append("customer_id = ?")
            args.append(customer_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_invoice(r) for r in self._conn.execute(sql + " ORDER BY due_date, number", args).fetchall()]

    # ------------------------------------------------------------ payments

    def add_payments(self, payments: Iterable[Payment], at: dt.datetime | None = None) -> int:
        """Insert new payments; a known id is ignored. Returns how many were new."""
        n = 0
        with self._tx() as c:
            for p in payments:
                cur = c.execute(
                    "INSERT OR IGNORE INTO payments (id, date, amount, currency, reference, counterparty, memo, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p.id, p.date.isoformat(), str(p.amount), p.currency, p.reference, p.counterparty, p.memo, p.source, _ts(at)),
                )
                n += cur.rowcount
        return n

    def get_payment(self, payment_id: str) -> Payment | None:
        row = self._conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        return _payment(row) if row else None

    def list_payments(self) -> list[Payment]:
        return [_payment(r) for r in self._conn.execute("SELECT * FROM payments ORDER BY date, id").fetchall()]

    def unapplied_payments(self) -> list[Payment]:
        """Receipts with money not yet applied and not dismissed by a person: what the next
        matching run looks at. A payment a person split in part comes back with what is left."""
        out = []
        for r in self._conn.execute("SELECT * FROM payments WHERE id NOT IN (SELECT payment_id FROM dismissed) ORDER BY date, id").fetchall():
            p = _payment(r)
            used = self.applied_from(p.id)
            if used == 0:
                out.append(p)
            elif p.amount > 0 and used < p.amount:
                out.append(replace(p, amount=p.amount - used))
        return out

    def applied_from(self, payment_id: str) -> Decimal:
        rows = self._conn.execute("SELECT amount FROM matches WHERE payment_id = ?", (payment_id,)).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))

    def apply_by_person(self, payment_id: str, invoice_number: str, amount: Decimal | None = None,
                        by: str = "", at: dt.datetime | None = None) -> Match:
        """A person applies (part of) a payment that matching left for review. Never more than is
        left of the payment or open on the invoice; same currency only."""
        p = self.get_payment(payment_id)
        if p is None:
            raise NotFoundError(f"no payment {payment_id!r}")
        if p.amount <= 0:
            raise ConflictError(f"{payment_id} is money going out, not a receipt; it cannot pay an invoice")
        if self._conn.execute("SELECT 1 FROM dismissed WHERE payment_id = ?", (payment_id,)).fetchone():
            raise ConflictError(f"{payment_id} was dismissed; it cannot be applied")
        inv = self.get_invoice(invoice_number)
        if not inv.is_open or inv.balance <= 0:
            raise ConflictError(f"{invoice_number} is {inv.status} with nothing open")
        if inv.currency != p.currency:
            raise ConflictError(f"{payment_id} is in {p.currency}, {invoice_number} in {inv.currency}")
        left = p.amount - self.applied_from(payment_id)
        if left <= 0:
            raise ConflictError(f"{payment_id} is already fully applied")
        value = (amount if amount is not None else min(left, inv.balance)).quantize(Decimal("0.01"))
        if value <= 0:
            raise ConflictError("the amount to apply must be more than zero")
        if value > left:
            raise ConflictError(f"only {p.currency} {left:,.2f} of {payment_id} is left to apply")
        if value > inv.balance:
            raise ConflictError(f"only {inv.currency} {inv.balance:,.2f} is open on {invoice_number}")
        match = Match(payment_id, invoice_number, value, p.currency, "person", "person",
                      f"applied by a person{(' (' + by + ')') if by else ''}")
        if not self.apply_match(match, at):
            raise ConflictError(f"{payment_id} is already applied to {invoice_number}")
        return match

    def dismiss_payment(self, payment_id: str, reason: str = "", at: dt.datetime | None = None) -> None:
        """A person says a receipt pays none of these invoices (other income, a transfer): it leaves review."""
        if self.get_payment(payment_id) is None:
            raise NotFoundError(f"no payment {payment_id!r}")
        p = self.get_payment(payment_id)
        used = self.applied_from(payment_id)
        if used >= p.amount > 0:
            raise ConflictError(f"{payment_id} is already fully applied; there is nothing left to dismiss")
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO dismissed (payment_id, reason, at) VALUES (?, ?, ?)", (payment_id, reason, _ts(at)))
            self.log_event("dismiss", payment_id, {"reason": reason}, at)

    def apply_match(self, match: Match, at: dt.datetime | None = None) -> bool:
        """Apply a match to its invoice once. False when this payment/invoice pair is already applied."""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM matches WHERE payment_id = ? AND invoice_number = ?", (match.payment_id, match.invoice_number)).fetchone():
                return False
            row = c.execute("SELECT * FROM invoices WHERE number = ?", (match.invoice_number,)).fetchone()
            if row is None:
                raise ConflictError(f"cannot apply {match.payment_id}: invoice {match.invoice_number} is not in the ledger")
            inv = _invoice(row)
            if inv.currency != match.currency:
                raise ConflictError(f"cannot apply {match.payment_id}: {match.currency} against {inv.number} in {inv.currency}")
            new_balance = inv.balance - match.amount
            status = inv.status
            paid_date = inv.paid_date
            if new_balance <= 0:
                new_balance = Decimal("0.00")
                status = "paid"
                payment = self.get_payment(match.payment_id)
                paid_date = payment.date if payment else _now(at).date()
            c.execute("INSERT INTO matches (payment_id, invoice_number, amount, currency, method, confidence, reason, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (match.payment_id, match.invoice_number, str(match.amount), match.currency, match.method, match.confidence, match.reason, _ts(at)))
            c.execute("UPDATE invoices SET balance = ?, status = ?, paid_date = ?, updated_at = ? WHERE number = ?",
                      (str(new_balance), status, paid_date.isoformat() if paid_date else None, _ts(at), inv.number))
            self.log_event("match", match.invoice_number, {"payment": match.payment_id, "amount": str(match.amount), "currency": match.currency, "method": match.method}, at)
            return True

    def list_matches(self) -> list[Match]:
        return [_match(r) for r in self._conn.execute("SELECT * FROM matches ORDER BY applied_at, rowid").fetchall()]

    # -------------------------------------------------------------- drafts

    def _check_hash(self, draft: Draft) -> None:
        if not verify_hash(draft, draft.body, draft.recipients, draft.subject):
            raise ConflictError(f"draft {draft.id}: its hash does not cover its own text; build drafts with render_draft or approval.with_hash")

    def create_draft(self, draft: Draft, at: dt.datetime | None = None) -> bool:
        """Insert a draft. The same draft again is a no-op; a different text under the same id is refused."""
        self._check_hash(draft)
        with self._tx() as c:
            row = c.execute("SELECT hash, status FROM drafts WHERE id = ?", (draft.id,)).fetchone()
            if row is not None:
                if row["hash"] == draft.hash:
                    return False
                if row["status"] != "rejected":
                    raise ConflictError(f"draft {draft.id} already exists with a different text; use replace_draft to revise it")
                # a rejected (or superseded) draft of the same day: the new, different text takes its place
                c.execute("DELETE FROM drafts WHERE id = ?", (draft.id,))
                c.execute("DELETE FROM approvals WHERE draft_id = ?", (draft.id,))
            customer = c.execute("SELECT on_hold FROM customers WHERE id = ?", (draft.customer_id,)).fetchone()
            status = "held" if (customer and customer["on_hold"] and draft.status == "pending") else draft.status
            c.execute(
                "INSERT INTO drafts (id, kind, customer_id, invoice_numbers, language, subject, body, recipients, created_at, status, hash, figures, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (draft.id, draft.kind, draft.customer_id, json.dumps(list(draft.invoice_numbers)), draft.language, draft.subject, draft.body,
                 json.dumps(list(draft.recipients)), _ts(draft.created_at), status, draft.hash, _figures_json(draft.figures), _ts(at)),
            )
            c.execute("DELETE FROM draft_basis WHERE draft_id = ?", (draft.id,))
            for number in draft.invoice_numbers:
                row = c.execute("SELECT balance, currency, due_date FROM invoices WHERE number = ?", (number,)).fetchone()
                if row is not None:
                    c.execute("INSERT INTO draft_basis (draft_id, invoice_number, balance, currency, due_date) VALUES (?, ?, ?, ?, ?)",
                              (draft.id, number, row["balance"], row["currency"], row["due_date"]))
            self.log_event("draft", draft.id, {"kind": draft.kind, "customer": draft.customer_id, "status": status}, at)
            return True

    def create_drafts(self, drafts: Iterable[Draft], at: dt.datetime | None = None) -> int:
        with self._tx():
            return sum(1 for d in drafts if self.create_draft(d, at))

    def get_draft(self, draft_id: str) -> Draft:
        row = self._conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no draft {draft_id!r}")
        return _draft(row)

    def list_drafts(self, status: str | None = None, customer_id: str | None = None) -> list[Draft]:
        sql, args, where = "SELECT * FROM drafts", [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if customer_id:
            where.append("customer_id = ?")
            args.append(customer_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_draft(r) for r in self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()]

    def replace_draft(self, draft: Draft, at: dt.datetime | None = None) -> None:
        """Store a revised text under the same id. Any approval is dropped; a sent draft cannot change.

        Only subject, body, recipients, hash and figures may change: a revision
        that switches kind, customer or invoices is a different draft.
        """
        self._check_hash(draft)
        with self._tx() as c:
            current = self.get_draft(draft.id)
            if current.status == "sent":
                raise ConflictError(f"draft {draft.id} was already sent and cannot be revised")
            if (current.kind, current.customer_id, current.invoice_numbers) != (draft.kind, draft.customer_id, draft.invoice_numbers):
                raise ConflictError(f"draft {draft.id}: a revision may change the text, not the kind, customer or invoices")
            customer = c.execute("SELECT on_hold FROM customers WHERE id = ?", (draft.customer_id,)).fetchone()
            status = "held" if (customer and customer["on_hold"]) else "pending"
            c.execute("UPDATE drafts SET subject = ?, body = ?, recipients = ?, hash = ?, figures = ?, status = ?, updated_at = ? WHERE id = ?",
                      (draft.subject, draft.body, json.dumps(list(draft.recipients)), draft.hash, _figures_json(draft.figures), status, _ts(at), draft.id))
            c.execute("DELETE FROM approvals WHERE draft_id = ?", (draft.id,))
            self.log_event("revise", draft.id, {"hash": draft.hash}, at)

    def reject_draft(self, draft_id: str, reason: str = "", at: dt.datetime | None = None, reset_stage: bool = True) -> None:
        with self._tx() as c:
            current = self.get_draft(draft_id)
            if current.status == "sent":
                raise ConflictError(f"draft {draft_id} was already sent")
            c.execute("UPDATE drafts SET status = 'rejected', updated_at = ? WHERE id = ?", (_ts(at), draft_id))
            c.execute("DELETE FROM approvals WHERE draft_id = ?", (draft_id,))
            if reset_stage and current.kind in REMINDER_KINDS:
                # the next drafting run may write this stage again (a person rejected the text, not the chase);
                # a rejected escalation note keeps its stage, so the customer is not chased again for it
                c.executemany("DELETE FROM chase_state WHERE invoice_number = ? AND stage != 'thanks'", [(n,) for n in current.invoice_numbers])
            self.log_event("reject", draft_id, {"reason": reason}, at)

    # ---------------------------------------------------- approvals, sends

    def approval(self, draft_id: str) -> Approval | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE draft_id = ?", (draft_id,)).fetchone()
        return Approval(row["draft_id"], row["hash"], _dt(row["approved_at"]), row["approved_by"]) if row else None

    def _gate(self, draft: Draft, hash: str) -> None:
        """The checks every approval and send must pass: exact hash, no hold, nothing stale."""
        if hash != draft.hash:
            raise HashMismatchError(f"draft {draft.id}: the hash does not match the stored text; re-read the draft")
        blocked = self._blocked_reason(draft.customer_id)
        if blocked:
            raise ConflictError(f"draft {draft.id} cannot go out: {blocked}")
        stale = self._stale_reason(draft)
        if stale:
            raise ConflictError(f"draft {draft.id} is stale: {stale}")

    def approve_draft(self, draft_id: str, hash: str, approved_by: str = "", at: dt.datetime | None = None) -> None:
        """Approve exactly the stored text. Refused for a held, sent or rejected draft, a customer on hold or in dispute, or a stale reminder."""
        with self._tx() as c:
            current = self.get_draft(draft_id)
            self._gate(current, hash)
            if current.status in ("sent", "rejected", "held"):
                raise ConflictError(f"draft {draft_id} is {current.status} and cannot be approved")
            c.execute("INSERT OR REPLACE INTO approvals (draft_id, hash, approved_at, approved_by) VALUES (?, ?, ?, ?)", (draft_id, hash, _ts(at), approved_by))
            c.execute("UPDATE drafts SET status = 'approved', updated_at = ? WHERE id = ?", (_ts(at), draft_id))
            self.log_event("approve", draft_id, {"hash": hash, "by": approved_by}, at)

    def reserve_send(self, draft_id: str, hash: str, mode: str, at: dt.datetime | None = None) -> str:
        """Reserve the one send of an approved draft and return its idempotency key.

        Call this before the mail leaves and ``confirm_send`` after. A second
        reservation, whether the first is in flight or done, is refused; a
        failed one (``fail_send``) may be retried under the same key.
        """
        with self._tx() as c:
            current = self.get_draft(draft_id)
            self._gate(current, hash)
            approval = self.approval(draft_id)
            if approval is None or approval.hash != hash:
                raise NotApprovedError(f"draft {draft_id} has no approval for this text")
            key = idempotency_key(draft_id, hash)
            row = c.execute("SELECT status FROM sends WHERE idempotency_key = ?", (key,)).fetchone()
            if current.status == "sent" or (row is not None and row["status"] == "sent"):
                raise AlreadySentError(f"draft {draft_id} was already sent (key {key})")
            if row is not None and row["status"] == "reserved":
                raise AlreadySentError(f"draft {draft_id} has a send in progress (key {key}); confirm or fail it first")
            c.execute("INSERT OR REPLACE INTO sends (idempotency_key, draft_id, hash, reserved_at, sent_at, mode, status, note) VALUES (?, ?, ?, ?, NULL, ?, 'reserved', '')",
                      (key, draft_id, hash, _ts(at), mode))
            self.log_event("send_reserved", draft_id, {"mode": mode, "key": key}, at)
            return key

    def confirm_send(self, key: str, at: dt.datetime | None = None) -> None:
        """Mark a reserved send as done; the draft becomes sent."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM sends WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                raise NotFoundError(f"no send reserved under key {key}")
            if row["status"] != "reserved":
                raise ConflictError(f"send {key} is {row['status']}, not reserved")
            c.execute("UPDATE sends SET status = 'sent', sent_at = ? WHERE idempotency_key = ?", (_ts(at), key))
            c.execute("UPDATE drafts SET status = 'sent', updated_at = ? WHERE id = ?", (_ts(at), row["draft_id"]))
            self.log_event("send", row["draft_id"], {"mode": row["mode"], "key": key}, at)

    def fail_send(self, key: str, reason: str = "", at: dt.datetime | None = None) -> None:
        """Mark a reserved send as failed so it can be retried; the draft stays approved."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM sends WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                raise NotFoundError(f"no send reserved under key {key}")
            if row["status"] != "reserved":
                raise ConflictError(f"send {key} is {row['status']}, not reserved")
            c.execute("UPDATE sends SET status = 'failed', note = ? WHERE idempotency_key = ?", (reason, key))
            self.log_event("send_failed", row["draft_id"], {"key": key, "reason": reason}, at)

    def record_send(self, draft_id: str, hash: str, mode: str, at: dt.datetime | None = None) -> str:
        """Reserve and confirm in one step, for a send that cannot fail half-way (an .eml file). Returns the key."""
        with self._tx():
            key = self.reserve_send(draft_id, hash, mode, at)
            self.confirm_send(key, at)
            return key

    def reminders_sent(self) -> dict[str, int]:
        """Invoice number -> how many customer reminders about it really went out (sent, not just drafted)."""
        counts: dict[str, int] = {}
        rows = self._conn.execute(
            "SELECT d.invoice_numbers FROM sends s JOIN drafts d ON d.id = s.draft_id WHERE s.status = 'sent' AND d.kind IN ('stage07', 'stage14', 'stage21', 'stage28')"
        ).fetchall()
        for r in rows:
            for number in json.loads(r["invoice_numbers"]):
                counts[number] = counts.get(number, 0) + 1
        return counts

    def list_sends(self) -> list[SendRecord]:
        rows = self._conn.execute("SELECT * FROM sends ORDER BY reserved_at, rowid").fetchall()
        return [SendRecord(r["idempotency_key"], r["draft_id"], r["hash"], _dt(r["sent_at"]), r["mode"], r["status"], _dt(r["reserved_at"]), r["note"]) for r in rows]

    # --------------------------------------------------------- chase state

    def chase_state(self) -> ChaseState:
        state = ChaseState()
        for r in self._conn.execute("SELECT invoice_number, stage FROM chase_state").fetchall():
            if r["stage"] == "thanks":
                state.thanked.add(r["invoice_number"])
            else:
                state.drafted[r["invoice_number"]] = r["stage"]
        return state

    def save_chase_state(self, state: ChaseState, at: dt.datetime | None = None) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM chase_state")
            rows = [(n, s, _ts(at)) for n, s in sorted(state.drafted.items())] + [(k, "thanks", _ts(at)) for k in sorted(state.thanked)]
            c.executemany("INSERT INTO chase_state (invoice_number, stage, updated_at) VALUES (?, ?, ?)", rows)

    # -------------------------------------------------------------- events

    def log_event(self, kind: str, ref: str = "", detail: dict | None = None, at: dt.datetime | None = None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO events (at, kind, ref, detail) VALUES (?, ?, ?, ?)", (_ts(at), kind, ref, json.dumps(detail or {}, ensure_ascii=False, default=str)))

    def events(self, limit: int = 200) -> list[Event]:
        rows = self._conn.execute("SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,)).fetchall()
        return [Event(r["id"], _dt(r["at"]), r["kind"], r["ref"], json.loads(r["detail"])) for r in rows]

    # ----------------------------------------------------------- snapshots

    def record_brief(self, at: dt.datetime | None = None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO briefs (at) VALUES (?)", (_ts(at),))

    def previous_brief_at(self) -> dt.datetime | None:
        row = self._conn.execute("SELECT at FROM briefs ORDER BY at DESC, id DESC LIMIT 1").fetchone()
        return _dt(row["at"]) if row else None

    def status_snapshot(self, as_of: dt.date) -> StatusSnapshot:
        """Open balance by age bucket per currency, draft counts, holds and the last import. Read-only."""
        buckets: dict[str, dict[str, Decimal]] = {}
        open_count = 0
        for inv in self.list_invoices():
            if not inv.is_open:
                continue
            open_count += 1
            b = bucket_for(inv.days_overdue(as_of))
            per = buckets.setdefault(inv.currency, {})
            per[b] = per.get(b, Decimal("0.00")) + inv.balance
        counts = {r["status"]: r["n"] for r in self._conn.execute("SELECT status, COUNT(*) AS n FROM drafts GROUP BY status").fetchall()}
        disputed = {r["customer_id"] for r in self._conn.execute("SELECT DISTINCT customer_id FROM invoices WHERE status = 'disputed' AND CAST(balance AS REAL) > 0").fetchall()}
        held = {r["id"] for r in self._conn.execute("SELECT id FROM customers WHERE on_hold = 1").fetchall()}
        imports = self.list_imports()
        return StatusSnapshot(as_of, {cur: dict(sorted(b.items(), key=lambda kv: BUCKETS.index(kv[0]))) for cur, b in sorted(buckets.items())},
                              open_count, counts.get("pending", 0), counts.get("approved", 0), counts.get("held", 0), len(disputed | held),
                              imports[-1] if imports else None)

    def brief_snapshot(self, as_of: dt.date) -> BriefSnapshot:
        """Everything the morning brief reads: payments applied since the previous brief, drafts, customers, invoices."""
        since = self.previous_brief_at()
        customers = {c.id: c for c in self.list_customers()}
        invoices = {i.number: i for i in self.list_invoices()}
        payments: list[PaymentView] = []
        for r in self._conn.execute("SELECT * FROM matches ORDER BY applied_at, rowid").fetchall():
            applied_at = _dt(r["applied_at"])
            if since is not None and applied_at <= since:
                continue
            m = _match(r)
            inv = invoices.get(m.invoice_number)
            payments.append(PaymentView(m, applied_at, self.get_payment(m.payment_id), inv, customers.get(inv.customer_id) if inv else None))
        return BriefSnapshot(as_of, since, tuple(payments), tuple(self.list_drafts(status="pending")), tuple(self.list_drafts(status="held")), customers, invoices)


# ------------------------------------------------------------ row mapping


def _credit_invoices(credits: Iterable, at: dt.datetime | None) -> list[Invoice]:
    """Credit notes / credit memos / unapplied payments from an export, kept as negative open
    invoices: never chased, and an open one holds that customer's reminders until a person decides."""
    day = _now(at).date()
    out = []
    for cn in credits:
        number = cn.number or f"CREDIT-{cn.customer_id}-{cn.row}"
        when = cn.date or day
        value = -abs(Decimal(cn.amount))
        out.append(Invoice(number, cn.customer_id, when, when, value, cn.currency, value, "open",
                           note=f"{cn.kind.replace('_', ' ')} from the export; a person decides which invoice it settles"))
    return out


def _customer(r: sqlite3.Row) -> Customer:
    return Customer(r["id"], r["name"], r["language"], r["email"], r["contact_name"], bool(r["on_hold"]), r["hold_reason"])


def _invoice(r: sqlite3.Row) -> Invoice:
    return Invoice(r["number"], r["customer_id"], _d(r["issue_date"]), _d(r["due_date"]), Decimal(r["amount"]), r["currency"], Decimal(r["balance"]),
                   r["status"], r["reference"], r["iban"], _d(r["paid_date"]), r["note"])


def _payment(r: sqlite3.Row) -> Payment:
    return Payment(r["id"], _d(r["date"]), Decimal(r["amount"]), r["currency"], r["reference"], r["counterparty"], r["memo"], r["source"])


def _match(r: sqlite3.Row) -> Match:
    return Match(r["payment_id"], r["invoice_number"], Decimal(r["amount"]), r["currency"], r["method"], r["confidence"], r["reason"])


def _figures_json(figures: Iterable[Figure]) -> str:
    return json.dumps([{"kind": f.kind, "canonical": f.canonical, "raw": f.raw} for f in figures], ensure_ascii=False)


def _draft(r: sqlite3.Row) -> Draft:
    figures = tuple(Figure(f["kind"], f["canonical"], f.get("raw", "")) for f in json.loads(r["figures"]))
    return Draft(r["id"], r["kind"], r["customer_id"], tuple(json.loads(r["invoice_numbers"])), r["language"], r["subject"], r["body"],
                 tuple(json.loads(r["recipients"])), _dt(r["created_at"]), r["status"], r["hash"], figures)


__all__ = [
    "BUCKETS",
    "UPSERT_MODES",
    "StoreError",
    "DuplicateImportError",
    "HashMismatchError",
    "NotApprovedError",
    "AlreadySentError",
    "ConflictError",
    "NotFoundError",
    "ImportRecord",
    "Approval",
    "SendRecord",
    "Event",
    "PaymentView",
    "StatusSnapshot",
    "BriefSnapshot",
    "bucket_for",
    "Store",
]
