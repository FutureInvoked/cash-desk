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
import hashlib
import itertools
import json
import re
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal

from .approval import idempotency_key, verify_hash
from .chasing import ChaseState, _contact_problem, _customer_holds
from .figure_lock import binding_changed
from .importers import BankEvidenceIssue, ImportResult, customer_key
from .matching import MatchResult, ReviewItem, invoices_named, payer_may_fit, match_payments, is_return, reference_evidence, payer_legal_conflict, no_payer_statements
from .model import Customer, Draft, Figure, Invoice, Match, Payment, to_money
from .money import format_money

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
CREATE TABLE IF NOT EXISTS payment_identity (
    payment_id TEXT PRIMARY KEY, origin TEXT NOT NULL, fingerprint TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipt_pairs (
    id1 TEXT NOT NULL, id2 TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT NOT NULL DEFAULT '', at TEXT NOT NULL, PRIMARY KEY(id1,id2));
CREATE TABLE IF NOT EXISTS receipt_aliases (alias_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipt_placement (canonical_id TEXT PRIMARY KEY, confirmed INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customer_origins (customer_id TEXT PRIMARY KEY, explicit INTEGER NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customer_identity_observations (id TEXT PRIMARY KEY, number TEXT NOT NULL, source TEXT NOT NULL,
    fingerprint TEXT NOT NULL, observed_customer TEXT NOT NULL, bound_customer TEXT NOT NULL, origin TEXT NOT NULL,
    name TEXT NOT NULL, email TEXT NOT NULL, row_number INTEGER NOT NULL, raw TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_customer_basis (number TEXT PRIMARY KEY, observed_customer TEXT NOT NULL,
    bound_customer TEXT NOT NULL, origin TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customer_identity_reviews (number TEXT PRIMARY KEY, detail TEXT NOT NULL, scope TEXT NOT NULL,
    legacy_unknown INTEGER NOT NULL, at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, explanation TEXT);
CREATE TABLE IF NOT EXISTS bank_return_reviews (id TEXT PRIMARY KEY, evidence TEXT NOT NULL, initial_scope TEXT NOT NULL,
    at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS receipt_observations (id TEXT PRIMARY KEY, payment_id TEXT NOT NULL, raw_id TEXT NOT NULL,
    account TEXT NOT NULL, status TEXT NOT NULL, raw TEXT NOT NULL, fingerprint TEXT NOT NULL, file TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipt_conflicts (original_id TEXT NOT NULL, observation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
    reason TEXT NOT NULL, account TEXT NOT NULL, status TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending', reviewer TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL, PRIMARY KEY(original_id, observation_id, fingerprint));
CREATE TABLE IF NOT EXISTS bank_import_observations (fingerprint TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS receipt_heads (original_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipt_identity_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
CREATE TABLE IF NOT EXISTS draft_text_basis (draft_id TEXT PRIMARY KEY, subject TEXT NOT NULL, body TEXT NOT NULL, language TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draft_contact_basis (draft_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draft_basis (draft_id TEXT NOT NULL, invoice_number TEXT NOT NULL, balance TEXT NOT NULL, currency TEXT NOT NULL,
    due_date TEXT NOT NULL, reference TEXT, iban TEXT, customer_id TEXT, PRIMARY KEY (draft_id, invoice_number));
CREATE TABLE IF NOT EXISTS draft_invalidations (draft_id TEXT PRIMARY KEY, kind TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_surpluses (id TEXT PRIMARY KEY, number TEXT NOT NULL, customer_id TEXT NOT NULL,
    currency TEXT NOT NULL, amount TEXT NOT NULL, balance TEXT NOT NULL, source TEXT NOT NULL, fingerprint TEXT NOT NULL,
    raw TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, customer_name TEXT NOT NULL DEFAULT '', prior_customer_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS invoice_surplus_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_settlements (
    number TEXT PRIMARY KEY, external_paid TEXT, export_settled TEXT, reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_closures (number TEXT PRIMARY KEY, source TEXT NOT NULL, customer_id TEXT NOT NULL,
    issue_date TEXT NOT NULL, amount TEXT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, paid_date TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_sources (number TEXT PRIMARY KEY, source TEXT NOT NULL, file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credit_sources (
    number TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, source_customer TEXT NOT NULL,
    document_number TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_import_issues (id TEXT PRIMARY KEY, source TEXT NOT NULL, fingerprint TEXT NOT NULL,
    number TEXT NOT NULL, exported_customer TEXT NOT NULL, bound_customer TEXT NOT NULL, row_number INTEGER NOT NULL,
    raw TEXT NOT NULL, detail TEXT NOT NULL, file TEXT NOT NULL, at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT);
CREATE TABLE IF NOT EXISTS bank_currency_evidence (observation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    payment_id TEXT NOT NULL, currency TEXT NOT NULL, evidence TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_alias_issues (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, row_number INTEGER NOT NULL,
    raw TEXT NOT NULL, evidence TEXT NOT NULL, detail TEXT NOT NULL, file TEXT NOT NULL, at TEXT NOT NULL,
    resolved_at TEXT, resolved_by TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS missing_from_export (number TEXT PRIMARY KEY, file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoice_source_flags (number TEXT PRIMARY KEY, planned_date TEXT, not_sent INTEGER NOT NULL DEFAULT 0, file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_receipts (id TEXT PRIMARY KEY, evidence TEXT NOT NULL, file TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS draft_as_of (draft_id TEXT PRIMARY KEY, as_of TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS setting_iban (number TEXT PRIMARY KEY, iban TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_refusals (file TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS distinct_invoices (a TEXT NOT NULL, b TEXT NOT NULL, reason TEXT NOT NULL, by TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (a, b));
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
    paused: dict[str, str] = field(default_factory=dict)  # invoice number -> why reminders wait on a person
    review: tuple[Payment, ...] = ()  # receipts waiting for Apply or Dismiss


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

    def __init__(self, path: str | os.PathLike[str], *, today: Callable[[], dt.date] | None = None,
                 ignore_balances_below: Decimal | str = "0.00"):
        self.path = str(path)
        self._today = today or (lambda: dt.datetime.now(_UTC).date())
        self.ignore_balances_below = max(Decimal("0.00"), to_money(ignore_balances_below))
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0
        self._conn.executescript(SCHEMA)
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(draft_basis)")}
        for column in ("reference", "iban", "customer_id"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE draft_basis ADD COLUMN {column} TEXT")
        surplus_columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(invoice_surpluses)")}
        if "prior_customer_id" not in surplus_columns:
            self._conn.execute("ALTER TABLE invoice_surpluses ADD COLUMN prior_customer_id TEXT NOT NULL DEFAULT ''")
        issue_columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(invoice_import_issues)")}
        if "customer_origin" not in issue_columns:
            self._conn.execute("ALTER TABLE invoice_import_issues ADD COLUMN customer_origin TEXT NOT NULL DEFAULT ''")
        new_receipt_questions = 0
        with self._tx() as c:
            if not c.execute("SELECT 1 FROM receipt_identity_state WHERE key='legacy_scanned'").fetchone():
                new_receipt_questions = self._discover_overlaps()
                c.execute("INSERT INTO receipt_identity_state VALUES ('legacy_scanned', '1')")
        # A legacy net balance cannot reveal which settlements preceded local matches.
        # Keep all financial rows untouched and pause unknown allocation histories.
        migrated_settlements = False
        with self._tx() as c:
            for row in c.execute("SELECT i.* FROM invoices i LEFT JOIN invoice_settlements b ON b.number=i.number WHERE b.number IS NULL").fetchall():
                local = self._applied_total(c, row["number"])
                if Decimal(row["amount"]) < 0 or c.execute("SELECT 1 FROM credit_sources WHERE number=?", (row["number"],)).fetchone():
                    continue
                reason = self._settlement_reason("historical imported payments are unknown") if local is not None else ""
                external = None if local is not None else str(max(Decimal("0"), Decimal(row["amount"]) - Decimal(row["balance"])))
                c.execute("INSERT INTO invoice_settlements VALUES (?, ?, ?, ?, ?)",
                          (row["number"], external, external, reason, _ts(None)))
                if reason:
                    migrated_settlements = True
                    self.log_event("settlement_review", row["number"], {"reason": reason})
            if migrated_settlements:
                self.refresh_reminders()
        self._migrate_surpluses()
        self._migrate_returns()
        self._migrate_customer_identity()
        with self._tx():
            self._remember_reference_history()
            self._remember_payer_history()
            if self._bank_evidence_pending():self.refresh_reminders()
        if new_receipt_questions or self._legacy_credit_holds():
            self.refresh_reminders()
        if self._conn.execute("SELECT 1 FROM drafts d LEFT JOIN draft_contact_basis b ON b.draft_id=d.id WHERE d.status IN ('pending','approved','held') AND d.kind!='escalation' AND b.draft_id IS NULL LIMIT 1").fetchone():
            self.refresh_reminders()
        # Unknown historical instructions must never be filled from today's ledger:
        # that would silently bless a draft carrying yesterday's bank account.
        if self._conn.execute(
            "SELECT 1 FROM draft_basis b JOIN drafts d ON d.id = b.draft_id "
            "WHERE d.status IN ('pending', 'approved', 'held') "
            "AND (b.reference IS NULL OR b.iban IS NULL OR b.customer_id IS NULL) "
            "AND NOT EXISTS (SELECT 1 FROM draft_invalidations v WHERE v.draft_id = b.draft_id) LIMIT 1"
        ).fetchone():
            self.refresh_reminders()

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

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """Keep planning and its stored draft basis on one ledger snapshot."""
        with self._tx():
            yield self

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
        if result.kind == "customers" and any("customer contact evidence conflicts" in error.message for error in result.errors):
            raise ConflictError("customer contact evidence conflicts: the contacts file cannot be partially imported; verify recipient columns and re-export")
        if result.kind == "customers" and any("customer identity conflicts" in error.message for error in result.errors):
            raise ConflictError("customer identity conflicts: the contacts file cannot be partially imported; verify original IDs and re-export")
        if result.kind == "bank" and any(error.code == "bank_status_conflict" for error in result.errors):
            raise ConflictError("bank status evidence conflicts: the file cannot be partially imported; verify the bank and re-export consistent lifecycle fields")
        if result.errors and not allow_errors:
            raise ConflictError(f"the {result.kind} file has {len(result.errors)} row error(s); fix it or pass allow_errors=True")
        counts: dict[str, int] = {}
        with self._tx():
            legacy_bank_replay = (result.kind == "bank" and self.has_import(result.fingerprint) and
                                  (not self._conn.execute("SELECT 1 FROM bank_import_observations WHERE fingerprint=?", (result.fingerprint,)).fetchone()
                                   or ((result.bank_alias_issues or result.bank_currency_issues) and not self._conn.execute(
                                       "SELECT 1 FROM bank_alias_issues WHERE fingerprint=?",(result.fingerprint,)).fetchone())
                                   or (any((item.evidence or {}).get("currency_basis") for item in result.bank_observations)
                                       and not self._conn.execute("SELECT 1 FROM bank_currency_evidence WHERE fingerprint=?",(result.fingerprint,)).fetchone())))
            if legacy_bank_replay:
                self.log_event("legacy_bank_reobserved", result.fingerprint, {"file": file_name, "financial_rows_retained": True}, at)
            else:
                self.record_import(result, at, file_name)
            if result.kind == "customers":
                remap = self._ingest_customer_identity(result, file_name, at)
                values = [replace(cu, id=remap.get(cu.id,cu.id)) for cu in result.records]
                counts["customers"] = self.upsert_customers(values, at, identity_origin="observed", contact_fields=result.customer_contact_fields)
            elif result.kind == "invoices":
                records = list(result.records)
                credits = list(getattr(result, "credit_notes", ()) or ())
                updates = list(getattr(result, "credit_updates", ()) or ())
                derived = list(result.customers)
                remap = self._ingest_customer_identity(result, file_name, at)
                records = [replace(inv, customer_id=remap.get(inv.customer_id, inv.customer_id)) for inv in records]
                derived = [customer for customer in derived if customer.id not in remap]
                if derived:
                    counts["customers"] = self.upsert_customers(derived, at, mode="fill", identity_origin="observed")
                # A credit shares the invoice table for holds, but can never replace a sale.
                for inv in records:
                    if inv.amount >= 0 and (inv.balance > inv.amount or (inv.status == "paid" and inv.balance > 0)):
                        raise ConflictError("invoice monetary evidence conflicts: remaining exceeds gross or paid status retains positive debt; verify the source export")
                    prior = self._conn.execute("SELECT * FROM invoices WHERE number=?", (inv.number,)).fetchone()
                    if (self._conn.execute("SELECT 1 FROM credit_sources WHERE number=?", (inv.number,)).fetchone()
                            or (prior and Decimal(prior["amount"]) < 0)):
                        self._credit_conflict(f"invoice {inv.number} would replace an existing credit")
                closures = [replace(inv, customer_id=remap.get(inv.customer_id, inv.customer_id))
                            for inv in getattr(result, "invoice_closures", ())]
                uncertain = {n for n in self.customer_identity_holds()}
                credits = [cn for cn in credits if not (cn.number in uncertain and self._conn.execute("SELECT 1 FROM invoices WHERE number=?",(cn.number,)).fetchone())]
                updates = [cn for cn in updates if not (cn.number in uncertain and self._conn.execute("SELECT 1 FROM invoices WHERE number=?",(cn.number,)).fetchone())]
                records = [self.get_invoice(inv.number) if inv.number in uncertain and self._conn.execute("SELECT 1 FROM invoices WHERE number=?",(inv.number,)).fetchone() else inv for inv in records]
                closures = [inv for inv in closures if inv.number not in uncertain]
                records, closed, unmatched = self._invoice_closure_records(records, closures, result.source, at)
                for observed in getattr(result, "invoice_surpluses", ()):
                    self._record_surplus(observed.number, remap.get(observed.customer_id, observed.customer_id), observed.currency,
                                         observed.amount, observed.balance, result.source, result.fingerprint, observed.raw, "csv", at)
                counts["invoices"] = self.upsert_invoices(records, at)
                counts["surplus_questions"] = len(self.surplus_reviews())
                if closures:
                    counts["invoice_closures"] = closed
                    counts["unmatched_invoice_closures"] = unmatched
                if credits or updates:
                    written = self._ingest_credits(credits + updates, result.source, remap, result.fingerprint, at)
                    if credits:
                        counts["credit_notes"] = written["open"]
                    if updates:
                        counts["credit_updates"] = written["closed"]
                c = self._conn
                c.executemany("INSERT OR REPLACE INTO invoice_sources (number, source, file, at) VALUES (?, ?, ?, ?)",
                              [(inv.number, result.source, file_name, _ts(at)) for inv in records])
                recovered_credits=[self.get_invoice(cn.number) for cn in credits+updates if cn.number and self._conn.execute("SELECT 1 FROM invoices WHERE number=?",(cn.number,)).fetchone()]
                identity_holds=self.customer_identity_holds()
                eligible_recovery=[inv for inv in [*records,*recovered_credits] if inv.number not in identity_holds]
                self._ingest_invoice_issues(result, eligible_recovery, file_name, at)
                counts["invoice_import_questions"] = len(self.invoice_import_questions())
                c.executemany("DELETE FROM missing_from_export WHERE number = ?", [(inv.number,) for inv in records])
                planned = dict(getattr(result, "invoice_planned", ()) or ())
                not_sent = set(getattr(result, "invoice_not_sent", ()) or ())
                c.executemany("DELETE FROM invoice_source_flags WHERE number = ?", [(inv.number,) for inv in records])
                c.executemany("INSERT INTO invoice_source_flags (number, planned_date, not_sent, file, at) VALUES (?, ?, ?, ?, ?)",
                              [(inv.number, planned[inv.number].isoformat() if inv.number in planned else None, int(inv.number in not_sent), file_name, _ts(at))
                               for inv in records if inv.number in planned or inv.number in not_sent])
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
            elif result.kind == "bank":
                for issue in (*result.bank_alias_issues,*result.bank_currency_issues):
                    self._retain_bank_evidence(issue,result.fingerprint,file_name,at)
                self._conn.execute("INSERT OR IGNORE INTO bank_import_observations VALUES (?)",(result.fingerprint,))
                if getattr(result, "bank_observations", ()):
                    counts["payments"] = self._ingest_bank_observations(result, file_name, at)
                else:
                    observations = [replace(p, id=f"{p.id}@{result.fingerprint[:16]}") if p.identity_origin == "derived" else p
                                    for p in result.records]
                    counts["payments"] = self.add_payments(observations, at, fingerprint=result.fingerprint, file_name=file_name)
                if self.receipt_conflicts():
                    counts["receipt_update_questions"] = len(self.receipt_conflicts())
                if self.return_reviews():
                    counts["return_questions"] = len(self.return_reviews())
                if self._bank_evidence_pending():
                    counts["bank_evidence_questions"] = len(self.bank_evidence_questions())
                questions = len(self.payment_overlaps())
                if questions:
                    counts["receipt_overlap_questions"] = questions
            else:
                raise ConflictError(f"unknown import kind {result.kind!r}")
            if result.kind == "invoices":
                counts["settlement_review"] = len(self.settlement_reviews())
            if result.kind=="invoices":
                for row in getattr(result,"customer_identities",()):
                    saved=self._conn.execute("SELECT customer_id FROM invoices WHERE number=?",(row.number,)).fetchone()
                    if saved:self._conn.execute("UPDATE invoice_customer_basis SET bound_customer=? WHERE number=?",(saved["customer_id"],row.number))
            if self.customer_identity_reviews():
                counts["customer_identity_questions"] = len(self.customer_identity_reviews())
            self.refresh_reminders(at=at)
        return counts

    @staticmethod
    def _terminal_conflict(detail: str):
        raise ConflictError("terminal invoice update conflicts: " + detail + "; check the original source and document identity")

    def _invoice_closure_records(self, records, closures, source, at):
        """Preserve terminal observations without inventing dates or trusting a number alone."""
        c = self._conn
        closed = unmatched = 0
        numbers = {inv.number for inv in records}
        for observed in closures:
            if observed.number in numbers:
                self._terminal_conflict(f"duplicate document {observed.number}")
            numbers.add(observed.number)
            prior = c.execute("SELECT * FROM invoices WHERE number=?", (observed.number,)).fetchone()
            origin = c.execute("SELECT source FROM invoice_sources WHERE number=?", (observed.number,)).fetchone()
            saved = c.execute("SELECT * FROM invoice_closures WHERE number=?", (observed.number,)).fetchone()
            if c.execute("SELECT 1 FROM credit_sources WHERE number=?", (observed.number,)).fetchone():
                self._terminal_conflict(f"{observed.number} identifies a credit, not a sale")
            if prior and (origin is None or origin["source"] != source):
                self._terminal_conflict(f"{observed.number} has a different or unknown import source")
            for basis in (prior, saved):
                if basis is not None and (basis["customer_id"] != observed.customer_id or
                        basis["currency"] != observed.currency or Decimal(basis["amount"]) != observed.amount or
                        basis["issue_date"] != observed.issue_date.isoformat()):
                    self._terminal_conflict(f"{observed.number} customer, currency, gross amount or issue date differs")
            if saved and saved["source"] != source:
                self._terminal_conflict(f"{observed.number} has a different closure source")
            status = "void" if (observed.status == "void" or (saved and saved["status"] == "void")
                                  or (prior and prior["status"] == "void")) else "paid"
            paid = observed.paid_date.isoformat() if observed.paid_date else (saved["paid_date"] if saved else None)
            c.execute("INSERT OR REPLACE INTO invoice_closures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (observed.number, source, observed.customer_id, observed.issue_date.isoformat(),
                       str(observed.amount), observed.currency, status, paid, _ts(at)))
            if prior:
                records.append(replace(_invoice(prior), balance=Decimal("0.00"), status=status,
                                       paid_date=_d(paid) or _d(prior["paid_date"])))
                closed += 1
            else:
                unmatched += 1
            self.log_event("invoice_closure", observed.number, {"source": source, "status": status,
                           "matched": bool(prior), "due_date": prior["due_date"] if prior else None}, at)
        out = []
        for inv in records:
            basis = c.execute("SELECT * FROM invoice_closures WHERE number=?", (inv.number,)).fetchone()
            if basis:
                if (basis["source"] != source or basis["customer_id"] != inv.customer_id or basis["currency"] != inv.currency
                        or Decimal(basis["amount"]) != inv.amount or basis["issue_date"] != inv.issue_date.isoformat()):
                    self._terminal_conflict(f"{inv.number} conflicts with a retained terminal observation")
                inv = replace(inv, balance=Decimal("0.00"), status=basis["status"], paid_date=_d(basis["paid_date"]) or inv.paid_date)
            out.append(inv)
        return out, closed, unmatched

    @staticmethod
    def _credit_conflict(detail: str):
        raise ConflictError("credit identity conflict: " + detail + "; check the accounting export before re-importing")

    def _ingest_credits(self, observations: Iterable, source: str, remap: dict[str, str],
                        fingerprint: str, at: dt.datetime | None) -> dict[str, int]:
        """Update a credit only with compatible document identity, inside ingest's transaction."""
        c = self._conn
        seen = {}
        written = {"open": 0, "closed": 0}
        for observed in observations:
            if c.execute("SELECT 1 FROM invoice_closures WHERE number=?", (observed.number,)).fetchone():
                self._credit_conflict(f"credit {observed.number} would replace a terminal sale observation")
            original_customer = observed.customer_id
            cn = replace(observed, customer_id=remap.get(original_customer, original_customer))
            if not cn.number:
                # A row position is not an identity across exports. Anonymous observations
                # remain separate holds and cannot close or overwrite earlier observations.
                identity = f"{source}|{fingerprint}|{cn.row}|{original_customer}|{cn.kind}"
                number = "CREDIT-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
                if cn.status in ("paid", "void"):
                    self._credit_conflict("credit closure needs a stable document number")
            else:
                number = cn.number
            signature = (cn.customer_id, cn.date, cn.amount, cn.currency, cn.kind, cn.status, cn.original_amount)
            if number in seen:
                if seen[number] != signature:
                    self._credit_conflict(f"credit {number} has conflicting rows")
                continue
            seen[number] = signature
            prior = c.execute("SELECT * FROM invoices WHERE number=?", (number,)).fetchone()
            identity = c.execute("SELECT * FROM credit_sources WHERE number=?", (number,)).fetchone()
            if self._applied_total(c, number) is not None:
                self._credit_conflict(f"credit {number} has receipt allocations; verify its history")
            if identity and (identity["source"] != source or identity["kind"] != cn.kind or
                             identity["source_customer"] != original_customer):
                self._credit_conflict(f"credit {number} source, kind or exported customer changed")
            when = cn.date or (_d(prior["issue_date"]) if prior else _now(at).date())
            if prior:
                recognizable = prior["note"] == f"{cn.kind.replace('_', ' ')} from the export; a person decides which invoice it settles"
                if not identity and (Decimal(prior["amount"]) >= 0 or not recognizable):
                    self._credit_conflict(f"credit {number} would replace a document without verified credit identity")
                if (prior["currency"] != cn.currency or prior["customer_id"] not in {original_customer, cn.customer_id}
                        or prior["issue_date"] != when.isoformat()):
                    self._credit_conflict(f"credit {number} customer, currency or issue date changed")
                if not identity:
                    self.log_event("credit_identity_adopted", number, {"source": source, "kind": cn.kind,
                                   "previous_customer": prior["customer_id"], "customer": cn.customer_id}, at)
            if cn.status not in ("open", "disputed", "paid", "void"):
                self._credit_conflict(f"credit {number} has an unknown state")
            gross = -abs(cn.original_amount if cn.original_amount is not None else cn.amount)
            if gross == 0 and prior:
                gross = Decimal(prior["amount"])
            balance = -abs(cn.amount) if cn.status in ("open", "disputed") else Decimal("0.00")
            status = cn.status
            # An old open export is not evidence that a closed credit is available again.
            if prior and prior["status"] in ("paid", "void") and status in ("open", "disputed"):
                self._credit_conflict(f"credit {number} was closed; an open export cannot reopen it without verification")
            note = f"{cn.kind.replace('_', ' ')} from the export; a person decides which invoice it settles"
            inv = Invoice(number, cn.customer_id, when, when, gross, cn.currency, balance, status, note=note)
            c.execute("INSERT INTO credit_sources VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(number) DO UPDATE SET at=excluded.at",
                      (number, source, cn.kind, original_customer, cn.number, _ts(at)))
            self.upsert_invoices([inv], at)
            proof=c.execute("SELECT origin FROM customer_identity_observations WHERE source=? AND fingerprint=? AND row_number=? AND observed_customer=? ORDER BY rowid DESC LIMIT 1",(source,fingerprint,cn.row,original_customer)).fetchone()
            if proof:
                c.execute("INSERT OR IGNORE INTO invoice_customer_basis VALUES (?,?,?,?,?,?)",(number,original_customer,cn.customer_id,proof["origin"],source,fingerprint))
                c.execute("UPDATE invoice_customer_basis SET bound_customer=? WHERE number=?",(cn.customer_id,number))
            self.log_event("credit_updated", number,
                           {"source": source, "kind": cn.kind, "customer": cn.customer_id,
                            "previous_customer": prior["customer_id"] if prior else None,
                            "previous_balance": prior["balance"] if prior else None,
                            "balance": str(balance), "status": status, "amount": str(gross),
                            "previous_amount": prior["amount"] if prior else None,
                            "currency": cn.currency, "anonymous": not bool(cn.number)}, at)
            written["closed" if status in ("paid", "void") else "open"] += 1
        return written

    def _record_customer_origin(self, customer_id, origin, at=None):
        explicit = int(origin == "explicit")
        self._conn.execute("INSERT INTO customer_origins VALUES (?,?,?) ON CONFLICT(customer_id) DO UPDATE SET explicit=MAX(explicit,excluded.explicit),at=excluded.at",(customer_id,explicit,_ts(at)))

    def _identity_question(self, number, owners, detail, *, legacy=False, at=None):
        old = self._conn.execute("SELECT scope FROM customer_identity_reviews WHERE number=?",(number,)).fetchone()
        owners = set(owners) | (set(json.loads(old["scope"])) if old else set())
        self._conn.execute("INSERT INTO customer_identity_reviews VALUES (?,?,?,?,?,NULL,NULL,NULL) ON CONFLICT(number) DO UPDATE SET detail=excluded.detail,scope=excluded.scope,legacy_unknown=MAX(legacy_unknown,excluded.legacy_unknown),resolved_at=NULL,resolved_by=NULL,explanation=NULL",
                           (number,detail,json.dumps(sorted(owners)),int(legacy),_ts(at)))
        self.log_event("customer_identity_question",number,{"owners":sorted(owners),"detail":detail,"legacy_unknown":legacy},at)

    def _ingest_customer_identity(self, result, file_name, at):
        remap = {}
        existing = {c.id:c for c in self.list_customers()}
        explicit = {r["customer_id"] for r in self._conn.execute("SELECT * FROM customer_origins WHERE explicit=1")}
        observations = list(getattr(result,"customer_identities",()))
        # Programmatic legacy ImportResult values carry no origin proof. Preserve
        # their IDs; never infer derived origin by comparing an ID to a slug.
        if not observations and result.customers:
            from .importers import CustomerIdentity
            observations = [CustomerIdentity(i.number,i.customer_id,"unknown",next((c.name for c in result.customers if c.id==i.customer_id),""),"",0,repr(i)) for i in result.records]
        # Decide the whole file before writing bindings. Later rows cannot
        # leave an earlier name-based remap in force after conflicting evidence.
        file_explicit={o.customer_id for o in observations if o.origin=="explicit"}
        for cu in result.customers:
            if cu.id in file_explicit:
                existing.setdefault(cu.id,cu)
        explicit |= file_explicit
        decisions={}
        for customer_id in {o.customer_id for o in observations}:
            items=[o for o in observations if o.customer_id==customer_id]
            item=items[0];bound=customer_id;detail=""
            matches=[c for c in existing.values() if item.name and " ".join(c.name.split()).casefold()==" ".join(item.name.split()).casefold()]
            candidates=[c for c in matches if c.id in explicit]
            if any(o.origin=="derived" for o in items):
                unknown=[c for c in matches if c.id!=customer_id and c.id not in explicit and not self._conn.execute("SELECT 1 FROM customer_origins WHERE customer_id=?",(c.id,)).fetchone()]
                emails={o.email.casefold() for o in items if o.email}
                if len(emails)>1 or any(o.origin=="explicit" for o in items):
                    detail="derived identity has conflicting explicit/contact evidence in this file; verify the original customer ID"
                elif len(candidates)==1 and not unknown:
                    candidate=candidates[0]
                    if emails and candidate.email and candidate.email.casefold() not in emails:
                        detail="name-derived contact has conflicting email evidence; verify the original customer ID"
                    else:bound=candidate.id
                elif len(candidates)>1 or unknown:
                    detail="name-derived contact matches several or historically unverified customer IDs; verify the original customer ID"
                same=existing.get(bound)
                if same and emails and same.email and same.email.casefold() not in emails:
                    detail="name-derived contact has conflicting email evidence; verify the original customer ID"
            decisions[customer_id]=(bound,detail,matches)
            if bound!=customer_id:remap[customer_id]=bound
        for item in observations:
            bound,detail,matches=decisions[item.customer_id]
            self._record_customer_origin(bound,"explicit" if bound in explicit else item.origin,at)
            prior=self._conn.execute("SELECT customer_id FROM invoices WHERE number=?",(item.number,)).fetchone()
            if prior and prior["customer_id"]!=bound:
                basis=self._conn.execute("SELECT * FROM invoice_customer_basis WHERE number=?",(item.number,)).fetchone()
                derived_adoption=(item.origin=="derived" and basis and basis["origin"]=="derived"
                                  and basis["observed_customer"]==item.customer_id and prior["customer_id"]==item.customer_id
                                  and not detail and self._applied_total(self._conn,item.number) is None
                                  and not any(item.number in d.invoice_numbers for d in self.list_drafts(status="sent")))
                if not derived_adoption:
                    detail="imported customer ID differs from the stored invoice owner; verify the original invoice before reassignment"
            if detail:
                owners={item.customer_id,bound}|{c.id for c in matches}|({prior["customer_id"]} if prior else set())
                self._identity_question(item.number or "@contact:"+item.customer_id,owners,detail,at=at)
            key=hashlib.sha256(json.dumps([result.source,result.fingerprint,item.row,item.raw,item.customer_id,item.origin],ensure_ascii=False).encode()).hexdigest()
            self._conn.execute("INSERT OR IGNORE INTO customer_identity_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(key,item.number,result.source,result.fingerprint,item.customer_id,bound,item.origin,item.name,item.email,item.row,item.raw,_ts(at)))
            if item.number:
                self._conn.execute("INSERT OR IGNORE INTO invoice_customer_basis VALUES (?,?,?,?,?,?)",(item.number,item.customer_id,bound,item.origin,result.source,result.fingerprint))
            self.log_event("customer_identity_observed",item.number or item.customer_id,{"source":result.source,"fingerprint":result.fingerprint,"file":file_name,"observed":item.customer_id,"bound":bound,"origin":item.origin},at)
        return remap

    def _migrate_customer_identity(self):
        with self._tx() as c:
            rows=c.execute("SELECT i.*,s.source,'' AS source_customer FROM invoices i JOIN invoice_sources s ON s.number=i.number LEFT JOIN invoice_customer_basis b ON b.number=i.number WHERE b.number IS NULL AND s.source IN ('stripe','xero','quickbooks') UNION SELECT i.*,s.source,s.source_customer FROM invoices i JOIN credit_sources s ON s.number=i.number LEFT JOIN invoice_customer_basis b ON b.number=i.number WHERE b.number IS NULL AND s.source IN ('stripe','xero','quickbooks')").fetchall()
            for row in rows:
                # Old source_customer may survive for credits; it is a hint,
                # without proof whether it was derived or explicitly exported.
                owners={row["customer_id"]}|({row["source_customer"]} if row["source_customer"] else set())
                self._identity_question(row["number"],owners,"historical source customer identity was not retained; verify the original accounting export",legacy=True)
                raw=json.dumps(dict(row),sort_keys=True)
                key=hashlib.sha256(("legacy:"+row["source"]+":"+row["number"]).encode()).hexdigest()
                c.execute("INSERT OR IGNORE INTO customer_identity_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(key,row["number"],row["source"],"legacy",row["source_customer"],row["customer_id"],"unknown","","",0,raw,_ts(None)))
                c.execute("INSERT OR IGNORE INTO invoice_customer_basis VALUES (?,?,?,?,?,?)",(row["number"],row["source_customer"],row["customer_id"],"unknown",row["source"],""))
            if rows:self.refresh_reminders()

    def customer_identity_observations(self):
        return [dict(r) for r in self._conn.execute("SELECT * FROM customer_identity_observations ORDER BY at,rowid")]

    def customer_identity_snapshot(self):
        tables=("customer_identity_observations","customer_identity_reviews","invoice_customer_basis","customer_origins","invoices","invoice_settlements","invoice_sources","customers","matches","drafts","approvals","sends","events")
        value={t:[dict(r) for r in self._conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

    def customer_identity_reviews(self, *, include_snapshot=True):
        questions=[]
        invoices=self.list_invoices()
        for row in self._conn.execute("SELECT * FROM customer_identity_reviews WHERE resolved_at IS NULL ORDER BY at,number"):
            current=next((i for i in invoices if i.number==row["number"]),None)
            observations=[dict(r) for r in self._conn.execute("SELECT * FROM customer_identity_observations WHERE number=? ORDER BY at,rowid",(row["number"],))]
            owners=set(json.loads(row["scope"]))|({current.customer_id} if current else set())
            owners.update(o[k] for o in observations for k in ("observed_customer","bound_customer") if o[k])
            known={c.id:c for c in self.list_customers()}
            labels={" ".join(known[c].name.split()).casefold() for c in owners if c in known}
            owners.update(c.id for c in known.values() if " ".join(c.name.split()).casefold() in labels)
            if row["legacy_unknown"]:
                # Lost origin cannot prove which customer an old name remap
                # belonged to. Current names may have changed since import.
                owners.update(known)
                owners.update(i.customer_id for i in invoices)
            questions.append({"number":row["number"],"detail":row["detail"],"legacy_unknown":bool(row["legacy_unknown"]),
                              "scope":"unknown legacy owner: whole ledger" if row["legacy_unknown"] else "plausible customer identities",
                              "customer_ids":sorted(owners),"affected_invoices":sorted(i.number for i in invoices if i.number==row["number"] or i.customer_id in owners),
                              "current":dict(self._conn.execute("SELECT * FROM invoices WHERE number=?",(row["number"],)).fetchone()) if current else None,
                              "observations":observations,"allocations":[dict(r) for r in self._conn.execute("SELECT * FROM matches WHERE invoice_number=?",(row["number"],))]})
        if questions and include_snapshot:
            token=self.customer_identity_snapshot()
            for q in questions:q["snapshot"]=token
        return questions

    def _remember_customer_identity_scope(self, at=None):
        for q in self.customer_identity_reviews(include_snapshot=False):
            saved=self._conn.execute("SELECT scope FROM customer_identity_reviews WHERE number=?",(q["number"],)).fetchone()
            if set(json.loads(saved["scope"]))!=set(q["customer_ids"]):
                self._conn.execute("UPDATE customer_identity_reviews SET scope=? WHERE number=?",(json.dumps(q["customer_ids"]),q["number"]))
                self.log_event("customer_identity_scope_extended",q["number"],{"owners":q["customer_ids"]},at)

    def customer_identity_holds(self):
        return {number:"customer_identity: customer identity review; verify /cashdesk identities before collection or acknowledgement"
                for q in self.customer_identity_reviews(include_snapshot=False) for number in q["affected_invoices"]}

    def confirm_customer_identity(self, number, *, customer_id, expected_snapshot, reason, by="", at=None):
        with self._tx() as c:
            q=next((q for q in self.customer_identity_reviews(include_snapshot=False) if q["number"]==number),None)
            if not q or not reason.strip() or not by.strip() or not expected_snapshot or expected_snapshot!=self.customer_identity_snapshot():
                raise ConflictError("customer identity review: snapshot changed or verified accounting explanation missing; reread /cashdesk identities")
            current=self.get_invoice(number) if q["current"] else None
            if customer_id not in q["customer_ids"] or not c.execute("SELECT 1 FROM customers WHERE id=?",(customer_id,)).fetchone():
                raise ConflictError("customer identity review: verify a retained customer ID; no new identity can be guessed")
            if current and customer_id!=current.customer_id:
                explicit={o["observed_customer"] for o in q["observations"] if o["origin"]=="explicit"}
                sent=any(number in d.invoice_numbers for d in self.list_drafts(status="sent")) or any(number in self.get_draft(r["draft_id"]).invoice_numbers for r in c.execute("SELECT draft_id FROM sends"))
                settlement=c.execute("SELECT external_paid FROM invoice_settlements WHERE number=?",(number,)).fetchone()
                if (customer_id not in explicit or q["allocations"] or sent or current.amount<0 or current.status in ("paid","void")
                        or current.balance!=current.amount or not settlement or settlement["external_paid"] is None or Decimal(settlement["external_paid"])!=0):
                    raise ConflictError("customer identity review: settled, allocated, credit or sent history needs audited correction; ownership is unchanged")
                c.execute("UPDATE invoices SET customer_id=?,updated_at=? WHERE number=?",(customer_id,_ts(at),number))
            elif not current and any(i.customer_id!=customer_id for i in self.list_invoices() if i.number in q["affected_invoices"]):
                raise ConflictError("customer identity review: verify each affected invoice separately; contact verification cannot transfer debt")
            c.execute("UPDATE customer_identity_reviews SET resolved_at=?,resolved_by=?,explanation=? WHERE number=?",(_ts(at),by,reason,number))
            c.execute("UPDATE invoice_customer_basis SET bound_customer=? WHERE number=?",(customer_id,number))
            self.log_event("customer_identity_verified",number,{"by":by,"customer_id":customer_id,"prior_customer":current.customer_id if current else "","snapshot":expected_snapshot,"explanation":reason,"allocation_and_sent_history_unchanged":True},at)
            self.refresh_reminders(at=at)

    def _bank_evidence_pending(self):
        return bool(self._conn.execute("SELECT 1 FROM bank_alias_issues WHERE resolved_at IS NULL LIMIT 1").fetchone())

    def _bank_evidence_reason(self):
        details = [row["detail"] for row in self._conn.execute("SELECT detail FROM bank_alias_issues WHERE resolved_at IS NULL")]
        fragments = [fragment for fragment in ("bank payer/reference evidence conflicts","bank currency evidence conflicts","reference evidence needs review","payer legal identity needs review") if any(fragment in detail for detail in details)]
        return "; ".join(fragments or ["bank evidence verification required"]) + ": verify /cashdesk bank-evidence; all allocation, collection and acknowledgements are paused"

    def _retain_bank_evidence(self, issue, fingerprint, file_name, at):
        identity = hashlib.sha256(json.dumps([fingerprint,issue.row,issue.raw,issue.evidence]).encode()).hexdigest()
        inserted = self._conn.execute("INSERT OR IGNORE INTO bank_alias_issues (id,fingerprint,row_number,raw,evidence,detail,file,at) VALUES (?,?,?,?,?,?,?,?)",
                           (identity,fingerprint,issue.row,issue.raw,issue.evidence,issue.detail,file_name,_ts(at)))
        if inserted.rowcount:
            self.log_event("bank_evidence_retained",identity,{"file":file_name,"row":issue.row,"financial_rows_unchanged":True},at)

    def _remember_reference_history(self, at=None):
        """Quarantine retained automatic allocations that lack current reference proof.

        Same evidence has a stable question identity; a person's verification is
        retained until material receipt/allocation/reference interpretation changes.
        No balance, match or send row is rewritten or inferred from the text.
        """
        invoices=self.list_invoices()
        payments={payment.id:payment for payment in self.list_payments()}
        all_matches=[dict(row) for row in self._conn.execute("SELECT * FROM matches ORDER BY payment_id,invoice_number")]
        signature=hashlib.sha256(json.dumps(["reference-proof-v1",[repr(payments[key]) for key in sorted(payments)],
                                            sorted((inv.number,inv.reference) for inv in invoices),all_matches],sort_keys=True).encode()).hexdigest()
        cached=self._conn.execute("SELECT value FROM receipt_identity_state WHERE key='reference-proof-v1'").fetchone()
        if cached and cached["value"]==signature:return
        self._conn.execute("INSERT OR REPLACE INTO receipt_identity_state VALUES ('reference-proof-v1',?)",(signature,))
        for payment_id in sorted({row["payment_id"] for row in all_matches if row["method"]=='reference+exact'}):
            payment=payments.get(payment_id)
            if not payment:continue
            evidence=reference_evidence(payment,invoices)
            matches=[row for row in all_matches if row["payment_id"]==payment.id]
            if not evidence.problem and len(evidence.named)==1 and {m["invoice_number"] for m in matches}==set(evidence.named):
                continue
            payload={"kind":"reference","payment_id":payment.id,"payment":repr(payment),"payers":[payment.counterparty],"references":[payment.reference,payment.memo],
                     "named":sorted(evidence.named),"unknown_references":list(evidence.unknown),"conflicts":sorted(evidence.conflicts),"allocations":matches,"policy":"reference-proof-v1"}
            detail="reference evidence needs review: retained automatic allocation lacks a complete, unambiguous invoice reference; verify original remittance and allocated history; no automatic undo"
            self._retain_bank_evidence(BankEvidenceIssue(0,payment.text,detail,json.dumps(payload,ensure_ascii=False,sort_keys=True)),"reference-proof-v1","retained receipt "+payment.id,at)

    def _remember_payer_history(self, at=None):
        """Retain contradictory explicit entity evidence; never undo old cash."""
        payments={p.id:p for p in self.list_payments()}
        invoices={i.number:i for i in self.list_invoices()}
        customers={c.id:c for c in self.list_customers()}
        matches=[dict(row) for row in self._conn.execute("SELECT * FROM matches WHERE method='reference+exact' ORDER BY payment_id,invoice_number")]
        basis=["payer-legal-v1",[repr(payments[k]) for k in sorted(payments)],sorted((i.number,i.customer_id) for i in invoices.values()),sorted((c.id,c.name) for c in customers.values()),matches]
        signature=hashlib.sha256(json.dumps(basis,sort_keys=True).encode()).hexdigest()
        cached=self._conn.execute("SELECT value FROM receipt_identity_state WHERE key='payer-legal-v1'").fetchone()
        if cached and cached["value"]==signature:return
        self._conn.execute("INSERT OR REPLACE INTO receipt_identity_state VALUES ('payer-legal-v1',?)",(signature,))
        for pid in sorted({m["payment_id"] for m in matches}):
            payment=payments.get(pid)
            if payment is None:continue  # Missing legacy ownership remains a separate question.
            allocations=[m for m in matches if m["payment_id"]==pid]
            owners=[customers[invoices[m["invoice_number"]].customer_id] for m in allocations if m["invoice_number"] in invoices and invoices[m["invoice_number"]].customer_id in customers]
            if not any(payer_legal_conflict(c.name,payment.counterparty) for c in owners):continue
            payload={"kind":"payer","payment_id":pid,"payment":repr(payment),"payers":[payment.counterparty],"references":[payment.reference,payment.memo],"customer_names":sorted({c.name for c in owners}),"customer_ids":sorted({c.id for c in owners}),"allocations":allocations,"policy":"payer-legal-v1"}
            detail="payer legal identity needs review: retained automatic allocation has incompatible explicit legal forms; verify bank entity and allocated history; no automatic undo"
            self._retain_bank_evidence(BankEvidenceIssue(0,payment.text,detail,json.dumps(payload,ensure_ascii=False,sort_keys=True)),"payer-legal-v1","retained receipt "+pid,at)

    def bank_evidence_snapshot(self):
        # A verification describes the complete current ledger, not just one row.
        tables = [row["name"] for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        evidence = {table:[dict(row) for row in self._conn.execute('SELECT * FROM "' + table.replace('"','""') + '" ORDER BY rowid')] for table in tables}
        return hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()

    def bank_evidence_questions(self):
        rows = [dict(row) for row in self._conn.execute("SELECT * FROM bank_alias_issues WHERE resolved_at IS NULL ORDER BY at,id")]
        if not rows:
            return rows
        snapshot = self.bank_evidence_snapshot()
        invoices, customers = self.list_invoices(), self.list_customers()
        from .matching import payer_may_fit
        for row in rows:
            evidence = json.loads(row["evidence"])
            if evidence.get("kind") in ("reference","payer"):
                ids=self._receipt_cohort_ids({evidence["payment_id"]})
                evidence["bank_observations"]=[dict(item) for item in self._conn.execute("SELECT * FROM receipt_observations ORDER BY id") if item["payment_id"] in ids]
                evidence["currency_evidence"]=self.receipt_currency_evidence(evidence["payment_id"])
            row.update(snapshot=snapshot, evidence=evidence, scope="whole ledger",
                       named=sorted({n for ref in evidence["references"] for n in invoices_named(ref,invoices)}),
                       customer_ids=sorted({cu.id for cu in customers for payer in evidence["payers"] if payer_may_fit(cu.name,payer)}),
                       affected_invoices=[inv.number for inv in invoices])
            if evidence.get("kind")=="reference":
                row["named"]=sorted(set(row["named"])|set(evidence["named"])|{match["invoice_number"] for match in evidence["allocations"]})
                row["unknown_references"]=evidence["unknown_references"]
        return rows

    def bank_evidence_holds(self):
        return {inv.number:"bank_evidence: "+self._bank_evidence_reason() for inv in self.list_invoices()} if self._bank_evidence_pending() else {}

    def receipt_currency_evidence(self, payment_id):
        ids = self._receipt_cohort_ids({payment_id})
        result = []
        for row in self._conn.execute("SELECT * FROM bank_currency_evidence ORDER BY at,observation_id"):
            if row["payment_id"] in ids:
                item = dict(row)
                item["evidence"] = json.loads(item["evidence"])
                result.append(item)
        return result

    def confirm_bank_evidence(self, identity, *, expected_snapshot, reason, by="", at=None):
        """A person verifies the bank and current ledger; no payment is invented.

        Corrected exports must be imported separately. Neither that import nor
        this verification chooses an alias, reverses history or restores approval.
        """
        with self._tx() as c:
            if (not c.execute("SELECT 1 FROM bank_alias_issues WHERE id=? AND resolved_at IS NULL",(identity,)).fetchone()
                    or not expected_snapshot or expected_snapshot != self.bank_evidence_snapshot() or not reason.strip() or not by.strip()):
                raise ConflictError(self._bank_evidence_reason()+"; snapshot changed or verified bank explanation missing; read /cashdesk bank-evidence again")
            c.execute("UPDATE bank_alias_issues SET resolved_at=?,resolved_by=?,resolution=? WHERE id=?",(_ts(at),by,reason,identity))
            self.log_event("bank_alias_evidence_verified",identity,{"snapshot":expected_snapshot,"by":by,"reason":reason,"financial_rows_unchanged":True},at)
            self.refresh_reminders(at=at)

    def invoice_import_snapshot(self):
        tables = ("invoice_import_issues", "invoices", "invoice_sources", "customers", "matches", "drafts", "approvals", "sends")
        evidence = {table: [dict(row) for row in self._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
        return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()

    def invoice_import_questions(self):
        rows = [dict(row) for row in self._conn.execute("SELECT * FROM invoice_import_issues WHERE resolved_at IS NULL ORDER BY at, id")]
        if rows:
            token = self.invoice_import_snapshot()
            for row in rows:
                row["snapshot"] = token
        return rows

    def invoice_import_holds(self):
        sources = {row["number"]: row["source"] for row in self._conn.execute("SELECT * FROM invoice_sources")}
        holds = {}
        invoices=self.list_invoices()
        for issue in self._conn.execute("SELECT * FROM invoice_import_issues WHERE resolved_at IS NULL"):
            lifecycle="invoice lifecycle evidence conflicts" in issue["detail"]
            owners={v for v in (issue["exported_customer"],issue["bound_customer"]) if v}
            owners.update(i.customer_id for i in invoices if i.number==issue["number"])
            if lifecycle and issue["customer_origin"]=="derived":
                owners.update(c.id for c in self.list_customers() if customer_key(c.name)==issue["exported_customer"])
            unknown=lifecycle and (not owners or not issue["exported_customer"] or issue["customer_origin"] not in ("explicit","derived"))
            for inv in invoices:
                affected = (unknown or inv.customer_id in owners or inv.number==issue["number"]) if lifecycle else (inv.number==issue["number"] if issue["number"] else sources.get(inv.number,issue["source"])==issue["source"])
                if affected:
                    holds[inv.number] = ("invoice import uncertainty: a failed update may change this debt; "
                                         "fix and re-import the original source row, or verify /cashdesk import-errors")
        return holds

    def _ingest_invoice_issues(self, result, records, file_name, at):
        c = self._conn
        # Only a valid matching source/document/customer row is recovery evidence.
        # A different source, unrelated valid row or a file-wide failure cannot
        # clear a question. Insert this import's failures AFTER resolving older
        # ones, so a duplicate row never clears its own conflicting evidence.
        for inv in records:
            for old in c.execute("SELECT * FROM invoice_import_issues WHERE resolved_at IS NULL AND number=? AND source=?", (inv.number, result.source)).fetchall():
                if old["bound_customer"] and old["bound_customer"] != inv.customer_id:
                    continue
                c.execute("UPDATE invoice_import_issues SET resolved_at=?, resolved_by=? WHERE id=?", (_ts(at), "corrected source row", old["id"]))
                self.log_event("invoice_import_error_resolved", old["id"], {"number": inv.number, "fingerprint": result.fingerprint}, at)
        issues = getattr(result, "invoice_issues", ())
        observed = [(issue.number, issue.customer_id, issue.row, issue.raw, issue.detail, getattr(issue,"customer_origin","")) for issue in issues]
        represented = {issue.row for issue in issues}
        # Header/date-inference/read errors have no safely parsed row identity.
        observed.extend(("", "", err.row, err.raw, err.message, "unknown") for err in result.errors if err.row not in represented)
        for number, customer, row, raw, detail, origin in observed:
            if "skipped source observation may contradict" in detail and not c.execute("SELECT 1 FROM invoices WHERE number=?",(number,)).fetchone():
                continue
            identity = hashlib.sha256(json.dumps([result.source, result.fingerprint, row, raw, detail], ensure_ascii=True).encode()).hexdigest()[:24]
            saved = c.execute("SELECT customer_id FROM invoices WHERE number=?", (number,)).fetchone()
            c.execute("INSERT OR IGNORE INTO invoice_import_issues (id,source,fingerprint,number,exported_customer,bound_customer,row_number,raw,detail,file,at,customer_origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (identity, result.source, result.fingerprint, number, customer, saved["customer_id"] if saved else "", row, raw, detail, file_name, _ts(at), origin))
            self.log_event("invoice_import_error_retained", identity, {"number": number, "row": row, "file": file_name}, at)

    def confirm_invoice_import_error(self, identity, *, expected_snapshot, by="", at=None):
        with self._tx() as c:
            issue = c.execute("SELECT * FROM invoice_import_issues WHERE id=? AND resolved_at IS NULL", (identity,)).fetchone()
            if not issue or not expected_snapshot or expected_snapshot != self.invoice_import_snapshot():
                raise ConflictError("invoice import uncertainty: snapshot changed; read /cashdesk import-errors and verify the original source again")
            c.execute("UPDATE invoice_import_issues SET resolved_at=?, resolved_by=? WHERE id=?", (_ts(at), by, identity))
            self.log_event("invoice_import_error_confirmed", identity, {"by": by, "snapshot": expected_snapshot, "financial_rows_unchanged": True}, at)
            self.refresh_reminders(at=at)

    @staticmethod
    def _invoice_key(text: str) -> str:
        """Invoice number as a person reads it: case, separators, a letter prefix and leading zeros ignored."""
        import re as _re
        key = _re.sub(r"[^0-9a-z]", "", (text or "").casefold())
        key = _re.sub(r"^[a-z]+", "", key).lstrip("0")
        return key if any(ch.isdigit() for ch in key) else ""

    def _pending_receipt_holds(self, invoices, as_of: dt.date) -> dict[str, str]:
        """A pending incoming bank line that names an open invoice for exactly its open balance
        holds that invoice for a week from the line's date: the money is on its way. Never cash."""
        from .matching import invoices_named
        open_invoices = [inv for inv in invoices if inv.status in ("open", "disputed") and inv.balance > 0]
        holds: dict[str, str] = {}
        for row in self._conn.execute("SELECT evidence, file, at FROM pending_receipts").fetchall():
            ev = json.loads(row["evidence"])
            try:
                amount = Decimal(str(ev.get("amount") or ""))
            except Exception:
                continue
            if amount <= 0:
                continue
            seen = dt.date.fromisoformat(ev["date"]) if ev.get("date") else dt.date.fromisoformat(row["at"][:10])
            if as_of > seen + dt.timedelta(days=7):
                continue
            text = " ".join(str(ev.get(k) or "") for k in ("reference", "memo", "payer"))
            for number in invoices_named(text, open_invoices):
                inv = next(i for i in open_invoices if i.number == number)
                if inv.balance == amount and (not ev.get("currency") or ev["currency"] == inv.currency):
                    holds.setdefault(number, f"pending_receipt: a pending bank line of {inv.currency} {amount} dated {seen.isoformat()} "
                                             f"names {number} ({row['file']}); the money is on its way but not booked, so no reminder "
                                             f"until {(seen + dt.timedelta(days=8)).isoformat()} unless it books first")
        return holds

    def possible_duplicate_invoices(self, invoices=None, customers=None) -> dict[str, str]:
        """Open invoices that look like one debt imported twice (renumbered export, or a Stripe invoice
        synced into Xero with the Stripe number as its reference): same amount and currency, same
        customer or recipient address, and numbers (or a reference) that match apart from case,
        separators, prefix and leading zeros. Held until a person says they are distinct."""
        invoices = invoices if invoices is not None else self.list_invoices()
        customers = customers if customers is not None else {c.id: c for c in self.list_customers()}
        cleared = {(r["a"], r["b"]) for r in self._conn.execute("SELECT a, b FROM distinct_invoices")}
        email = lambda inv: (getattr(customers.get(inv.customer_id), "email", "") or "").strip().casefold()
        live = [inv for inv in invoices if inv.status in ("open", "disputed") and inv.balance > 0]
        out: dict[str, str] = {}
        for i, a in enumerate(live):
            keys_a = {k for k in (self._invoice_key(a.number), self._invoice_key(a.reference or "")) if k}
            for b in live[i + 1:]:
                if a.currency != b.currency or a.amount != b.amount:
                    continue
                if not (a.customer_id == b.customer_id or (email(a) and email(a) == email(b))):
                    continue
                keys_b = {k for k in (self._invoice_key(b.number), self._invoice_key(b.reference or "")) if k}
                if not (self._invoice_key(a.number) in keys_b or self._invoice_key(b.number) in keys_a):
                    continue
                pair = tuple(sorted((a.number, b.number)))
                if pair in cleared:
                    continue
                why = (f"possible_duplicate: {a.number} and {b.number} look like the same {a.currency} {a.amount} invoice imported twice; "
                       f"a reminder would ask for both. If they are different invoices, run /cashdesk not-duplicate {pair[0]} {pair[1]} <why>")
                out.setdefault(a.number, why); out.setdefault(b.number, why)
        return out

    def confirm_distinct_invoices(self, a: str, b: str, *, reason: str, by: str, at: dt.datetime | None = None) -> None:
        if not str(reason or "").strip() or not str(by or "").strip():
            raise ConflictError("not-duplicate needs a reason: say how you know these are two different invoices")
        pair = tuple(sorted((a, b)))
        dupes = self.possible_duplicate_invoices()
        if pair[0] not in dupes or pair[1] not in dupes or pair[1] not in dupes[pair[0]]:
            raise ConflictError(f"{pair[0]} and {pair[1]} are not held as a possible duplicate pair; nothing to confirm")
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO distinct_invoices VALUES (?, ?, ?, ?, ?)", (*pair, reason, by, _ts(at)))
            self.log_event("distinct_invoices_confirmed", pair[0], {"other": pair[1], "reason": reason, "by": by}, at)

    def record_bank_refusal(self, file: str, reason: str, at: dt.datetime | None = None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO bank_refusals VALUES (?, ?, ?)", (file, reason, _ts(at)))
            self.log_event("bank_import_refused", file, {"reason": reason}, at)

    def clear_bank_refusals(self, file: str | None = None) -> None:
        with self._tx() as c:
            if file is None:
                c.execute("DELETE FROM bank_refusals")
            else:
                c.execute("DELETE FROM bank_refusals WHERE file = ?", (file,))

    def bank_refusals(self) -> list[tuple[str, str]]:
        return [(r["file"], r["reason"]) for r in self._conn.execute("SELECT file, reason FROM bank_refusals ORDER BY rowid")]

    def sync_payment_iban(self, iban: str, at: dt.datetime | None = None) -> int:
        """Invoices whose export carries no IBAN use the payment_iban setting, and follow it when it
        changes (approvals of drafts stating the old account then go stale). Export IBANs are untouched."""
        with self._tx() as c:
            filled = {r["number"] for r in c.execute("SELECT number FROM setting_iban")}
            rows = c.execute("SELECT number, iban FROM invoices").fetchall()
            changed = 0
            for r in rows:
                mine = r["number"] in filled
                if not iban:
                    if mine and r["iban"]:
                        c.execute("UPDATE invoices SET iban='' WHERE number=?", (r["number"],))
                        c.execute("DELETE FROM setting_iban WHERE number=?", (r["number"],))
                        changed += 1
                    continue
                if (not r["iban"] or mine) and r["iban"] != iban:
                    c.execute("UPDATE invoices SET iban=? WHERE number=?", (iban, r["number"]))
                    c.execute("INSERT OR REPLACE INTO setting_iban VALUES (?, ?, ?)", (r["number"], iban, _ts(at)))
                    changed += 1
            if changed:
                self.log_event("payment_iban_synced", iban or "(cleared)", {"invoices": changed}, at)
            return changed

    def would_go_missing(self, source: str, listed) -> tuple[list[str], int]:
        """Open invoices of ``source`` that an export listing ``listed`` would hold as not in the export,
        and how many open invoices that source has now."""
        listed = set(listed)
        rows = self._conn.execute(
            "SELECT i.number FROM invoices i JOIN invoice_sources s ON s.number = i.number "
            "WHERE s.source = ? AND i.status IN ('open', 'disputed') AND CAST(i.balance AS REAL) > 0", (source,)).fetchall()
        already = set(self.missing_from_export())  # held earlier: not news, do not count again
        open_now = [r["number"] for r in rows if r["number"] not in already]
        return [n for n in open_now if n not in listed], len(open_now)

    def confirm_still_open(self, number: str, *, reason: str, by: str, at: dt.datetime | None = None) -> None:
        """A person confirms that an invoice the latest export left out is still owed."""
        if not str(reason or "").strip() or not str(by or "").strip():
            raise ConflictError("still-open needs a reason: say how you know the invoice is still owed")
        with self._tx() as c:
            row = c.execute("SELECT file FROM missing_from_export WHERE number=?", (number,)).fetchone()
            if row is None:
                raise ConflictError(f"{number} is not held as missing from an export; nothing to confirm")
            c.execute("DELETE FROM missing_from_export WHERE number=?", (number,))
            self.log_event("still_open_confirmed", number, {"file": row["file"], "reason": reason, "by": by}, at)

    def release_missing(self, numbers, *, file_name: str = "", at: dt.datetime | None = None) -> list[str]:
        """A re-sent copy of an earlier export lists these invoices again: they are not missing."""
        with self._tx() as c:
            held = [n for n in numbers if c.execute("SELECT 1 FROM missing_from_export WHERE number=?", (n,)).fetchone()]
            c.executemany("DELETE FROM missing_from_export WHERE number=?", [(n,) for n in held])
            for n in held:
                self.log_event("listed_again", n, {"file": file_name}, at)
        return held

    def missing_from_export(self) -> dict[str, str]:
        """Open invoices that the latest open-invoice export of their source no longer lists -> that file."""
        return {r["number"]: r["file"] for r in self._conn.execute("SELECT number, file FROM missing_from_export").fetchall()}

    def snooze(self, customer_id: str, until: dt.date, reason: str = "", at: dt.datetime | None = None) -> None:
        """The customer promised to pay by ``until``: no reminder until then (and a short grace)."""
        self.get_customer(customer_id)
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO snoozes (customer_id, until, reason, at) VALUES (?, ?, ?, ?)", (customer_id, until.isoformat(), reason, _ts(at)))
            self.log_event("snooze", customer_id, {"until": until.isoformat(), "reason": reason}, at)
            if self._depth == 1:
                self.refresh_reminders(at=at)

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

    def upsert_customers(self, customers: Iterable[Customer], at: dt.datetime | None = None, mode: str = "replace", identity_origin: str = "explicit", contact_fields: Iterable[str] | None = None) -> int:
        """Insert or update customers.

        ``replace`` (the customers file): name and language are written, a
        blank e-mail or contact preserves a stored one unless its name is
        explicitly supplied in contact_fields (authoritative CSV column presence). ``fill`` (customers
        derived from an invoice export): only a missing customer is created and
        only blank e-mail and contact fields are filled. A hold in the store
        survives both; a hold on the incoming record is applied and moves the
        customer's pending and approved drafts to held.
        """
        # None preserves the legacy programmatic API's blank-means-unspecified
        # convention. An authoritative CSV supplies which columns were present.
        contact_fields = frozenset(contact_fields or ())
        if not contact_fields <= {"email", "contact_name"}:
            raise ValueError("contact_fields must contain only email or contact_name")
        if mode not in UPSERT_MODES:
            raise ValueError(f"mode must be one of {', '.join(UPSERT_MODES)}")
        if mode == "replace":
            update = """name = excluded.name, language = excluded.language,
                        email = CASE WHEN excluded.email <> '' THEN excluded.email ELSE customers.email END,
                        contact_name = CASE WHEN excluded.contact_name <> '' THEN excluded.contact_name ELSE customers.contact_name END"""
        else:
            update = """email = CASE WHEN customers.email = '' THEN excluded.email ELSE customers.email END,
                        contact_name = CASE WHEN customers.contact_name = '' THEN excluded.contact_name ELSE customers.contact_name END"""
        if mode == "replace":
            for field in contact_fields:
                old = f"{field} = CASE WHEN excluded.{field} <> '' THEN excluded.{field} ELSE customers.{field} END"
                update = update.replace(old, f"{field} = excluded.{field}")
        n = 0
        with self._tx() as c:
            self._remember_customer_identity_scope(at)
            for cu in customers:
                before=self._contact_snapshot(cu.id)
                if identity_origin != "observed":
                    self._record_customer_origin(cu.id, identity_origin, at)
                c.execute(
                    f"""INSERT INTO customers (id, name, language, email, contact_name, on_hold, hold_reason, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET {update},
                            on_hold = CASE WHEN excluded.on_hold = 1 THEN 1 ELSE customers.on_hold END,
                            hold_reason = CASE WHEN excluded.on_hold = 1 THEN excluded.hold_reason ELSE customers.hold_reason END,
                            updated_at = excluded.updated_at""",
                    (cu.id, cu.name, cu.language, cu.email, cu.contact_name, 1 if cu.on_hold else 0, cu.hold_reason, _ts(at)),
                )
                after=self._contact_snapshot(cu.id)
                if before is not None and before != after:
                    self.log_event("customer_contact_changed",cu.id,{"before":json.loads(before),"after":json.loads(after),"mode":mode,"explicit_contact_columns":sorted(contact_fields)},at)
                    # Revoke at each material transition, even if a caller batches
                    # A -> B -> A before the outer transaction refreshes eligibility.
                    for draft in self.list_drafts(customer_id=cu.id):
                        if draft.status in ("pending","approved","held") and draft.kind!="escalation":
                            self._invalidate_draft(draft,("stale","customer contact changed or its original basis is missing; run cashdesk_draft and read the current recipient and greeting before fresh approval"),at)
                if cu.on_hold:
                    self._hold_drafts(cu.id, at)
                n += 1
            if self._depth == 1:
                self.refresh_reminders(at=at)
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
            if self._depth == 1:
                self.refresh_reminders(at=at)

    def clear_hold(self, customer_id: str, at: dt.datetime | None = None) -> None:
        """Lift a hold; held drafts go back to pending and need a fresh approval."""
        with self._tx() as c:
            self.get_customer(customer_id)
            c.execute("UPDATE customers SET on_hold = 0, hold_reason = '', updated_at = ? WHERE id = ?", (_ts(at), customer_id))
            c.execute("UPDATE drafts SET status = 'pending', updated_at = ? WHERE customer_id = ? AND status = 'held'", (_ts(at), customer_id))
            c.execute("DELETE FROM draft_invalidations WHERE kind = 'on_hold' AND draft_id IN (SELECT id FROM drafts WHERE customer_id = ?)", (customer_id,))
            self.log_event("unhold", customer_id, {}, at)
            if self._depth == 1:
                self.refresh_reminders(at=at)

    def match_result(self, as_of: dt.date | None = None) -> MatchResult:
        """Read-only current matching, including unapplied and undismissed receipts."""
        identity_blocked = self._blocked_receipts()
        placements = self._placement_roots() - identity_blocked
        cleared_returns = {json.loads(r["evidence"]).get("payment_id") for r in self._conn.execute("SELECT evidence FROM bank_return_reviews WHERE resolved_at IS NOT NULL")}
        quarantine = self._return_quarantine()
        result = match_payments(self.list_invoices(), [p for p in self.unapplied_payments() if p.id not in placements and p.id not in cleared_returns and p.id not in quarantine], self.list_customers(), as_of or self._today(),
                                no_payer_files=no_payer_statements(self.list_payments()))
        placement_items = tuple(ReviewItem(replace(self.get_payment(root), amount=Decimal("0.00") if self._group_dismissed(root) else max(Decimal("0.00"), self.get_payment(root).amount - self.applied_from(root))),
                                          "MULTI_INVOICE", "receipt allocation review required: same identity has conflicting invoice/payer evidence; use Apply/Dismiss for unused money or /cashdesk receipt-keep to confirm existing completed allocations")
                                for root in sorted(placements))
        blocked = self._blocked_receipts()
        update_ids = self._receipt_update_ids()
        questions = tuple(ReviewItem(p, "RECEIPT_UPDATE" if p.id in update_ids else "POSSIBLE_DUPLICATE",
                                    (f"receipt update conflict: retained evidence is uncertain; {p.id} has {self.applied_from(p.id)} allocated. "
                                     "Read /cashdesk receipt-updates; verify bank evidence before keep/distinct/replace. No automatic undo."
                                     if p.id in update_ids else
                                     f"possible receipt overlap: observed row value, not confirmed new cash; {p.id} has {self.applied_from(p.id)} already allocated. "
                                     "Read /cashdesk overlaps and verify same or distinct before applying or dismissing"))
                          for p in self.list_payments() if p.id in blocked and self._receipt_root(p.id) == p.id)
        identity_holds = self.customer_identity_holds()
        bank_holds = self.bank_evidence_holds()
        import_holds = {**self.invoice_import_holds(), **identity_holds, **bank_holds}
        uncertain = [m for m in result.applied if m.invoice_number in import_holds]
        uncertain_ids = {m.payment_id for m in uncertain}
        uncertain = [m for m in result.applied if m.payment_id in uncertain_ids]
        incoming = {p.id: p for p in self.unapplied_payments()}
        import_questions = tuple(ReviewItem(incoming[pid], "CUSTOMER_IDENTITY" if any(m.invoice_number in identity_holds for m in uncertain if m.payment_id==pid) else "INVOICE_IMPORT", "customer identity review: verify /cashdesk identities before allocating this receipt" if any(m.invoice_number in identity_holds for m in uncertain if m.payment_id==pid) else "invoice import uncertainty: verify /cashdesk import-errors before allocating this receipt",
                                           named=tuple(m.invoice_number for m in uncertain if m.payment_id == pid)) for pid in sorted({m.payment_id for m in uncertain}))
        result = replace(result, applied=tuple(m for m in result.applied if m.payment_id not in uncertain_ids))
        counted = self._export_counted(result.applied)
        if counted:
            counted_items = tuple(ReviewItem(incoming[m.payment_id], "ALREADY_COUNTED", reason, named=(m.invoice_number,))
                                  for m, reason in counted)
            counted_ids = {m.payment_id for m, _ in counted}
            result = replace(result, applied=tuple(m for m in result.applied if m.payment_id not in counted_ids),
                             review=(*result.review, *counted_items),
                             held_invoices={**result.held_invoices, **{m.invoice_number: reason for m, reason in counted}})
        if bank_holds:
            # Keep genuine receipts visible, but offer no Apply/Dismiss authority.
            import_questions = tuple(replace(item,kind="BANK_EVIDENCE",detail=self._bank_evidence_reason()) for item in import_questions)
            result = replace(result,
                             review=tuple(replace(item,kind="BANK_EVIDENCE",detail=self._bank_evidence_reason()) for item in result.review),
                             unmatched=tuple(replace(item,kind="BANK_EVIDENCE",detail=self._bank_evidence_reason()) for item in result.unmatched))
        aliases = tuple(p for p in self.list_payments() if self._receipt_root(p.id) != p.id)
        invoices_now = self.list_invoices()
        returned_items = tuple(ReviewItem(p, "RETURNED_PAYMENT", self._quarantine_reason(p.id),
                                          named=tuple(invoices_named(f"{p.reference} {p.memo}", invoices_now)))
                               for p in self.unapplied_payments() if p.id in quarantine and p.id not in placements)
        named_holds = self._receipt_names_customer_holds((*result.review, *result.unmatched, *questions, *placement_items, *import_questions, *returned_items), result.held_invoices)
        return replace(result, review=(*result.review, *questions, *placement_items, *import_questions, *returned_items), duplicates=(*result.duplicates, *aliases),
                       held_invoices={**named_holds, **result.held_invoices, **self._overlap_holds(), **{m.invoice_number: "invoice import uncertainty: a shared receipt needs verification via /cashdesk import-errors" for m in uncertain}, **import_holds, **self.return_holds(), **self.customer_identity_holds()})

    def _receipt_names_customer_holds(self, items, already) -> dict[str, str]:
        """Hold a customer's reminders while an unresolved incoming receipt names that customer.

        A director paying from a personal account, a statement with no payer column, or a payment
        short by bank fees cannot be applied, but a reminder to that customer could chase money it
        has already sent. The name must appear as whole words in the payer, reference or memo.
        """
        import re as _re
        from .matching import normalise_name
        holds: dict[str, str] = {}
        customers = [(c, normalise_name(c.name)) for c in self.list_customers()]
        customers = [(c, base) for c, base in customers if len(base.replace(" ", "")) >= 4]
        if not customers:
            return holds
        open_by_customer: dict[str, list[str]] = {}
        for inv in self.list_invoices():
            if inv.status == "open" and inv.balance > 0:
                open_by_customer.setdefault(inv.customer_id, []).append(inv.number)
        for item in items:
            p = item.payment
            if p.amount <= 0:
                continue
            text = normalise_name(" ".join(x for x in (p.counterparty, p.reference, p.memo) if x))
            for c, base in customers:
                if not _re.search(rf"(?<!\w){_re.escape(base)}(?!\w)", text):
                    continue
                for number in open_by_customer.get(c.id, ()):
                    if number not in already:
                        holds.setdefault(number, f"held: receipt {p.id} names {c.name} but is not matched to an invoice; "
                                                 "check it in the match review before chasing this customer")
        return holds

    def _export_counted(self, applied) -> list:
        """Automatic matches whose receipt may be money the invoice export already counts as paid.

        An export that shows part of an invoice paid (Xero InvoiceAmountPaid, a balance below the
        total) has usually reconciled a bank receipt. A receipt for exactly that paid amount could be
        the same money, or a second instalment of the same size: a person decides, never the matcher.
        """
        out = []
        for m in applied:
            row = self._conn.execute("SELECT external_paid FROM invoice_settlements WHERE number=?", (m.invoice_number,)).fetchone()
            if not row or row["external_paid"] is None:
                continue
            paid = Decimal(row["external_paid"])
            if paid > 0 and paid == m.amount:
                out.append((m, f"receipt may already be counted: the invoice export already shows {m.currency} {paid} paid on "
                               f"{m.invoice_number}, the same as receipt {m.payment_id}; if it is that payment, Dismiss it; if it is a "
                               "further payment, Apply it. Reminders for the invoice are held until you decide"))
        return out

    def _legacy_credit_holds(self) -> dict[str, tuple[str, str]]:
        customers = {customer.id: customer for customer in self.list_customers()}
        holds = {}
        # Older imports could leave a credit under the export's derived ID.
        # Protect plausible owners without guessing a financial reassignment;
        # only a fresh compatible credit observation can adopt that identity.
        for credit in self.list_invoices():
            if credit.balance >= 0 or credit.status not in ("open", "disputed"):
                continue
            origin = self._conn.execute("SELECT explicit FROM customer_origins WHERE customer_id=?",(credit.customer_id,)).fetchone()
            if origin and origin["explicit"]:
                continue  # Equal names do not merge independently authoritative IDs.
            old_customer = customers.get(credit.customer_id)
            for customer in customers.values():
                same_name = old_customer and " ".join(old_customer.name.split()).casefold() == " ".join(customer.name.split()).casefold()
                if customer.id != credit.customer_id and (customer_key(customer.name) == credit.customer_id or same_name):
                    holds[customer.id] = ("credit_note", f"credit identity needs review: credit {credit.number} may belong to this customer; "
                                                   "re-import its original numbered accounting export before chasing")
        return holds

    def _eligibility(self, as_of: dt.date) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
        invoices = self.list_invoices()
        customers = {c.id: c for c in self.list_customers()}
        review = self.match_result(as_of)
        holds = dict(review.held_invoices)
        for candidate in review.applied:
            holds.setdefault(candidate.invoice_number,
                             f"payment_in_review: payment {candidate.payment_id} is ready to apply; run cashdesk_match before chasing this invoice")
        for item in review.credit_notes:
            for number in item.named:
                holds[number] = f"credit_note: {item.detail}"
        for number, reason in self.possible_duplicate_invoices(invoices, customers).items():
            holds.setdefault(number, reason)
        for number, reason in self._pending_receipt_holds(invoices, as_of).items():
            holds.setdefault(number, reason)
        refused = self.bank_refusals()
        if refused:
            name, why = refused[-1]
            for inv in invoices:
                if inv.is_open:
                    holds.setdefault(inv.number, f"bank_refused: the bank statement {name} was refused, so payments in it are unknown "
                                                 f"({why[:120]}); fix and import it again, or draft with bank_checked=true after checking")
        for row in self._conn.execute("SELECT number, planned_date, not_sent, file FROM invoice_source_flags").fetchall():
            if row["not_sent"]:
                holds.setdefault(row["number"], f"not_sent: the invoice export ({row['file']}) says {row['number']} was never sent to the customer; "
                                                "send the invoice itself first, then re-import the export")
            elif row["planned_date"]:
                planned = dt.date.fromisoformat(row["planned_date"])
                if as_of <= planned + dt.timedelta(days=2):
                    holds.setdefault(row["number"], f"planned_payment: the customer's expected payment date {planned.isoformat()} is recorded in "
                                                    f"the invoice export ({row['file']}); chased again from {(planned + dt.timedelta(days=3)).isoformat()}")
        for number, file in self.missing_from_export().items():
            holds.setdefault(number, f"not_in_export: not in the latest invoice export ({file}); it may have been paid. "
                                     f"Check before chasing; it is chased again if a later export lists it, or run "
                                     f"/cashdesk still-open {number} <why> if you know it is still owed")
        customer_holds = _customer_holds(invoices, customers)
        customer_holds.update(self._legacy_credit_holds())
        surplus_holds = self.surplus_customer_holds()
        customer_holds.update(surplus_holds)
        # Two customer records behind one mailbox are often one business (a Xero contact and a
        # Stripe customer): a hold or dispute on one must not let the other be chased.
        by_address: dict[str, list[str]] = {}
        for cid, cust in customers.items():
            for address in {a.strip().casefold() for a in re.split(r"[;,]", cust.email or "") if a.strip()}:
                by_address.setdefault(address, []).append(cid)
        for address, ids in by_address.items():
            stopped = [cid for cid in ids if cid in customer_holds and customer_holds[cid][0] in ("on_hold", "dispute")]
            for cid in ids:
                if stopped and cid not in customer_holds:
                    other = customers[stopped[0]]
                    customer_holds[cid] = ("shared_address", f"shares the address {address} with {other.name}, which is "
                                           f"{'in dispute' if customer_holds[stopped[0]][0] == 'dispute' else 'on hold'}; check before chasing")
        snoozed = self.snoozed(as_of)
        for inv in invoices:
            if not inv.is_open:
                continue
            if inv.customer_id in customer_holds:
                code, detail = customer_holds[inv.customer_id]
                holds[inv.number] = f"{code}: {detail}"
            elif inv.customer_id in snoozed:
                until, why = snoozed[inv.customer_id]
                holds.setdefault(inv.number, f"promise: promised to pay by {until.isoformat()}" + (f" ({why})" if why else "")
                                 + "; no reminder until two days after that")
            elif inv.balance < self.ignore_balances_below:
                holds.setdefault(inv.number, f"small_balance: only {format_money(inv.balance, inv.currency)} is open "
                                 "(under the ignore_balances_below setting); write it off or chase it by hand")
            customer = customers.get(inv.customer_id)
            if customer is None:
                holds.setdefault(inv.number, "no_customer: no customer record for this id")
            elif _contact_problem(customer):
                holds.setdefault(inv.number, "no_email: the customer needs an e-mail address")
        for number, reason in self.settlement_reviews().items():
            holds[number] = reason
        for inv in invoices:
            if inv.customer_id in surplus_holds:
                code, detail = surplus_holds[inv.customer_id]
                holds[inv.number] = f"{code}: {detail}"
        holds.update(self.invoice_import_holds())
        holds.update(self.return_holds())
        holds.update(self.customer_identity_holds())
        holds.update(self.bank_evidence_holds())
        return holds, customer_holds

    def reminder_holds(self, as_of: dt.date | None = None) -> dict[str, str]:
        """One current policy for planning, approval and sending. Does not write."""
        return self._eligibility(as_of or self._today())[0]

    def _draft_issue(self, draft: Draft, eligibility: tuple[dict[str, str], dict[str, tuple[str, str]]]) -> tuple[str, str] | None:
        if self._bank_evidence_pending():
            return "bank_evidence", self._bank_evidence_reason()
        holds, customer_holds = eligibility
        if draft.customer_id in customer_holds:
            code, detail = customer_holds[draft.customer_id]
            if code == "on_hold":
                detail = f"customer {draft.customer_id} is on hold ({detail})"
            elif code == "dispute":
                detail = f"customer {draft.customer_id} has a disputed invoice ({detail})"
            return code, detail
        overlap_holds = self._overlap_holds()
        import_holds = self.invoice_import_holds()
        for number in draft.invoice_numbers:
            if holds.get(number, "").startswith("customer_identity:"):
                return "customer_identity", holds[number]
            if holds.get(number, "").startswith("returned_payment:"):
                return "returned_payment", holds[number]
            if number in import_holds:
                return "invoice_import", import_holds[number]
            if number in overlap_holds:
                return "receipt_overlap", overlap_holds[number]
            if number in self.settlement_reviews():
                return "settlement_review", self.settlement_reviews()[number]
        stale = self._stale_reason(draft)
        if stale:
            return "stale", stale
        if draft.kind in REMINDER_KINDS:
            twin = self._same_address_reminder(draft)
            if twin:
                return "shared_address", twin
            for number in draft.invoice_numbers:
                if number in holds:
                    code, _, detail = holds[number].partition(": ")
                    if code not in ("credit_note", "promise", "not_in_export", "small_balance", "no_email", "no_customer"):
                        code, detail = "payment_in_review", holds[number]
                    return code, detail
        return None

    def _same_address_reminder(self, draft: Draft) -> str | None:
        """One reminder per mailbox within three days: another customer record's reminder to the
        same address that was sent, or is earlier in the queue, pauses this one."""
        mine = {r.strip().casefold() for r in draft.recipients if r.strip()}
        if not mine:
            return None
        for other in self.list_drafts():
            if other.id == draft.id or other.customer_id == draft.customer_id or other.kind not in REMINDER_KINDS:
                continue
            if other.status not in ("pending", "approved", "sent"):
                continue
            if not mine & {r.strip().casefold() for r in other.recipients}:
                continue
            if abs((other.created_at - draft.created_at).total_seconds()) > 3 * 86400:
                continue
            if other.status == "sent" or other.id < draft.id:
                return (f"another reminder ({other.id}) goes to the same address; one reminder per address within three days. "
                        "Reject one of them, or merge the customers in your accounting system")
        return None

    def _invalidate_draft(self, draft: Draft, issue: tuple[str, str], at: dt.datetime | None = None) -> bool:
        if draft.status == "sent":
            return False
        code, reason = issue
        # Keep obsolete text visible in the brief with its DO NOT APPROVE / STALE
        # warning until the next drafting run replaces it; it has no approval.
        status = "held"
        previous = self._conn.execute("SELECT kind, reason FROM draft_invalidations WHERE draft_id = ?", (draft.id,)).fetchone()
        changed = previous is None or (previous["kind"], previous["reason"], draft.status) != (code, reason, status)
        self._conn.execute("DELETE FROM approvals WHERE draft_id = ?", (draft.id,))
        if not changed:
            return False
        self._conn.execute("UPDATE drafts SET status = ?, updated_at = ? WHERE id = ?", (status, _ts(at), draft.id))
        self._conn.execute("INSERT OR REPLACE INTO draft_invalidations (draft_id, kind, reason, at) VALUES (?, ?, ?, ?)",
                           (draft.id, code, reason, _ts(at)))
        if draft.kind in REMINDER_KINDS:
            self._conn.executemany("DELETE FROM chase_state WHERE invoice_number = ? AND stage != 'thanks'", [(n,) for n in draft.invoice_numbers])
        if draft.kind == "thanks" and (code in ("receipt_overlap", "invoice_import", "customer_surplus", "returned_payment", "customer_identity", "bank_evidence") or (code=="stale" and "customer contact" in reason)):
            sent_numbers = {n for sent in self.list_drafts(status="sent") if sent.kind=="thanks" for n in sent.invoice_numbers}
            for match in self.list_matches():
                if match.invoice_number in draft.invoice_numbers and match.invoice_number not in sent_numbers:
                    key = ChaseState.thanks_key(match.invoice_number,match.payment_id)
                    self.log_event("thanks_redraft_after_contact_change" if code=="stale" else "thanks_redraft_after_surplus_review" if code == "customer_surplus" else "thanks_redraft_after_receipt_review",draft.id,{"state_key":key},at)
                    self._conn.execute("DELETE FROM chase_state WHERE invoice_number=? AND stage='thanks'",(key,))
        self.log_event("invalidate", draft.id, {"reason": reason, "kind": code, "status": status}, at)
        return True

    def refresh_reminders(self, as_of: dt.date | None = None, at: dt.datetime | None = None) -> None:
        """Revoke affected approvals in the same transaction as a ledger change.

        Resolving a hold never restores an approval. A fresh drafting run clears
        its invalidation only when it renders a currently eligible reminder.
        """
        with self._tx():
            self._remember_reference_history(at)
            self._remember_payer_history(at)
            self._remember_return_scope(at)
            self._remember_customer_identity_scope(at)
            eligibility = self._eligibility(as_of or (_now(at).date() if at is not None else self._today()))
            for draft in self.list_drafts():
                if draft.status in ("pending", "approved", "held"):
                    issue = self._draft_issue(draft, eligibility)
                    if issue:
                        self._invalidate_draft(draft, issue, at)

    def _contact_snapshot(self, customer_id: str) -> str | None:
        row=self._conn.execute("SELECT id,name,language,email,contact_name FROM customers WHERE id=?",(customer_id,)).fetchone()
        return json.dumps(dict(row),sort_keys=True) if row else None

    def _contact_reason(self, draft: Draft) -> str | None:
        if draft.kind == "escalation" or draft.status == "sent":return None
        basis=self._conn.execute("SELECT snapshot FROM draft_contact_basis WHERE draft_id=?",(draft.id,)).fetchone()
        current=self._contact_snapshot(draft.customer_id)
        if basis is None or current is None or basis["snapshot"] != current:
            return "customer contact changed or its original basis is missing; run cashdesk_draft and read the current recipient and greeting before fresh approval"
        email=json.loads(current)["email"]
        expected=tuple(dict.fromkeys(a.strip() for a in re.split(r"[;,]", email or "") if a.strip()))
        if not expected or tuple(draft.recipients)!=expected:
            return "customer contact changed: recipient must equal the current customer email; correct contacts and redraft before approval"
        return None

    def _stale_reason(self, draft: Draft) -> str | None:
        """A reminder must still be true when it is approved and when it is sent: every invoice
        open, and its open balance and due date still the figures the draft states."""
        contact=self._contact_reason(draft)
        if contact:return contact
        if draft.kind not in REMINDER_KINDS:
            return None
        basis = {r["invoice_number"]: r for r in self._conn.execute("SELECT * FROM draft_basis WHERE draft_id = ?", (draft.id,)).fetchall()}
        for number in draft.invoice_numbers:
            row = self._conn.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone()
            if row is None:
                return f"{number} is not in the ledger"
            inv = _invoice(row)
            if not inv.is_open:
                return f"{number} is {inv.status}; redraft before approving"
            was = basis.get(number)
            if was is None or any(was[name] is None for name in ("reference", "iban", "customer_id")):
                return f"payment instructions were not recorded for {number}; run cashdesk_draft and read the replacement before approving"
            if (Decimal(was["balance"]), was["currency"]) != (inv.balance, inv.currency):
                return f"{number} now has {inv.currency} {inv.balance:,.2f} open, not the amount this draft states; redraft before approving"
            if was["due_date"] != inv.due_date.isoformat():
                return f"{number} is now due {inv.due_date.isoformat()}, not the date this draft states; redraft before approving"
            if (was["reference"], was["iban"], was["customer_id"]) != (inv.reference, inv.iban, inv.customer_id):
                return f"payment instructions changed for {number} (bank account, payment reference or customer); run cashdesk_draft and read the replacement before approving"
        return None

    def stale_drafts(self) -> list[tuple[Draft, str]]:
        """Pending or approved reminders that are no longer true, with the reason."""
        out = []
        for d in self.list_drafts():
            invalidation = self._conn.execute("SELECT reason FROM draft_invalidations WHERE draft_id = ? AND kind = 'stale'", (d.id,)).fetchone()
            if d.status in ("pending", "approved", "held") or (d.status == "rejected" and invalidation):
                why = self._stale_reason(d)
                if not why and invalidation:
                    why = invalidation["reason"]
                if why:
                    out.append((d, why))
        return out

    def _applied_total(self, c: sqlite3.Connection, number: str) -> Decimal | None:
        rows = c.execute("SELECT amount FROM matches WHERE invoice_number = ?", (number,)).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0")) if rows else None

    # ------------------------------------------------------------ invoices

    @staticmethod
    def _settlement_reason(detail: str) -> str:
        return ("settlement reconciliation required: " + detail +
                "; read /cashdesk settlements, verify payments outside Cash Desk, then use /cashdesk reconcile")

    def _record_surplus(self, number, customer, currency, amount, balance, source, fingerprint, raw, kind, at=None):
        if amount < 0 or balance >= 0:
            return
        values = [number, customer, currency, str(to_money(amount)), str(to_money(balance)), source, fingerprint, raw, kind]
        identity = hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()[:24]
        contact = self._conn.execute("SELECT name FROM customers WHERE id=?", (customer,)).fetchone()
        previous = self._conn.execute("SELECT customer_id FROM invoices WHERE number=?", (number,)).fetchone()
        prior_customer = previous["customer_id"] if previous and previous["customer_id"] != customer else ""
        self._conn.execute("INSERT OR IGNORE INTO invoice_surpluses VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)", [identity, *values, _ts(at), contact["name"] if contact else "", prior_customer])
        self.log_event("invoice_surplus_observed", number, {"customer": customer, "currency": currency, "observed_balance": str(balance), "kind": kind}, at)

    def _record_basis_surplus(self, inv, balance, kind, at=None):
        # The CSV observation is more informative than a normalized fallback.
        # Do not manufacture another credit for the same pending signed facts.
        if self._conn.execute("SELECT 1 FROM invoice_surpluses WHERE number=? AND customer_id=? AND currency=? AND amount=? AND balance=? AND resolved_at IS NULL",
                              (inv.number, inv.customer_id, inv.currency, str(inv.amount), str(to_money(balance)))).fetchone():
            return
        basis = self._conn.execute("SELECT * FROM invoice_settlements WHERE number=?", (inv.number,)).fetchone()
        source = self._conn.execute("SELECT * FROM invoice_sources WHERE number=?", (inv.number,)).fetchone()
        raw = json.dumps({"origin": kind, "invoice": repr(inv), "basis": dict(basis) if basis else None,
                          "known_source": dict(source) if source else None,
                          "local_allocated": str(self._applied_total(self._conn, inv.number) or Decimal("0")),
                          "possible_surplus": str(-balance)}, sort_keys=True)
        self._record_surplus(inv.number, inv.customer_id, inv.currency, inv.amount, balance, kind, _ts(at), raw, kind, at)

    def _migrate_surpluses(self):
        with self._tx() as c:
            if c.execute("SELECT 1 FROM invoice_surplus_state WHERE key='legacy_scanned'").fetchone():
                return
            for inv in self.list_invoices():
                if inv.amount < 0 or c.execute("SELECT 1 FROM credit_sources WHERE number=?", (inv.number,)).fetchone():
                    continue
                basis = c.execute("SELECT * FROM invoice_settlements WHERE number=?", (inv.number,)).fetchone()
                local = self._applied_total(c, inv.number)
                evidence = [inv.balance] + ([inv.amount - local] if local is not None else [])
                if basis:
                    if basis["export_settled"] is not None:
                        evidence.append(inv.amount - Decimal(basis["export_settled"]))
                    if basis["external_paid"] is not None:
                        evidence.append(inv.amount - Decimal(basis["external_paid"]) - (self._applied_total(c, inv.number) or Decimal("0")))
                if min(evidence) < 0:
                    self._record_basis_surplus(inv, min(evidence), "legacy_basis", None)
            # Earlier clamps may have been followed by zero exports that
            # replaced the basis, while the negative observation remains in an
            # audit event. Retain it as historical, not verified available cash.
            for event in c.execute("SELECT * FROM events WHERE kind='import_kept_payment'").fetchall():
                detail = json.loads(event["detail"])
                balance = to_money(detail.get("export_balance", "0"))
                if balance >= 0:
                    continue
                saved = c.execute("SELECT * FROM invoices WHERE number=?", (event["ref"],)).fetchone()
                if not saved or Decimal(saved["amount"]) < 0 or c.execute("SELECT 1 FROM credit_sources WHERE number=?", (event["ref"],)).fetchone():
                    continue
                currency = detail.get("currency") or saved["currency"]
                if c.execute("SELECT 1 FROM invoice_surpluses WHERE number=? AND customer_id=? AND currency=? AND balance=? AND resolved_at IS NULL",
                             (event["ref"], saved["customer_id"], currency, str(balance))).fetchone():
                    continue
                raw = json.dumps({"origin": "legacy_event", "event": dict(event), "current_reference_invoice": dict(saved),
                                  "historical_customer_not_independently_verified": True}, sort_keys=True)
                self._record_surplus(event["ref"], saved["customer_id"], currency, Decimal(saved["amount"]), balance,
                                     "legacy_event", "event:" + str(event["id"]), raw, "legacy_event", None)
            c.execute("INSERT INTO invoice_surplus_state VALUES ('legacy_scanned','1')")
            if c.execute("SELECT 1 FROM invoice_surpluses WHERE resolved_at IS NULL").fetchone():
                self.refresh_reminders()

    def _surplus_owners(self, row, known):
        current = self._conn.execute("SELECT customer_id FROM invoices WHERE number=?", (row["number"],)).fetchone()
        customers = {row["customer_id"]} | ({row["prior_customer_id"]} if row["prior_customer_id"] else set()) | ({current["customer_id"]} if current else set())
        label = " ".join(row["customer_name"].split()).casefold()
        derived = bool(label) and row["customer_id"] == customer_key(row["customer_name"])
        for contact in known:
            if (derived and " ".join(contact.name.split()).casefold() == label) or customer_key(contact.name) == row["customer_id"]:
                customers.add(contact.id)
        return customers

    def surplus_customer_holds(self):
        holds = {}
        known = self.list_customers()
        for row in self._conn.execute("SELECT * FROM invoice_surpluses WHERE resolved_at IS NULL"):
            for customer in self._surplus_owners(row, known):
                holds[customer] = ("customer_surplus", "customer surplus review: exported or historical excess may be available to this customer; verify /cashdesk surpluses before collection or acknowledgement")
        return holds

    def surplus_snapshot(self):
        tables = ("invoice_surpluses", "invoices", "invoice_settlements", "invoice_sources", "invoice_closures", "customers", "matches", "drafts", "approvals", "sends")
        rows = {table: [dict(row) for row in self._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    def surplus_reviews(self):
        groups = {}
        for row in self._conn.execute("SELECT * FROM invoice_surpluses WHERE resolved_at IS NULL ORDER BY at,id"):
            groups.setdefault(row["number"], []).append(dict(row))
        token = self.surplus_snapshot() if groups else ""
        questions = []
        known = self.list_customers()
        invoices = self.list_invoices()
        for number, observations in groups.items():
            current = self._conn.execute("SELECT * FROM invoices WHERE number=?", (number,)).fetchone()
            basis = self._conn.execute("SELECT * FROM invoice_settlements WHERE number=?", (number,)).fetchone()
            matches = [dict(row) for row in self._conn.execute("SELECT * FROM matches WHERE invoice_number=?", (number,))]
            owners = set().union(*(self._surplus_owners(row, known) for row in observations))
            questions.append({"number": number, "observations": observations, "snapshot": token,
                              "customer_ids": sorted(owners), "affected_invoices": sorted(inv.number for inv in invoices if inv.customer_id in owners),
                              "current": dict(current) if current else None, "settlements": dict(basis) if basis else None,
                              "allocations": matches,
                              "message": "Observations are not summed or allocated; verify every version against accounting records"})
        return questions

    def confirm_surplus(self, number, *, expected_snapshot, by="", at=None):
        with self._tx() as c:
            if (not c.execute("SELECT 1 FROM invoice_surpluses WHERE number=? AND resolved_at IS NULL", (number,)).fetchone()
                    or not expected_snapshot or expected_snapshot != self.surplus_snapshot()):
                raise ConflictError("customer surplus review: snapshot changed; read /cashdesk surpluses and verify the accounting records again")
            current = c.execute("SELECT balance FROM invoices WHERE number=?", (number,)).fetchone()
            if current and Decimal(current["balance"]) < 0:
                raise ConflictError("customer surplus review: the ledger still has a negative balance; correct and re-import the accounting export first")
            c.execute("UPDATE invoice_surpluses SET resolved_at=?,resolved_by=? WHERE number=? AND resolved_at IS NULL", (_ts(at), by, number))
            self.log_event("invoice_surplus_accounted", number, {"by": by, "snapshot": expected_snapshot, "financial_rows_unchanged": True}, at)
            self.refresh_reminders(at=at)

    def settlement_reviews(self) -> dict[str, str]:
        return {r["number"]: r["reason"] for r in self._conn.execute(
            "SELECT number, reason FROM invoice_settlements WHERE reason != '' ORDER BY number")}

    def settlement_snapshot(self, number: str) -> str:
        """Bind a person's reconciliation to the complete current ledger/basis snapshot."""
        inv = self.get_invoice(number)
        basis = self._conn.execute("SELECT * FROM invoice_settlements WHERE number=?", (number,)).fetchone()
        payload = [repr(inv), str(self._applied_total(self._conn, number)), dict(basis) if basis else None]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def reconcile_settlement(self, number: str, external_paid: Decimal | str, *, expected_balance: Decimal | str,
                             currency: str, expected_snapshot: str, by: str = "",
                             at: dt.datetime | None = None) -> None:
        """A person verified total non-local settlements, excluding every stored local match.

        Never deletes/reverses a match. Every caller must supply the complete snapshot token.
        """
        external = to_money(external_paid)
        with self._tx() as c:
            inv = self.get_invoice(number)
            if (inv.balance != to_money(expected_balance) or inv.currency != currency or
                    expected_snapshot != self.settlement_snapshot(number)):
                raise ConflictError("settlement snapshot changed; read /cashdesk settlements again")
            if external < 0 or external > inv.amount or inv.amount < 0:
                raise ConflictError("verified external settlement must be between zero and the invoice amount")
            local = self._applied_total(c, number) or Decimal("0")
            signed = inv.amount - external - local
            if signed < 0:
                self._record_basis_surplus(inv, signed, "verified_settlement", at)
            balance = max(Decimal("0.00"), signed)
            old = c.execute("SELECT * FROM invoice_settlements WHERE number=?", (number,)).fetchone()
            c.execute("INSERT INTO invoice_settlements VALUES (?, ?, ?, '', ?) ON CONFLICT(number) DO UPDATE SET external_paid=excluded.external_paid, reason='', updated_at=excluded.updated_at",
                      (number, str(external), str(external), _ts(at)))
            status = "void" if inv.status == "void" else ("paid" if balance == 0 else ("disputed" if inv.status == "disputed" else "open"))
            c.execute("UPDATE invoices SET balance=?, status=?, paid_date=?, updated_at=? WHERE number=?",
                      (str(balance), status, inv.paid_date.isoformat() if balance == 0 and inv.paid_date else None, _ts(at), number))
            self.log_event("settlement_reconciled", number,
                           {"previous_basis": dict(old) if old else None, "external_paid": str(external),
                            "local_applied": str(local), "previous_balance": str(inv.balance), "balance": str(balance), "by": by}, at)
            self.refresh_reminders(at=at)

    def _settled_invoice(self, c: sqlite3.Connection, inv: Invoice, at: dt.datetime | None) -> Invoice:
        if (inv.amount < 0 or c.execute("SELECT 1 FROM credit_sources WHERE number=?", (inv.number,)).fetchone()) and self._applied_total(c, inv.number) is None:
            return inv  # credit lifecycle is separate from receipt allocations
        if inv.amount >= 0 and inv.balance < 0:
            self._record_basis_surplus(inv, inv.balance, "direct_model", at)
        prior = c.execute("SELECT * FROM invoices WHERE number=?", (inv.number,)).fetchone()
        if prior and prior["status"] == "void":
            inv = replace(inv, balance=Decimal("0.00"), status="void")
        basis = c.execute("SELECT * FROM invoice_settlements WHERE number=?", (inv.number,)).fetchone()
        local = self._applied_total(c, inv.number) or Decimal("0")
        exported = max(Decimal("0"), inv.amount - inv.balance)
        reason = basis["reason"] if basis else ""
        external = Decimal(basis["external_paid"]) if basis and basis["external_paid"] is not None else None
        if prior and local and (inv.amount < 0 or prior["currency"] != inv.currency or prior["customer_id"] != inv.customer_id):
            reason = self._settlement_reason("invoice currency or customer changed after a local allocation")
        elif basis and external is None:
            reason = self._settlement_reason("historical imported payments are unknown")
        elif not reason:
            if not local:
                closed = prior and prior["status"] == "paid" and Decimal(prior["balance"]) == 0
                if external is not None and exported < external and not (closed and inv.amount == Decimal(prior["amount"])):
                    reason = self._settlement_reason("the export reduces a previously recorded external settlement")
                else:
                    external = exported
                    # A stale export cannot reopen a known fully settled invoice.
                    if closed:
                        external = max(external, Decimal(prior["amount"]))
            elif inv.balance == 0:
                external = max(external or Decimal("0"), inv.amount - local)
            elif basis and exported != Decimal(basis["export_settled"]):
                if inv.amount - (external or Decimal("0")) - local > 0:
                    reason = self._settlement_reason("the export's settlement changed and may overlap local payments")
        if reason and prior:
            kept = _invoice(prior)  # no financial/ownership mutation while unresolved
        else:
            external = external if external is not None else exported
            signed = inv.amount - external - local
            if signed < 0:
                self._record_basis_surplus(inv, signed, "settlement_basis", at)
            balance = max(Decimal("0.00"), signed)
            if balance != inv.balance:
                self.log_event("import_kept_payment", inv.number, {"export_balance": str(inv.balance), "kept_balance": str(balance),
                               "applied": str(local), "external_paid": str(external), "currency": inv.currency}, at)
            paid = inv.paid_date or (_d(prior["paid_date"]) if prior else None)
            kept = replace(inv, balance=balance, status="paid" if balance == 0 and inv.status != "void" else inv.status,
                           paid_date=paid if balance == 0 else inv.paid_date)
        c.execute("INSERT INTO invoice_settlements VALUES (?, ?, ?, ?, ?) ON CONFLICT(number) DO UPDATE SET external_paid=excluded.external_paid, export_settled=excluded.export_settled, reason=excluded.reason, updated_at=excluded.updated_at",
                  (inv.number, str(external) if external is not None else None, str(exported), reason, _ts(at)))
        if reason:
            self.log_event("settlement_review", inv.number, {"reason": reason, "incoming_amount": str(inv.amount),
                           "incoming_balance": str(inv.balance), "incoming_currency": inv.currency, "incoming_customer": inv.customer_id}, at)
        return kept

    def upsert_invoices(self, invoices: Iterable[Invoice], at: dt.datetime | None = None) -> int:
        """Retain imported settlements separately from local allocations; hold ambiguous overlap."""
        n = 0
        with self._tx() as c:
            self._remember_return_scope(at)
            self._remember_customer_identity_scope(at)
            for inv in invoices:
                inv = self._settled_invoice(c, inv, at)
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
            if self._depth == 1:
                self.refresh_reminders(at=at)
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

    def _group_dismissed(self, root: str) -> bool:
        return any(self._receipt_root(r["payment_id"]) == root for r in self._conn.execute("SELECT payment_id FROM dismissed"))

    def _placement_roots(self) -> set[str]:
        return {self._receipt_root(r["canonical_id"]) for r in self._conn.execute("SELECT canonical_id FROM receipt_placement WHERE confirmed=0")}

    def _conflicting_receipt_evidence(self, root: str) -> bool:
        members = [p for p in self.list_payments() if self._receipt_root(p.id) == root]
        named = {n for p in members for n in invoices_named(p.text, self.list_invoices())}
        payers = {" ".join(p.counterparty.split()).casefold() for p in members if p.counterparty.strip()}
        # Matching can discover new invoice numbers in reference/memo after a later import.
        # Preserve differing matching text before all invoices are known; identity alone
        # is never authority to pick which observation's placement evidence is correct.
        texts = {" ".join(p.text.split()).casefold() for p in members if p.text.strip()}
        return len(named) > 1 or len(payers) > 1 or len(texts) > 1

    def receipt_placement_reviews(self) -> list[dict]:
        return [{"canonical": root, "snapshot": self.overlap_snapshot(root, root),
                 "rows": [{"id": p.id, "reference": p.reference, "payer": p.counterparty, "date": p.date.isoformat(), "amount": str(p.amount), "currency": p.currency,
                           "allocations": [{"invoice": r["invoice_number"], "amount": r["amount"]} for r in self._conn.execute("SELECT * FROM matches WHERE payment_id=?", (p.id,))],
                           "applied": str(self.applied_from(p.id))}
                          for p in self.list_payments() if self._receipt_root(p.id) == root],
                 "message": "receipt allocation review required: identity is the same but invoice/payer evidence conflicts; place unused money with Apply/Dismiss, or confirm existing completed allocations with /cashdesk receipt-keep"}
                for root in sorted(self._placement_roots() - self._blocked_receipts())]

    def _finish_receipt_placement(self, root: str, by: str, at: dt.datetime | None) -> None:
        p = self.get_payment(root)
        if root in self._placement_roots() and (self._group_dismissed(root) or self.applied_from(root) >= p.amount):
            for row in self._conn.execute("SELECT canonical_id FROM receipt_placement").fetchall():
                if self._receipt_root(row["canonical_id"]) == root:
                    self._conn.execute("UPDATE receipt_placement SET confirmed=1, at=? WHERE canonical_id=?", (_ts(at), row["canonical_id"]))
            self.log_event("receipt_placement_confirmed", root, {"by": by, "applied": str(self.applied_from(root)), "dismissed": self._group_dismissed(root)}, at)

    def confirm_receipt_placement(self, payment_id: str, *, expected_snapshot: str, by: str = "", at: dt.datetime | None = None) -> None:
        with self._tx():
            root = self._receipt_root(payment_id)
            if expected_snapshot != self.overlap_snapshot(root, root):
                raise ConflictError("receipt snapshot changed; read /cashdesk overlaps again")
            if root not in self._placement_roots():
                raise ConflictError("no receipt allocation review is pending")
            self._guard_receipt(root, allow_dismissed=True)
            p = self.get_payment(root)
            if self.applied_from(root) < p.amount and not self._group_dismissed(root):
                raise ConflictError("place unused receipt money with Apply or Dismiss before confirming its history")
            self._finish_receipt_placement(root, by, at)
            self.refresh_reminders(at=at)

    def _receipt_root(self, payment_id: str) -> str:
        seen = set()
        while payment_id not in seen:
            seen.add(payment_id)
            row = self._conn.execute("SELECT canonical_id FROM receipt_aliases WHERE alias_id=?", (payment_id,)).fetchone()
            if row is None:
                return payment_id
            payment_id = row["canonical_id"]
        raise ConflictError("receipt identity history contains a cycle; stop and review the ledger")

    def _discover_overlaps(self, at: dt.datetime | None = None) -> int:
        buckets = {}
        for p in self.list_payments():
            if p.amount > 0:
                buckets.setdefault((p.date, p.amount, p.currency), []).append(p)
        added = 0
        for payments in buckets.values():
            for left, right in itertools.combinations(payments, 2):
                left_scope, right_scope = self._declared_receipt_account(left.id), self._declared_receipt_account(right.id)
                if left_scope and right_scope and left_scope.split(":")[0] == right_scope.split(":")[0] and left_scope.split("|")[0] != right_scope.split("|")[0]:
                    continue
                if left.identity_origin == right.identity_origin == "provided":
                    continue
                if self._separate_lines_of_one_statement(left, right):
                    continue
                id1, id2 = sorted((left.id, right.id))
                cur = self._conn.execute("INSERT OR IGNORE INTO receipt_pairs VALUES (?, ?, 'pending', '', ?)", (id1, id2, _ts(at)))
                if cur.rowcount:
                    added += 1
                    self.log_event("receipt_overlap", id1, {"other": id2, "reason": "same booking date, amount and currency; identity not established"}, at)
                    if self._identical_in_overlapping_statements(left, right, payments):
                        # The same line in this week's and last week's download: one receipt, no question.
                        self.resolve_overlap(id1, id2, "same", expected_snapshot=self.overlap_snapshot(id1, id2),
                                             by="automatic: identical line in two overlapping statements", at=at)
        return added

    @staticmethod
    def _separate_lines_of_one_statement(left, right) -> bool:
        """Two lines of the same statement with different payers and different references are two
        payments (three customers each paying 99.00 on one day), not one receipt seen twice."""
        key = lambda p: p.id.rsplit("@", 1)[1] if p.identity_origin == "derived" and "@" in p.id else ""
        if not key(left) or key(left) != key(right):
            return False
        norm = lambda s: " ".join((s or "").casefold().split())
        payers, refs = (norm(left.counterparty), norm(right.counterparty)), (norm(left.reference), norm(right.reference))
        return all(payers) and payers[0] != payers[1] and all(refs) and refs[0] != refs[1]

    def _identical_in_overlapping_statements(self, left, right, bucket) -> bool:
        """Two rows with identical date, amount, currency, payer, reference and memo from two
        different statement files, each the only such row in its file: one bank line seen twice.
        Two identical rows inside one file stay two receipts and still get a question."""
        statement = lambda p: p.id.rsplit("@", 1)[1] if p.identity_origin == "derived" and "@" in p.id else ""
        if not statement(left) or not statement(right) or statement(left) == statement(right):
            return False
        if self._receipt_root(left.id) == self._receipt_root(right.id):
            return False  # already one receipt (a third rolling download)
        if self._receipt_signature(left) != self._receipt_signature(right):
            return False
        if self.applied_from(left.id) > 0 and self.applied_from(right.id) > 0:
            return False
        sig = self._receipt_signature(left)
        for file_key in (statement(left), statement(right)):
            if sum(1 for p in bucket if statement(p) == file_key and self._receipt_signature(p) == sig) != 1:
                return False
        # The raw line must be identical, running balance included: two equal payments in two
        # "new since last time" downloads differ there, or not at all, so ask unless it is identical.
        raws = []
        for p in (left, right):
            rows = {r["raw"] for r in self._conn.execute("SELECT raw FROM receipt_observations WHERE payment_id=?", (p.id,))}
            if len(rows) != 1:
                return False
            raws.append(rows.pop())
        if raws[0] != raws[1]:
            return False
        # The two downloads must really overlap: more than one shared day around this line.
        ranges = []
        for file_key in (statement(left), statement(right)):
            dates = [p.date for p in self.list_payments() if statement(p) == file_key]
            ranges.append((min(dates), max(dates)))
        start, end = max(r[0] for r in ranges), min(r[1] for r in ranges)
        return start < end and start <= left.date <= end

    def _pending_receipt_pairs(self) -> set[tuple[str, str]]:
        pending, distinct = set(), set()
        for row in self._conn.execute("SELECT * FROM receipt_pairs"):
            roots = tuple(sorted((self._receipt_root(row["id1"]), self._receipt_root(row["id2"]))))
            if roots[0] == roots[1]:
                continue
            if row["decision"] == "distinct":
                distinct.add(roots)
            elif row["decision"] == "pending":
                pending.add(roots)
        return pending - distinct

    def _blocked_receipts(self) -> set[str]:
        roots = {p for pair in self._pending_receipt_pairs() for p in pair}
        conflict_rows = self._conn.execute("SELECT * FROM receipt_conflicts WHERE decision='pending'").fetchall()
        roots |= {self._receipt_root(row[key]) for row in conflict_rows for key in ("original_id", "observation_id")}
        for row in conflict_rows:
            head = self._conn.execute("SELECT canonical_id FROM receipt_heads WHERE original_id=?", (row["original_id"],)).fetchone()
            if head: roots.add(self._receipt_root(self._receipt_head(row["original_id"])))
        return {p.id for p in self.list_payments() if self._receipt_root(p.id) in roots}

    def _guard_receipt(self, payment_id: str, *, allow_dismissed: bool = False) -> None:
        if self._bank_evidence_pending():
            raise ConflictError(self._bank_evidence_reason())
        root = self._receipt_root(payment_id)
        if root != payment_id:
            raise ConflictError(f"receipt is a confirmed duplicate of {root}; use that original receipt, not this observation")
        if payment_id in self._receipt_update_ids():
            raise ConflictError("receipt update conflict: verify /cashdesk receipt-updates before applying or dismissing")
        if payment_id in self._blocked_receipts():
            raise ConflictError("possible receipt overlap; read /cashdesk overlaps and verify same or distinct before applying or dismissing")
        if not allow_dismissed and self._group_dismissed(root):
            raise ConflictError("receipt identity group was dismissed; it cannot be applied")

    def payment_overlaps(self) -> list[dict]:
        """Identity questions, not cash totals. All original rows and allocation history remain."""
        out = []
        for id1, id2 in sorted(self._pending_receipt_pairs()):
            rows = []
            for p in self.list_payments():
                if self._receipt_root(p.id) not in (id1, id2):
                    continue
                meta = self._conn.execute("SELECT * FROM payment_identity WHERE payment_id=?", (p.id,)).fetchone()
                rows.append({"id": p.id, "canonical": self._receipt_root(p.id), "date": p.date.isoformat(),
                             "amount": str(p.amount), "currency": p.currency, "reference": p.reference,
                             "counterparty": p.counterparty, "memo": p.memo, "source": p.source,
                             "origin": p.identity_origin, "file": meta["file"] if meta else "legacy/unknown",
                             "applied": str(self.applied_from(p.id)),
                             "allocations": [{"invoice": r["invoice_number"], "amount": r["amount"]} for r in self._conn.execute("SELECT * FROM matches WHERE payment_id=?", (p.id,))],
                             "dismissed": bool(self._conn.execute("SELECT 1 FROM dismissed WHERE payment_id=?", (p.id,)).fetchone())})
            out.append({"id1": id1, "id2": id2, "rows": rows, "snapshot": self.overlap_snapshot(id1, id2),
                        "message": "possible receipt overlap: observed rows are not confirmed additional cash; verify the bank evidence and choose same or distinct"})
        return out

    def overlap_snapshot(self, id1: str, id2: str) -> str:
        p = self.get_payment(id1)
        if p is None or self.get_payment(id2) is None:
            raise NotFoundError("receipt identity review needs two stored receipts")
        cohort = [r for r in self.list_payments() if (r.date, r.amount, r.currency) == (p.date, p.amount, p.currency)]
        ids = {r.id for r in cohort}
        matches = [dict(r) for r in self._conn.execute("SELECT * FROM matches ORDER BY payment_id, invoice_number") if r["payment_id"] in ids]
        payload = {"ids": sorted((self._receipt_root(id1), self._receipt_root(id2))),
                   "rows": [repr(r) for r in cohort], "matches": matches,
                   "invoices": [repr(self.get_invoice(n)) for n in sorted({m["invoice_number"] for m in matches})],
                   "pairs": [dict(r) for r in self._conn.execute("SELECT * FROM receipt_pairs ORDER BY id1,id2") if r["id1"] in ids or r["id2"] in ids],
                   "aliases": [dict(r) for r in self._conn.execute("SELECT * FROM receipt_aliases ORDER BY alias_id") if r["alias_id"] in ids],
                   "placement": [dict(r) for r in self._conn.execute("SELECT * FROM receipt_placement ORDER BY canonical_id") if r["canonical_id"] in ids],
                   "dismissed": [dict(r) for r in self._conn.execute("SELECT * FROM dismissed ORDER BY payment_id") if r["payment_id"] in ids]}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def resolve_overlap(self, id1: str, id2: str, decision: str, *, expected_snapshot: str,
                        by: str = "", at: dt.datetime | None = None) -> dict:
        if decision not in ("same", "distinct"):
            raise ConflictError("choose same or distinct after verifying the receipt evidence")
        refusal = None
        canonical = None
        with self._tx() as c:
            if expected_snapshot != self.overlap_snapshot(id1, id2):
                raise ConflictError("receipt snapshot changed; read /cashdesk overlaps again")
            left, right = self._receipt_root(id1), self._receipt_root(id2)
            pair = tuple(sorted((left, right)))
            if pair not in self._pending_receipt_pairs():
                raise ConflictError("receipt identity decision is already recorded or contradicts a confirmed same/distinct decision")
            if decision == "same" and self.applied_from(left) > 0 and self.applied_from(right) > 0:
                refusal = "both receipts already have allocations; they remain paused and need an audited allocation correction before a same-receipt resolution; no financial history was changed"
                self.log_event("receipt_identity_conflict", id1, {"other": id2, "attempted_decision": decision, "by": by, "reason": refusal}, at)
            else:
                relevant = [(r["id1"], r["id2"]) for r in c.execute("SELECT * FROM receipt_pairs")
                            if tuple(sorted((self._receipt_root(r["id1"]), self._receipt_root(r["id2"])))) == pair]
                c.executemany("UPDATE receipt_pairs SET decision=?, reviewer=?, at=? WHERE id1=? AND id2=?",
                              [(decision, by, _ts(at), a, b) for a, b in relevant])
                if decision == "same":
                    if self.applied_from(left) > 0:
                        canonical = left
                    elif self.applied_from(right) > 0:
                        canonical = right
                    else:
                        canonical = min((left, right), key=lambda pid: c.execute("SELECT rowid FROM payments WHERE id=?", (pid,)).fetchone()[0])
                    alias = right if canonical == left else left
                    c.execute("INSERT INTO receipt_aliases VALUES (?, ?, ?)", (alias, canonical, _ts(at)))
                    if self._conflicting_receipt_evidence(canonical) and (not self._group_dismissed(canonical) or self.applied_from(canonical) > 0):
                        c.execute("INSERT OR REPLACE INTO receipt_placement VALUES (?, 0, ?)", (canonical, _ts(at)))
                self.log_event("receipt_identity_resolved", id1, {"other": id2, "roots": pair, "decision": decision, "canonical": canonical, "by": by}, at)
            self.refresh_reminders(at=at)
        if refusal:
            raise ConflictError(refusal)
        return {"decision": decision, "canonical": canonical}

    def _overlap_holds(self) -> dict[str, str]:
        blocked = self._blocked_receipts()
        placements = self._placement_roots() - blocked
        blocked |= {p.id for p in self.list_payments() if self._receipt_root(p.id) in placements}
        if not blocked:
            return {}
        invoices = self.list_invoices()
        customers = {c.id: c for c in self.list_customers()}
        holds = {r["invoice_number"]: "possible receipt overlap: an allocated receipt has an unresolved identity; read /cashdesk overlaps"
                 for r in self._conn.execute("SELECT * FROM matches") if r["payment_id"] in blocked}
        update_ids = self._receipt_update_ids()
        for p in self.list_payments():
            if p.id not in blocked:
                continue
            named = set(invoices_named(p.text, invoices))
            payers = {c.id for c in customers.values() if p.counterparty and
                      (p.counterparty.casefold() == c.name.casefold() or payer_may_fit(c.name, p.counterparty))}
            plausible = {inv.number for inv in invoices if inv.is_open and inv.currency == p.currency and
                         (inv.number in named or inv.customer_id in payers or inv.balance == p.amount)} | named
            if not plausible:
                plausible = {inv.number for inv in invoices if inv.is_open and inv.currency == p.currency}
            for number in plausible:
                holds[number] = ("receipt update conflict: verify /cashdesk receipt-updates before trusting receipt or payment claims" if p.id in update_ids else "possible receipt overlap: a receipt that may pay this invoice needs a same/distinct decision; read /cashdesk overlaps")
        for root in placements:
            related = {p.id for p in self.list_payments() if self._receipt_root(p.id) == root}
            numbers = {n for p in self.list_payments() if p.id in related for n in invoices_named(p.text, invoices)}
            numbers |= {r["invoice_number"] for r in self._conn.execute("SELECT * FROM matches") if r["payment_id"] in related}
            payers = {c.id for p in self.list_payments() if p.id in related for c in customers.values() if p.counterparty and payer_may_fit(c.name, p.counterparty)}
            for inv in invoices:
                if inv.number in numbers or inv.customer_id in payers:
                    holds[inv.number] = "receipt allocation review required: same receipt has conflicting placement evidence; use Apply/Dismiss or /cashdesk receipt-keep after checking existing completed allocations"
        return holds

    def _record_return(self, key, evidence, at=None):
        identity = hashlib.sha256(key.encode()).hexdigest()
        if self._conn.execute("SELECT 1 FROM bank_return_reviews WHERE id=?", (identity,)).fetchone():
            return
        scope = self._return_scope(evidence)
        self._conn.execute("INSERT INTO bank_return_reviews VALUES (?,?,?,?,NULL,NULL,NULL)",
                           (identity, json.dumps(evidence,sort_keys=True), json.dumps(scope,sort_keys=True), _ts(at)))
        self.log_event("bank_return_observed", identity, {"financial_rows_unchanged": True, "scope": scope}, at)

    @staticmethod
    def _payment_return_evidence(p, **extra):
        return {"payment_id":p.id,"date":p.date.isoformat(),"amount":str(p.amount),"currency":p.currency,
                "payer":p.counterparty,"reference":p.reference,"memo":p.memo, **extra}

    def _return_scope(self, evidence):
        """Candidate scope is a UNION of hints, never a guessed reversal.

        Payer/reference hints protect the whole customer's collection. Amount
        alone protects every plausible receipt; absent identity/amount widens
        the hold to the currency ledger. No date or account guess excludes cash.
        """
        invoices = self.list_invoices()
        text = " ".join(str(evidence.get(k) or "") for k in ("reference","memo"))
        numbers = set(invoices_named(text,invoices))
        customers = {i.customer_id for i in invoices if i.number in numbers}
        customers.update(c.id for c in self.list_customers() if evidence.get("payer") and payer_may_fit(c.name,evidence["payer"]))
        identity_hint = bool(customers)
        amount = abs(Decimal(evidence["amount"])) if evidence.get("amount") else None
        currency = evidence.get("currency")
        receipts = set()
        matches = self.list_matches()
        for p in self.list_payments():
            if p.amount <= 0 or p.id == evidence.get("payment_id") or is_return(p.text):
                continue
            allocations = [m for m in matches if m.payment_id==p.id]
            by_identity = bool(evidence.get("payer")) and payer_may_fit(p.counterparty,evidence["payer"])
            by_reference = bool(evidence.get("raw_id")) and evidence["raw_id"] in (p.id, self._receipt_root(p.id))
            by_owner = any(m.invoice_number in numbers or any(i.number==m.invoice_number and i.customer_id in customers for i in invoices) for m in allocations)
            by_amount = (amount is None or p.amount==amount) and (not currency or p.currency==currency)
            if by_identity or by_reference or by_owner or (not customers and by_amount):
                receipts.add(p.id)
                numbers.update(m.invoice_number for m in allocations)
        customers.update(i.customer_id for i in invoices if i.number in numbers)
        # Settlement safety may retain an older operative owner while recording
        # the incoming identity in its audit event. Protect both identities.
        for event in self._conn.execute("SELECT ref,detail FROM events WHERE kind='settlement_review'"):
            if event["ref"] in numbers:
                owner=json.loads(event["detail"]).get("incoming_customer")
                if owner:customers.add(owner)
        uncertain = not customers or (not identity_hint and amount is None)
        if uncertain:
            customers.update(i.customer_id for i in invoices if not currency or i.currency==currency)
        numbers.update(i.number for i in invoices if i.customer_id in customers)
        return {"receipts":sorted(receipts),"customer_ids":sorted(customers),"affected_invoices":sorted(numbers),
                "scope":"uncertain" if uncertain else "candidate"}

    def _migrate_returns(self):
        with self._tx():
            for p in self.list_payments():
                if is_return(p.text):
                    self._record_return("payment:"+p.id, self._payment_return_evidence(p,raw=p.text,origin="retained payment"))
            # Historical lifecycle rows have raw evidence but no extracted hints.
            # Preserve that uncertainty instead of silently ignoring an old return.
            for row in self._conn.execute("SELECT * FROM receipt_observations WHERE status IN ('returned','reversed','reverted')").fetchall():
                if self.get_payment(row["payment_id"]):
                    continue  # The existing receipt-update workflow owns linked lifecycle evidence.
                key="observation:"+row["id"]
                if not self._conn.execute("SELECT 1 FROM bank_return_reviews WHERE id=?",(hashlib.sha256(key.encode()).hexdigest(),)).fetchone():
                    self._record_return(key, {"raw":row["raw"],"status":row["status"],"raw_id":row["raw_id"],"account":row["account"],"file":row["file"],"fingerprint":row["fingerprint"]})
            if self._conn.execute("SELECT 1 FROM bank_return_reviews WHERE resolved_at IS NULL").fetchone():
                self.refresh_reminders()

    def _remember_return_scope(self, at=None):
        # Retain every previously affected identity while the question is open.
        # Evidence itself is immutable; expansions have their own audit events.
        for q in self.return_reviews(include_snapshot=False):
            scope={k:q[k] for k in ("receipts","customer_ids","affected_invoices","scope")}
            saved=self._conn.execute("SELECT initial_scope FROM bank_return_reviews WHERE id=?",(q["id"],)).fetchone()
            if json.loads(saved["initial_scope"])!=scope:
                self._conn.execute("UPDATE bank_return_reviews SET initial_scope=? WHERE id=?",(json.dumps(scope,sort_keys=True),q["id"]))
                self.log_event("bank_return_scope_extended",q["id"],scope,at)

    def return_snapshot(self):
        tables=("bank_return_reviews","dismissed","payments","payment_identity","receipt_observations","receipt_aliases","receipt_heads","matches","invoices","invoice_settlements","customers","drafts","approvals","sends","events")
        payload={t:[dict(r) for r in self._conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
        return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()

    def return_reviews(self, *, include_snapshot=True):
        questions=[]
        for row in self._conn.execute("SELECT * FROM bank_return_reviews WHERE resolved_at IS NULL ORDER BY at,id"):
            evidence=json.loads(row["evidence"])
            scope=self._return_scope(evidence)
            prior=json.loads(row["initial_scope"])
            for key in ("receipts","customer_ids","affected_invoices"):
                scope[key]=sorted(set(scope[key])|set(prior[key]))
            # A later customer reassignment cannot erase earlier ownership.
            owners=set(scope["customer_ids"])
            owners.update(i.customer_id for i in self.list_invoices() if i.number in scope["affected_invoices"])
            scope["customer_ids"]=sorted(owners)
            scope["affected_invoices"]=sorted(set(scope["affected_invoices"])|{i.number for i in self.list_invoices() if i.customer_id in owners})
            questions.append({"id":row["id"],"evidence":evidence, "quarantined":self._plausibly_returned(evidence,scope["receipts"]), "bank_observations":[dict(r) for r in self._conn.execute("SELECT * FROM receipt_observations WHERE payment_id=? ORDER BY id",(evidence.get("payment_id", ""),))], **scope})
        if questions and include_snapshot:
            token=self.return_snapshot()
            for q in questions:q["snapshot"]=token
        return questions

    def return_holds(self):
        return {n:"returned_payment: returned payment review; verify /cashdesk returns against bank and accounting evidence before collection or acknowledgement"
                for q in self.return_reviews(include_snapshot=False) for n in q["affected_invoices"]}

    def confirm_return_unrelated(self, identity, *, expected_snapshot, reason, by="", at=None):
        """Human attests this observation does not undermine retained receipts.

        A genuine bounced allocation stays held for audited correction; this
        action cannot record a reversal, change balances or restore approval.
        """
        with self._tx() as c:
            if (not c.execute("SELECT 1 FROM bank_return_reviews WHERE id=? AND resolved_at IS NULL",(identity,)).fetchone()
                    or not expected_snapshot or expected_snapshot!=self.return_snapshot() or not reason.strip() or not by.strip()):
                raise ConflictError("returned payment review: snapshot changed or verified bank explanation missing; read /cashdesk returns again")
            c.execute("UPDATE bank_return_reviews SET resolved_at=?,resolved_by=?,resolution=? WHERE id=?",(_ts(at),by,reason,identity))
            self.log_event("bank_return_verified_unrelated",identity,{"snapshot":expected_snapshot,"by":by,"reason":reason,"financial_rows_unchanged":True},at)
            self.refresh_reminders(at=at)

    def confirm_return_reverses(self, identity, payment_id, *, expected_snapshot, reason, by="", at=None):
        """Human attests the return gave back this still-unallocated receipt.

        Both raw rows stay; the receipt is closed as not-cash (audited dismissal),
        the question resolves and the invoice keeps its full open balance. An
        allocated receipt is refused: that needs an audited correction.
        """
        with self._tx() as c:
            if (not c.execute("SELECT 1 FROM bank_return_reviews WHERE id=? AND resolved_at IS NULL",(identity,)).fetchone()
                    or not expected_snapshot or expected_snapshot!=self.return_snapshot() or not str(reason).strip() or not str(by).strip()):
                raise ConflictError("returned payment review: snapshot changed or verified bank explanation missing; read /cashdesk returns again")
            q = next(q for q in self.return_reviews(include_snapshot=False) if q["id"]==identity)
            p = self.get_payment(payment_id)
            if p is not None and p.amount > 0 and self.applied_from(payment_id) > 0:
                raise ConflictError(f"returned payment review: {payment_id} already has allocations; a genuine bounced allocation needs an audited correction, not this command")
            if payment_id not in q["quarantined"]:
                raise ConflictError(f"returned payment review: {payment_id} is not a plausible unallocated receipt for this return "
                                    "(same amount and currency, nothing allocated); read /cashdesk returns")
            self._guard_receipt(payment_id, allow_dismissed=True)
            c.execute("INSERT OR REPLACE INTO dismissed (payment_id, reason, at) VALUES (?, ?, ?)",
                      (payment_id, f"returned by the bank (verified by {by}): {reason}", _ts(at)))
            c.execute("UPDATE bank_return_reviews SET resolved_at=?,resolved_by=?,resolution=? WHERE id=?",
                      (_ts(at),by,f"reverses {payment_id}: {reason}",identity))
            self.log_event("bank_return_reverses_unapplied",identity,{"payment_id":payment_id,"snapshot":expected_snapshot,"by":by,"reason":reason,
                                                                     "raw_rows_retained":True,"allocations_unchanged":True},at)
            self.refresh_reminders(at=at)

    def verified_matches(self) -> list[Match]:
        if self._bank_evidence_pending():
            return []
        blocked = self._blocked_receipts() | {p.id for p in self.list_payments() if self._receipt_root(p.id) in self._placement_roots()}
        held = {**self.invoice_import_holds(), **self.return_holds(), **self.customer_identity_holds()}
        customers = self.surplus_customer_holds()
        held.update({inv.number: "customer surplus review" for inv in self.list_invoices() if inv.customer_id in customers})
        return [m for m in self.list_matches() if m.payment_id not in blocked and m.invoice_number not in held]

    def _receipt_cohort_ids(self, ids):
        ids = set(ids)
        roots = {self._receipt_root(i) for i in ids} | {self._receipt_root(self._receipt_head(i)) for i in ids}
        return ids | roots | {p.id for p in self.list_payments() if self._receipt_root(p.id) in roots}

    def _receipt_update_ids(self):
        ids = {r[k] for r in self._conn.execute("SELECT original_id,observation_id FROM receipt_conflicts WHERE decision='pending'")
               for k in ("original_id","observation_id")}
        return self._receipt_cohort_ids(ids) if ids else set()

    def _declared_receipt_account(self, payment_id):
        # A copied adverse observation is evidence, not an account assignment.
        for row in self._conn.execute("SELECT raw_id,account FROM receipt_observations WHERE payment_id=? AND account!=''",(payment_id,)):
            digest = hashlib.sha256(row["account"].encode()).hexdigest()[:16]
            scoped = "acct-"+digest+":"+row["raw_id"]
            if row["raw_id"] and (payment_id == scoped or payment_id.startswith(scoped+"@obs-")):
                return row["account"]
            p = self.get_payment(payment_id)
            if p and p.identity_origin=="derived" and "@acct-"+digest+"@" in payment_id:
                return row["account"]
        return ""

    def _receipt_head(self, original):
        seen = set()
        while original not in seen:
            seen.add(original)
            row = self._conn.execute("SELECT canonical_id FROM receipt_heads WHERE original_id=?",(original,)).fetchone()
            if row is None: return original
            original = row["canonical_id"]
        raise ConflictError("receipt update conflict: correction history contains a cycle; verify the ledger")

    @staticmethod
    def _receipt_signature(p):
        return (p.date.isoformat(), str(p.amount), p.currency,
                " ".join(p.counterparty.casefold().split()), "".join(p.reference.casefold().split()),
                " ".join(p.memo.casefold().split()), p.source)

    def _ingest_bank_observations(self, result, file_name, at):
        c = self._conn
        added = 0
        previous_observations = {}
        for row in self._conn.execute("SELECT payment_id,raw,account FROM receipt_observations"):
            previous_observations.setdefault(row["raw"],[]).append(row)
        terminal = {"reverted", "reversed", "returned", "rejected", "refunded", "void", "voided", "cancelled", "canceled", "failed", "declined"}
        def record_observation(payment_id, item):
            payload = json.dumps([payment_id,item.raw_id,item.account,item.status,item.raw,result.fingerprint],ensure_ascii=False)
            oid = hashlib.sha256(payload.encode()).hexdigest()
            c.execute("INSERT OR IGNORE INTO receipt_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (oid,payment_id,item.raw_id,item.account,item.status,item.raw,result.fingerprint,file_name,_ts(at)))
            basis = (item.evidence or {}).get("currency_basis")
            if basis:
                c.execute("INSERT OR IGNORE INTO bank_currency_evidence VALUES (?,?,?,?,?,?)",
                          (oid,result.fingerprint,payment_id,basis["resolved"],json.dumps(basis,ensure_ascii=False),_ts(at)))
        def question(original, observed, item, reason):
            for payment_id in {original,observed}: record_observation(payment_id,item)
            c.execute("INSERT OR IGNORE INTO receipt_conflicts (original_id, observation_id, fingerprint, reason, account, status, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (original, observed, result.fingerprint, reason, item.account, item.status, _ts(at)))
            self.log_event("receipt_update_conflict", original, {"observation": observed, "reason": reason, "status": item.status}, at)
        from .importers import NONBOOKED_STATUS_GROUPS
        for item in result.bank_observations:
            if item.payment is None and item.status in NONBOOKED_STATUS_GROUPS[0]:
                ev = {k: v for k, v in (item.evidence or {}).items() if k != "currency_basis"}
                pid = hashlib.sha256(json.dumps([item.account, item.raw_id, item.raw], ensure_ascii=False).encode()).hexdigest()[:24]
                c.execute("INSERT OR IGNORE INTO pending_receipts VALUES (?, ?, ?, ?)",
                          (pid, json.dumps({**ev, "status": item.status}, ensure_ascii=False), file_name, _ts(at)))
            p = item.payment
            if p:
                prior_currency = []
                prior_basis = []
                signature = self._receipt_signature(p)
                for observation in previous_observations.get(item.raw,()):
                    if observation["account"] and item.account and observation["account"] != item.account:
                        continue
                    old = self.get_payment(self._receipt_head(observation["payment_id"]))
                    if old and old.currency != p.currency:
                        old_signature = self._receipt_signature(old)
                        if old_signature[:2]+old_signature[3:] == signature[:2]+signature[3:]:
                            prior_currency.append(repr(old))
                            prior_basis.extend(self.receipt_currency_evidence(old.id))
                if prior_currency:
                    basis = (item.evidence or {}).get("currency_basis",{})
                    evidence = {"kind":"currency","payers":[p.counterparty],"references":[p.reference],
                                "currency_basis":basis,"prior_receipts":sorted(set(prior_currency)),"prior_currency_evidence":prior_basis}
                    detail = "bank currency evidence conflicts: retained receipt currency disagrees for the same raw bank row; verify original statement currency and allocated history; no automatic undo"
                    self._retain_bank_evidence(BankEvidenceIssue(item.row,item.raw,detail,json.dumps(evidence,ensure_ascii=False)),result.fingerprint,file_name,at)
            stable = ("acct-" + hashlib.sha256(item.account.encode()).hexdigest()[:16] + ":" + item.raw_id
                      if item.raw_id and item.account else item.raw_id)
            if p and p.identity_origin == "derived":
                stable = p.id + ("@acct-"+hashlib.sha256(item.account.encode()).hexdigest()[:16] if item.account else "") + "@" + result.fingerprint[:16]
            current_id = self._receipt_head(stable) if stable else ""
            prior = self.get_payment(current_id) if current_id else None
            linked = prior or (item.account and item.raw_id and self.get_payment(item.raw_id)) or (not item.account and item.raw_id and any(
                self.get_payment(self._receipt_head(r["payment_id"])) for r in c.execute("SELECT DISTINCT payment_id FROM receipt_observations WHERE raw_id=?",(item.raw_id,))))
            if not p and not linked and (item.status in {"returned","reversed","reverted"} or is_return(" ".join(str(v) for key,v in (item.evidence or {}).items() if key!="currency_basis"))):
                # Use exactly the lifecycle observation identity for idempotent
                # migration; even rows without an ID remain durable questions.
                observed_id = ("acct-"+hashlib.sha256(item.account.encode()).hexdigest()[:16]+":"+item.raw_id if item.raw_id and item.account else item.raw_id)
                oid = hashlib.sha256(json.dumps([observed_id,item.raw_id,item.account,item.status,item.raw,result.fingerprint],ensure_ascii=False).encode()).hexdigest()
                self._record_return("observation:"+oid, {**(item.evidence or {}), "raw":item.raw,"raw_id":item.raw_id,"account":item.account,"status":item.status,"file":file_name,"fingerprint":result.fingerprint}, at)
            if not stable:
                continue  # Return rows without an ID already have durable review evidence.
            observation_id = current_id
            if p:
                incoming = replace(p, id=stable)
                if prior and (self._receipt_signature(prior) != self._receipt_signature(incoming) or (not item.account and self._declared_receipt_account(prior.id))):
                    observation_id = stable + "@obs-" + hashlib.sha256(repr((self._receipt_signature(incoming),result.fingerprint)).encode()).hexdigest()[:16]
                    added += self.add_payments([replace(p,id=observation_id)], at, fingerprint=result.fingerprint, file_name=file_name, discover=False)
                    question(current_id, observation_id, item, "provided receipt ID has changed financial, payer/reference or account-scope evidence")
                elif prior is None:
                    added += self.add_payments([incoming], at, fingerprint=result.fingerprint, file_name=file_name, discover=False)
                    prior = incoming
                    # Unknown historical scope must never become a second certain receipt.
                    legacy = self.get_payment(item.raw_id) if item.account and item.raw_id else None
                    if legacy and legacy.id != stable and legacy.identity_origin != "derived":
                        question(legacy.id, stable, item, "historical receipt account is unknown; verify same or distinct cash")
                    for old in c.execute("SELECT DISTINCT payment_id, account FROM receipt_observations WHERE raw_id=? AND account!=?", (item.raw_id,item.account)).fetchall():
                        candidate = self._receipt_head(old["payment_id"])
                        if item.raw_id and self.get_payment(candidate) and (not item.account or not old["account"] or
                                (item.account.startswith("account:") and old["account"].split("|")[0] == item.account.split("|")[0])):
                            question(candidate,stable,item,"account metadata changed or is missing; verify statement account identity")
                    if c.execute("SELECT 1 FROM receipt_observations WHERE raw_id=? AND (account=? OR account='') AND status IN ("+",".join("?" for _ in terminal)+")", (item.raw_id,item.account,*sorted(terminal))).fetchone() and item.raw_id:
                        question(stable,stable,item,"booked row conflicts with retained terminal evidence whose scope may match")
                    if c.execute("SELECT 1 FROM receipt_observations WHERE payment_id=? AND status IN ("+",".join("?" for _ in terminal)+")", (stable,*sorted(terminal))).fetchone():
                        question(stable,stable,item,"booked row conflicts with retained earlier terminal status")
            elif prior:
                question(current_id,current_id,item,"later non-booked status contradicts a retained receipt; verify settlement before using it")
            elif item.account and item.raw_id and self.get_payment(item.raw_id):
                question(item.raw_id,item.raw_id,item,"unscoped historical receipt has adverse account-scoped evidence")
                observation_id = item.raw_id
            if not p and not prior and not item.account:
                for old in c.execute("SELECT DISTINCT payment_id FROM receipt_observations WHERE raw_id=?",(item.raw_id,)).fetchall():
                    candidate = self._receipt_head(old["payment_id"])
                    if self.get_payment(candidate):
                        question(candidate,candidate,item,"unscoped status update may belong to this retained scoped receipt")
            record_observation(observation_id,item)
        if added: self._discover_overlaps(at)
        return added

    def receipt_update_snapshot(self, original):
        rows = [dict(r) for r in self._conn.execute("SELECT * FROM receipt_conflicts WHERE original_id=? ORDER BY observation_id, fingerprint", (original,))]
        related_ids = self._receipt_cohort_ids({original,*(row["observation_id"] for row in rows)})
        all_observations = self._conn.execute("SELECT * FROM receipt_observations ORDER BY id").fetchall()
        raw_ids = {r["raw_id"] for r in all_observations if r["payment_id"] in related_ids and r["raw_id"]}
        observations = [dict(r) for r in all_observations if r["payment_id"] in related_ids or (r["raw_id"] and r["raw_id"] in raw_ids)]
        ids = related_ids
        matches = [dict(r) for r in self._conn.execute("SELECT * FROM matches ORDER BY payment_id,invoice_number") if r["payment_id"] in ids]
        payments = [p for p in self.list_payments() if p.id in ids]
        numbers = {m["invoice_number"] for m in matches} | {n for p in payments for n in invoices_named(p.text,self.list_invoices())}
        state = {"payments":[repr(p) for p in payments], "invoices":[repr(self.get_invoice(n)) for n in sorted(numbers)],
                 "currency_evidence":[dict(row) for row in self._conn.execute("SELECT * FROM bank_currency_evidence ORDER BY observation_id") if row["payment_id"] in ids],
                 "dismissals":[dict(r) for r in self._conn.execute("SELECT * FROM dismissed ORDER BY payment_id") if r["payment_id"] in ids],
                 "heads":[dict(r) for r in self._conn.execute("SELECT * FROM receipt_heads ORDER BY original_id") if r["original_id"] in ids or r["canonical_id"] in ids],
                 "aliases":[dict(r) for r in self._conn.execute("SELECT * FROM receipt_aliases ORDER BY alias_id") if r["alias_id"] in ids or r["canonical_id"] in ids]}
        return hashlib.sha256(json.dumps([rows,observations,matches,state],sort_keys=True).encode()).hexdigest()[:16]

    def receipt_conflicts(self):
        out = []
        for row in self._conn.execute("SELECT * FROM receipt_conflicts WHERE decision='pending' ORDER BY original_id,observation_id"):
            original, observed = self.get_payment(self._receipt_root(self._receipt_head(row["original_id"]))), self.get_payment(row["observation_id"])
            out.append({"original":row["original_id"],"observation":row["observation_id"],"reason":row["reason"],"status":row["status"],
                        "account":row["account"],"before":repr(original),"after":repr(observed),
                        "allocations":[dict(r) for r in self._conn.execute("SELECT * FROM matches ORDER BY payment_id,invoice_number") if r["payment_id"] in self._receipt_cohort_ids({row["original_id"],row["observation_id"]})],
                        "dismissals":[dict(r) for r in self._conn.execute("SELECT * FROM dismissed ORDER BY payment_id") if r["payment_id"] in self._receipt_cohort_ids({row["original_id"],row["observation_id"]})],
                        "evidence":[dict(r) for r in self._conn.execute("SELECT raw,status,account,file FROM receipt_observations WHERE payment_id IN (?,?) OR (raw_id!='' AND raw_id IN (SELECT raw_id FROM receipt_observations WHERE payment_id IN (?,?))) ORDER BY at,id",(row["original_id"],row["observation_id"],row["original_id"],row["observation_id"]))],
                        "currency_evidence":self.receipt_currency_evidence(row["original_id"])+self.receipt_currency_evidence(row["observation_id"]),
                        "snapshot":self.receipt_update_snapshot(row["original_id"])})
        return out

    def resolve_receipt_update(self, original, observation, decision, *, expected_snapshot, by="", at=None):
        """A person verifies original cash, separate receipts, or an unallocated correction."""
        if decision not in ("keep", "distinct", "replace"):
            raise ConflictError("receipt update conflict: choose keep, distinct or replace after bank verification")
        with self._tx() as c:
            if expected_snapshot != self.receipt_update_snapshot(original):
                raise ConflictError("receipt update snapshot changed; read /cashdesk receipt-updates again")
            rows = c.execute("SELECT * FROM receipt_conflicts WHERE original_id=? AND observation_id=? AND decision='pending'",(original,observation)).fetchall()
            if not rows: raise ConflictError("receipt update conflict: no pending observation matches")
            active_original = self._receipt_head(original)
            left,right = self.get_payment(active_original),self.get_payment(observation)
            if decision != "keep":
                if original == observation or any(r["status"] not in ("booked", "completed", "complete", "posted", "settled", "cleared", "executed", "processed", "success", "succeeded", "successful", "done", "sent", "received", "paid", "gebucht", "zaúčtováno", "zauctovano", "provedeno", "verwerkt", "geboekt") for r in rows):
                    raise ConflictError("receipt update conflict: adverse status cannot create separate cash; keep only after bank verification, otherwise allocation reconciliation is required")
                if decision == "distinct" and any(r["account"] and "historical" not in r["reason"] and "metadata" not in r["reason"] for r in rows):
                    raise ConflictError("receipt update conflict: same scoped ID is not evidence of two receipts")
                if decision == "replace" and any(self.applied_from(p.id)>0 for p in self.list_payments() if self._receipt_root(p.id) in {self._receipt_root(original),self._receipt_root(active_original),self._receipt_root(observation)}):
                    raise ConflictError("receipt update conflict: allocated history requires verified allocation reconciliation; no automatic undo")
            if decision == "keep" and original != observation:
                if any(self.applied_from(pid)>0 for pid in self._receipt_cohort_ids({observation})):
                    raise ConflictError("receipt update conflict: allocated observation requires verified allocation reconciliation; no automatic undo")
                c.execute("INSERT OR REPLACE INTO dismissed VALUES (?, ?, ?)",(observation,"verified superseded observation",_ts(at)))
                if observation != active_original:
                    c.execute("INSERT OR REPLACE INTO receipt_heads VALUES (?,?)",(observation,active_original))
            if decision == "replace":
                c.execute("INSERT OR REPLACE INTO dismissed VALUES (?, ?, ?)",(original,"verified unallocated correction",_ts(at)))
                if active_original != original:
                    c.execute("INSERT OR REPLACE INTO dismissed VALUES (?, ?, ?)",(active_original,"verified unallocated correction",_ts(at)))
                c.execute("INSERT OR REPLACE INTO receipt_heads VALUES (?,?)",(original,observation))
            c.execute("UPDATE receipt_pairs SET decision='verified_update',reviewer=?,at=? WHERE (id1=? AND id2=?) OR (id1=? AND id2=?)",(by,_ts(at),original,observation,observation,original))
            c.execute("UPDATE receipt_conflicts SET decision=?,reviewer=?,at=? WHERE original_id=? AND observation_id=? AND decision='pending'",(decision,by,_ts(at),original,observation))
            self.log_event("receipt_update_resolved",original,{"observation":observation,"decision":decision,"by":by},at)
            self.refresh_reminders(at=at)

    def add_payments(self, payments: Iterable[Payment], at: dt.datetime | None = None, *,
                     fingerprint: str = "", file_name: str = "", discover: bool = True) -> int:
        """Insert new payments; an unchanged known id is idempotent. Returns how many were new."""
        n = 0
        with self._tx() as c:
            for p in payments:
                prior = self.get_payment(p.id)
                if prior and self._receipt_signature(prior) != self._receipt_signature(p):
                    raise ConflictError("receipt update conflict: this ID already has different evidence; import the bank CSV to retain and review the observation")
                cur = c.execute(
                    "INSERT OR IGNORE INTO payments (id, date, amount, currency, reference, counterparty, memo, source, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p.id, p.date.isoformat(), str(p.amount), p.currency, p.reference, p.counterparty, p.memo, p.source, _ts(at)),
                )
                n += cur.rowcount
                if is_return(p.text):
                    self._record_return("payment:"+p.id,self._payment_return_evidence(p,raw=p.text,file=file_name,fingerprint=fingerprint),at)
                if cur.rowcount:
                    c.execute("INSERT INTO payment_identity VALUES (?, ?, ?, ?, ?)",
                              (p.id, p.identity_origin, fingerprint, file_name, _ts(at)))
            if n and discover:
                self._discover_overlaps(at)
            if self._depth == 1:
                self.refresh_reminders(at=at)
        return n

    def get_payment(self, payment_id: str) -> Payment | None:
        row = self._conn.execute("SELECT p.*, COALESCE(i.origin,'unknown') AS identity_origin FROM payments p LEFT JOIN payment_identity i ON i.payment_id=p.id WHERE p.id = ?", (payment_id,)).fetchone()
        return _payment(row) if row else None

    def list_payments(self) -> list[Payment]:
        return [_payment(r) for r in self._conn.execute("SELECT p.*, COALESCE(i.origin,'unknown') AS identity_origin FROM payments p LEFT JOIN payment_identity i ON i.payment_id=p.id ORDER BY p.date, p.id").fetchall()]

    def unapplied_payments(self) -> list[Payment]:
        """Receipts with money not yet applied and not dismissed by a person: what the next
        matching run looks at. A payment a person split in part comes back with what is left."""
        out = []
        blocked = self._blocked_receipts()
        for p in self.list_payments():
            if p.id in blocked or self._receipt_root(p.id) != p.id or self._group_dismissed(p.id):
                continue
            used = self.applied_from(p.id)
            if used == 0:
                out.append(p)
            elif p.amount > 0 and used < p.amount:
                out.append(replace(p, amount=p.amount - used))
        return out

    def applied_from(self, payment_id: str) -> Decimal:
        rows = self._conn.execute("SELECT amount FROM matches WHERE payment_id = ?", (payment_id,)).fetchall()
        return sum((Decimal(r["amount"]) for r in rows), Decimal("0"))

    def _guard_return_allocation(self, payment_id):
        questions = self.return_reviews(include_snapshot=False)
        if any(q["evidence"].get("payment_id")==payment_id for q in questions):
            raise ConflictError("returned payment review: this observation is not verified incoming cash; read /cashdesk returns")
        if payment_id in self._return_quarantine(questions):
            raise ConflictError(self._quarantine_reason(payment_id))

    def _plausibly_returned(self, evidence, receipts):
        """Unallocated money in receipts an open return could have reversed.

        A known return amount/currency must equal the whole receipt (a bank
        return gives back the item); unknown amount keeps every scoped receipt.
        No date or payer guess excludes cash. Existing allocations are never
        touched; only further allocation of a remainder is held.
        """
        try:
            amount = abs(Decimal(str(evidence.get("amount")))) if evidence.get("amount") not in (None, "") else None
        except Exception:
            amount = None
        currency = evidence.get("currency") or None
        out = []
        for pid in receipts:
            p = self.get_payment(pid)
            if (p is None or p.amount <= 0 or self._receipt_root(pid) != pid or self._group_dismissed(pid)
                    or self.applied_from(pid) >= p.amount):
                continue
            if (amount is None or p.amount == amount) and (currency is None or p.currency == currency):
                out.append(pid)
        return sorted(out)

    def _return_quarantine(self, questions=None) -> dict[str, str]:
        """Receipt id -> open return question that may have reversed it."""
        out = {}
        for q in (questions if questions is not None else self.return_reviews(include_snapshot=False)):
            for pid in q["quarantined"]:
                out.setdefault(pid, q["id"])
        return out

    @staticmethod
    def _quarantine_reason(payment_id):
        return (f"returned payment review: {payment_id} may have been returned by the bank; it is not verified incoming cash. "
                f"Read /cashdesk returns, then confirm the return unrelated or record that it reverses {payment_id} before allocating")

    def apply_by_person(self, payment_id: str, invoice_number: str, amount: Decimal | None = None,
                        by: str = "", at: dt.datetime | None = None) -> Match:
        """A person applies (part of) a payment that matching left for review. Never more than is
        left of the payment or open on the invoice; same currency only."""
        with self._tx():
            self._guard_receipt(payment_id)
            self._guard_return_allocation(payment_id)
            if invoice_number in self.customer_identity_holds():
                raise ConflictError(self.customer_identity_holds()[invoice_number])
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
            self._finish_receipt_placement(payment_id, by, at)
            self.refresh_reminders(at=at)
            return match

    def dismiss_payment(self, payment_id: str, reason: str = "", at: dt.datetime | None = None) -> None:
        """A person says a receipt pays none of these invoices (other income, a transfer): it leaves review."""
        with self._tx() as c:
            self._guard_receipt(payment_id, allow_dismissed=True)
            p = self.get_payment(payment_id)
            if p is None:
                raise NotFoundError(f"no payment {payment_id!r}")
            used = self.applied_from(payment_id)
            if used >= p.amount > 0:
                raise ConflictError(f"{payment_id} is already fully applied; there is nothing left to dismiss")
            c.execute("INSERT OR REPLACE INTO dismissed (payment_id, reason, at) VALUES (?, ?, ?)", (payment_id, reason, _ts(at)))
            self.log_event("dismiss", payment_id, {"reason": reason}, at)
            self._finish_receipt_placement(payment_id, "dismiss", at)
            self.refresh_reminders(at=at)

    def apply_match(self, match: Match, at: dt.datetime | None = None) -> bool:
        """Apply a match to its invoice once. False when this payment/invoice pair is already applied."""
        with self._tx() as c:
            if match.invoice_number in self.invoice_import_holds():
                raise ConflictError(self.invoice_import_holds()[match.invoice_number])
            self._guard_receipt(match.payment_id)
            self._guard_return_allocation(match.payment_id)
            if match.invoice_number in self.customer_identity_holds():
                raise ConflictError(self.customer_identity_holds()[match.invoice_number])
            if c.execute("SELECT 1 FROM matches WHERE payment_id = ? AND invoice_number = ?", (match.payment_id, match.invoice_number)).fetchone():
                return False
            if match.payment_id in self._placement_roots() and match.method != "person":
                raise ConflictError("receipt allocation review required: use a human Apply choice, not automatic matching")
            receipt = self.get_payment(match.payment_id)
            if receipt is not None:
                remaining = receipt.amount - self.applied_from(receipt.id)
                if match.amount <= 0 or match.amount > remaining:
                    raise ConflictError(f"only {receipt.currency} {remaining:,.2f} of {receipt.id} is left to apply")
            row = c.execute("SELECT * FROM invoices WHERE number = ?", (match.invoice_number,)).fetchone()
            if row is None:
                raise ConflictError(f"cannot apply {match.payment_id}: invoice {match.invoice_number} is not in the ledger")
            inv = _invoice(row)
            if match.method=="reference+exact":
                if receipt is None:
                    raise ConflictError("reference evidence needs review: the automatic proposal has no retained receipt to verify")
                customer=self.get_customer(inv.customer_id)
                if customer and payer_legal_conflict(customer.name,receipt.counterparty):
                    raise ConflictError("payer legal identity needs review: automatic allocation cannot resolve incompatible explicit legal forms; verify the bank entity and use human Apply if appropriate")
                evidence=reference_evidence(receipt,self.list_invoices())
                if evidence.problem or list(evidence.named)!=[match.invoice_number]:
                    raise ConflictError("reference evidence needs review: automatic allocation requires one complete invoice reference; verify the remittance and use human Apply if appropriate")
            if inv.currency != match.currency:
                raise ConflictError(f"cannot apply {match.payment_id}: {match.currency} against {inv.number} in {inv.currency}")
            reason = self.settlement_reviews().get(inv.number)
            if reason:
                raise ConflictError(reason)
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
            if self._depth == 1:
                self.refresh_reminders(at=at)
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
                invalidated = c.execute("SELECT 1 FROM draft_invalidations WHERE draft_id = ?", (draft.id,)).fetchone()
                rebuilding = invalidated is not None and row["status"] in ("held", "rejected")
                if row["hash"] == draft.hash and not rebuilding:
                    return False
                if row["status"] != "rejected" and not rebuilding:
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
            snapshot=self._contact_snapshot(draft.customer_id)
            if snapshot is not None:
                c.execute("INSERT OR REPLACE INTO draft_contact_basis VALUES (?,?)",(draft.id,snapshot))
            else:c.execute("DELETE FROM draft_contact_basis WHERE draft_id=?",(draft.id,))
            c.execute("INSERT OR REPLACE INTO draft_text_basis VALUES (?, ?, ?, ?)",
                      (draft.id, draft.subject, draft.body, draft.language))
            c.execute("DELETE FROM draft_basis WHERE draft_id = ?", (draft.id,))
            for number in draft.invoice_numbers:
                row = c.execute("SELECT balance, currency, due_date, reference, iban, customer_id FROM invoices WHERE number = ?", (number,)).fetchone()
                if row is not None:
                    c.execute("INSERT INTO draft_basis (draft_id, invoice_number, balance, currency, due_date, reference, iban, customer_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (draft.id, number, row["balance"], row["currency"], row["due_date"], row["reference"], row["iban"], row["customer_id"]))
            c.execute("DELETE FROM draft_invalidations WHERE draft_id = ?", (draft.id,))
            self.log_event("draft", draft.id, {"kind": draft.kind, "customer": draft.customer_id, "status": status}, at)
            return True

    STALE_AGE_DAYS = 6  # a Monday draft approved on Thursday still goes; never past the next stage

    def create_drafts(self, drafts: Iterable[Draft], at: dt.datetime | None = None, as_of: dt.date | None = None) -> int:
        """Store new drafts; ``as_of`` records the day whose overdue ages a reminder states."""
        with self._tx() as c:
            created = 0
            for d in drafts:
                if self.create_draft(d, at):
                    created += 1
                    if as_of is not None:
                        c.execute("INSERT OR REPLACE INTO draft_as_of VALUES (?, ?)", (d.id, as_of.isoformat()))
            return created

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

    def _binding_issue(self, draft: Draft) -> str | None:
        basis = self._conn.execute("SELECT * FROM draft_text_basis WHERE draft_id = ?", (draft.id,)).fetchone()
        if basis is None:
            return "financial baseline missing: regenerate this legacy draft before approval"
        if draft.language != basis["language"] or binding_changed(
                basis["subject"], basis["body"], draft.subject, draft.body, basis["language"]):
            return "financial lines changed: keep invoice rows, totals and payment details intact; edit surrounding prose only"
        return None

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
            binding_issue = self._binding_issue(draft)
            if binding_issue:
                raise ConflictError(binding_issue)
            customer = c.execute("SELECT on_hold FROM customers WHERE id = ?", (draft.customer_id,)).fetchone()
            invalidated = c.execute("SELECT 1 FROM draft_invalidations WHERE draft_id = ?", (draft.id,)).fetchone()
            status = "held" if (customer and customer["on_hold"]) or invalidated else "pending"
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

    def _gate(self, draft: Draft, hash: str, at: dt.datetime | None = None) -> str | None:
        """Return a refusal after persisting its revocation; the caller raises AFTER commit."""
        if hash != draft.hash:
            raise HashMismatchError(f"draft {draft.id}: the hash does not match the stored text; re-read the draft")
        day = _now(at).date() if at is not None else self._today()
        binding_issue = self._binding_issue(draft) or self._age_issue(draft, day)
        issue = ("stale", binding_issue) if binding_issue else self._draft_issue(draft, self._eligibility(day))
        if issue is None:
            prior = self._conn.execute("SELECT kind, reason FROM draft_invalidations WHERE draft_id = ?", (draft.id,)).fetchone()
            if prior:
                issue = prior["kind"], prior["reason"]
        if issue:
            self._invalidate_draft(draft, issue, at)
            code, reason = issue
            prefix = "is stale" if code == "stale" else "reminder is paused"
            return (f"draft {draft.id} {prefix}: {reason}. Resolve the hold or changed figures, run cashdesk_draft, "
                    "then read the replacement and have a person approve its current hash. Nothing was sent")
        return None

    def _age_issue(self, draft: Draft, day: dt.date) -> str | None:
        """A reminder states days overdue as of the day it was written; after a couple of days that is untrue."""
        if not draft.kind.startswith("stage"):
            return None
        row = self._conn.execute("SELECT as_of FROM draft_as_of WHERE draft_id=?", (draft.id,)).fetchone()
        if row is None:
            return None
        written = dt.date.fromisoformat(row["as_of"])
        if (day - written).days > self.STALE_AGE_DAYS:
            return (f"it was written on {written.isoformat()} and its days-overdue figures are no longer true on {day.isoformat()}")
        return None

    def check_draft(self, draft_id: str, hash: str, at: dt.datetime | None = None) -> None:
        """Preflight eligibility without reserving mail; refusals durably revoke approval."""
        with self._tx():
            refused = self._gate(self.get_draft(draft_id), hash, at)
        if refused:
            raise ConflictError(refused)

    def approve_draft(self, draft_id: str, hash: str, approved_by: str = "", at: dt.datetime | None = None) -> None:
        """Approve exactly the stored text. Refused for a held, sent or rejected draft, a customer on hold or in dispute, or a stale reminder."""
        with self._tx() as c:
            current = self.get_draft(draft_id)
            if current.kind == "escalation":
                raise ConflictError(f"{draft_id} is an internal escalation note for you, not a message to the customer; "
                                    "nothing is sent. Decide the next step (call them, hold, or a final notice outside Cash Desk) and reject it when done")
            refused = self._gate(current, hash, at)
            if not refused:
                if current.status in ("sent", "rejected", "held"):
                    raise ConflictError(f"draft {draft_id} is {current.status} and cannot be approved")
                c.execute("INSERT OR REPLACE INTO approvals (draft_id, hash, approved_at, approved_by) VALUES (?, ?, ?, ?)", (draft_id, hash, _ts(at), approved_by))
                c.execute("UPDATE drafts SET status = 'approved', updated_at = ? WHERE id = ?", (_ts(at), draft_id))
                self.log_event("approve", draft_id, {"hash": hash, "by": approved_by}, at)
        if refused:
            raise ConflictError(refused)

    def reserve_send(self, draft_id: str, hash: str, mode: str, at: dt.datetime | None = None) -> str:
        """Reserve the one send of an approved draft and return its idempotency key.

        Call this before the mail leaves and ``confirm_send`` after. A second
        reservation, whether the first is in flight or done, is refused; a
        failed one (``fail_send``) may be retried under the same key.
        """
        with self._tx() as c:
            current = self.get_draft(draft_id)
            refused = self._gate(current, hash, at)
            if not refused:
                approval = self.approval(draft_id)
                if approval is None or approval.hash != hash:
                    raise NotApprovedError(f"draft {draft_id} has no approval for this text")
                key = idempotency_key(draft_id, hash)
                row = c.execute("SELECT status FROM sends WHERE idempotency_key = ?", (key,)).fetchone()
                if current.status == "sent" or (row is not None and row["status"] == "sent"):
                    raise AlreadySentError(f"draft {draft_id} was already sent (key {key})")
                if current.status != "approved":
                    raise NotApprovedError(f"draft {draft_id} has no approval for this text")
                if row is not None and row["status"] == "reserved":
                    raise AlreadySentError(f"draft {draft_id} has a send in progress (key {key}); confirm or fail it first")
                c.execute("INSERT OR REPLACE INTO sends (idempotency_key, draft_id, hash, reserved_at, sent_at, mode, status, note) VALUES (?, ?, ?, ?, NULL, ?, 'reserved', '')",
                          (key, draft_id, hash, _ts(at), mode))
                self.log_event("send_reserved", draft_id, {"mode": mode, "key": key}, at)
        if refused:
            raise ConflictError(refused)
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
        refused = None
        with self._tx():
            try:
                key = self.reserve_send(draft_id, hash, mode, at)
            except ConflictError as exc:
                refused = exc
            else:
                self.confirm_send(key, at)
        if refused:
            raise refused
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

    def final_reminders_sent(self) -> set[str]:
        """Invoices whose 28-day reminder really went out."""
        rows = self._conn.execute(
            "SELECT d.invoice_numbers FROM sends s JOIN drafts d ON d.id = s.draft_id WHERE s.status = 'sent' AND d.kind = 'stage28'").fetchall()
        return {n for r in rows for n in json.loads(r["invoice_numbers"])}

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
        blocked_receipts = self._blocked_receipts() | {p.id for p in self.list_payments() if self._receipt_root(p.id) in self._placement_roots()}
        return_holds = {**self.return_holds(), **self.customer_identity_holds(), **self.bank_evidence_holds()}
        for r in self._conn.execute("SELECT * FROM matches ORDER BY applied_at, rowid").fetchall():
            if r["payment_id"] in blocked_receipts or r["invoice_number"] in return_holds:
                continue
            applied_at = _dt(r["applied_at"])
            if since is not None and applied_at <= since:
                continue
            m = _match(r)
            inv = invoices.get(m.invoice_number)
            payments.append(PaymentView(m, applied_at, self.get_payment(m.payment_id), inv, customers.get(inv.customer_id) if inv else None))
        review = tuple(i.payment for i in self.match_result(as_of).review if i.payment.amount > 0)
        return BriefSnapshot(as_of, since, tuple(payments), tuple(self.list_drafts(status="pending")), tuple(self.list_drafts(status="held")), customers, invoices,
                             dict(self.reminder_holds(as_of)), review)


# ------------------------------------------------------------ row mapping


def _customer(r: sqlite3.Row) -> Customer:
    return Customer(r["id"], r["name"], r["language"], r["email"], r["contact_name"], bool(r["on_hold"]), r["hold_reason"])


def _invoice(r: sqlite3.Row) -> Invoice:
    return Invoice(r["number"], r["customer_id"], _d(r["issue_date"]), _d(r["due_date"]), Decimal(r["amount"]), r["currency"], Decimal(r["balance"]),
                   r["status"], r["reference"], r["iban"], _d(r["paid_date"]), r["note"])


def _payment(r: sqlite3.Row) -> Payment:
    return Payment(r["id"], _d(r["date"]), Decimal(r["amount"]), r["currency"], r["reference"], r["counterparty"], r["memo"], r["source"], r["identity_origin"] if "identity_origin" in r.keys() else "unknown")


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
