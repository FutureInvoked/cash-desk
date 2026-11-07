"""Cash Desk: a Hermes plugin that chases overdue invoices.

The model writes the words; the ledger owns the numbers.

This file is the Hermes half: the tools, the ``pre_tool_call`` gate, the
``/cashdesk`` slash command and the morning-brief blueprint. The engine
(import, matching, chasing, Figure Lock, SQLite store) lives in the
``cashdesk`` package beside it and never imports Hermes; ``sending.py`` writes
.eml files or talks to the user's own SMTP server.

Two human touches stand between a draft and a customer: a person approves the
exact text (Desktop button or ``/cashdesk approve``), and Hermes's own approval
gate asks once more when the model calls ``cashdesk_send``. Scheduled runs only
read.
"""

from __future__ import annotations

import json
import os
import re
import threading
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Relative imports stay inside functions: Hermes loads this folder as a package, but
# pytest also imports this file while collecting tests (the repository root is a
# package), and a module-level relative import would fail there.

PLUGIN_NAME = "cash-desk"
TOOLSET = "cashdesk"
SEND_TOOL = "cashdesk_send"
LANGS = ("en", "cs")
IMPORT_KINDS = ("customers", "invoices", "bank")
INVOICE_SOURCES = ("generic", "xero", "quickbooks", "stripe")
QUEUE_STATUSES = ("pending", "approved", "held", "sent", "rejected", "all")
# Tools that change the ledger or the queue; none of them runs from a scheduled job.
WRITE_TOOLS = frozenset({
    "cashdesk_import", "cashdesk_match", "cashdesk_draft", "cashdesk_revise",
    "cashdesk_reply", "cashdesk_hold", SEND_TOOL,
})
HELP_TEXT = """Cash Desk commands (type /cashdesk <command>):

Every day
  status                         ledger, drafts waiting, customers on hold
  queue                          drafts waiting for approval
  show <draft-id>                the exact text, ending with its approve command
  approve <draft-id> <hash>      approve the exact text you read (as shown at the end of show)
  reject <draft-id> [reason]     drop a draft
  review                         payments waiting for a decision
  apply <payment-id> <invoice> [amount]   apply a payment by hand
  dismiss <payment-id> [reason]  not an invoice payment (or already counted)
  hold <customer-id> [reason]    pause a customer;  release <customer-id> to resume

When Cash Desk pauses an invoice (it tells you which command to run)
  still-open <invoice> <why>     an invoice missing from the latest export is still owed
  not-duplicate <a> <b> <why>    two similar invoices are really two debts
  send-status <draft> sent|failed <note>   a send interrupted midway (check your Sent folder first)
  import-errors / import-error   rows an import could not trust
  overlaps / receipt / receipt-keep / receipt-updates / receipt-update   one bank receipt seen twice
  returns / return-unrelated / return-reverses   money the bank returned
  settlements / reconcile        payments an export already counts
  surpluses / surplus-accounted  overpayments
  identities / identity-verify   customer IDs that disagree
  bank-evidence / bank-evidence-verify   bank columns Cash Desk could not read

Each of these lists what it needs and shows the exact command with its snapshot code."""

COMMAND_HELP = "/cashdesk help | status | queue | show <draft-id> | approve <draft-id> <hash> | review | apply | dismiss | hold | release  (/cashdesk help lists every command)"

# Set in register(); the gate asks Hermes whether the current code runs inside a
# scheduled (cron) execution, and the tools read plugin settings through it.
_ctx: Any = None


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


AS_OF = {"type": "string", "description": "ISO date to treat as today (default: today)"}

STATUS_SCHEMA = _schema(
    "cashdesk_status",
    "Report the Cash Desk ledger state: open balance by age bucket per currency, drafts waiting for "
    "approval, customers on hold and the last import. Read-only.",
    {"as_of": AS_OF},
)
BRIEF_SCHEMA = _schema(
    "cashdesk_brief",
    "Morning cash brief: payments received since the last brief, newly overdue invoices, drafts awaiting "
    "approval and customers on hold, per currency, in English or Czech. Read-only: it never drafts, "
    "revises, approves or sends anything.",
    {"lang": {"type": "string", "enum": list(LANGS), "description": "Language of the brief (default en)"}, "as_of": AS_OF},
)
IMPORT_SCHEMA = _schema(
    "cashdesk_import",
    "Import a CSV export into the Cash Desk ledger. kind=customers reads customer_id, name, language, email, "
    "contact_name, on_hold, hold_reason; kind=invoices reads a generic, Xero, QuickBooks or Stripe invoice "
    "export; kind=bank reads a bank statement (English or Czech headers). The same file is never imported "
    "twice. Reads only the file you pass.",
    {
        "kind": {"type": "string", "enum": list(IMPORT_KINDS)},
        "path": {"type": "string", "description": "Path of the CSV file. Relative paths start at the plugin folder; only the plugin data folder, demo/ and the import_dirs setting are readable. The bundled demo is four files, imported in this order: demo/customers.csv (customers), demo/invoices.csv (invoices, source generic), demo/bank_day1.csv and demo/bank_day2.csv (bank); then match and draft (as of today: the demo's dates move to today when it is loaded)."},
        "source": {"type": "string", "enum": list(INVOICE_SOURCES), "description": "Invoice export format (default generic)"},
        "default_currency": {"type": "string", "description": "Currency for rows without one. Invoices fall back to the currency setting; a bank statement with no currency column is refused until you pass the account's currency here - ask the person which currency the account is in"},
        "date_order": {"type": "string", "enum": ["auto", "dmy", "mdy"], "description": "How to read slashed dates. Default auto: decided from the whole file; a file whose dates all read both ways is refused until you say dmy (day first) or mdy (month first)."},
        "allow_errors": {"type": "boolean", "description": "Import the good rows even when some rows have errors (default false)"},
        "open_list": {"type": "boolean", "description": "kind=invoices only. Default true: an invoice export is treated as listing every open invoice of its source, so an open invoice from the same source that it no longer lists (usually because it was paid) is held as 'not in the latest export' instead of being chased. Pass false only when the person says this file is deliberately partial (one month, one customer, new invoices only)."},
    },
    ["kind", "path"],
)
MATCH_SCHEMA = _schema(
    "cashdesk_match",
    "Match unapplied bank payments to open invoices. A payment is applied only when its reference names "
    "exactly one invoice and its amount equals that invoice's balance in the same currency; everything else "
    "goes to a review list with scored candidates for a person. Nothing is sent.",
    {"as_of": AS_OF},
)
DRAFT_SCHEMA = _schema(
    "cashdesk_draft",
    "Plan today's follow-ups from the ledger and store them as pending drafts: reminders at 7, 14, 21 and 28 "
    "days overdue in the customer's language (an older backlog invoice starts at the 28-day reminder), thank-you "
    "notes for payments received, and an internal escalation note a week after an unanswered 28-day reminder. "
    "Customers on hold or in dispute get nothing. Every draft waits for a "
    "person's approval; nothing is sent.",
    {"as_of": {"type": "string", "description": "Must be today's ISO date if given; drafts for any other date are refused"},
     "sender": {"type": "string", "description": "Business name that signs the drafts (default: the sender_name setting)"},
     "bank_checked": {"type": "boolean", "description": "Only after the person confirmed no payment is missing: draft although the last bank statement was refused"}},
)
QUEUE_SCHEMA = _schema(
    "cashdesk_queue",
    "List drafts by status (pending by default) with their exact subject, body, recipients, verified figures "
    "and hash. Use the hash when asking a person to approve or when sending.",
    {"status": {"type": "string", "enum": list(QUEUE_STATUSES)}, "customer_id": {"type": "string"}},
)
REVISE_SCHEMA = _schema(
    "cashdesk_revise",
    "Edit a draft's surrounding prose. When you write the text, keep financial lines intact, including invoice rows, totals and bank "
    "details; whole rows may be reordered, but do not rewrite financial sentences. When the user gives their own text or asks for a "
    "change, submit it as asked: Figure Lock, not you, decides. It checks that every amount, invoice number, due date, IBAN and "
    "payment reference still matches the ledger exactly, with nothing added, changed or missing. A rejected revision returns the "
    "precise diff and leaves the draft untouched (show the user that diff); an accepted one gets a new hash and needs a fresh approval.",
    {
        "draft_id": {"type": "string"},
        "new_text": {"type": "string", "description": "The complete new body"},
        "new_subject": {"type": "string", "description": "Optional new subject line"},
    },
    ["draft_id", "new_text"],
)
REPLY_SCHEMA = _schema(
    "cashdesk_reply",
    "Record a customer's reply as dispute, paid, promise or other (pass kind with your own reading; a keyword "
    "classifier is only the fallback). A dispute puts the customer on hold until a person releases them; a "
    "promise with a date pauses their reminders until two days after it; paid changes nothing until the bank "
    "statement shows the money.",
    {"customer_id": {"type": "string"}, "text": {"type": "string"}, "lang": {"type": "string", "enum": list(LANGS)},
     "kind": {"type": "string", "enum": ["dispute", "paid", "promise", "other"], "description": "Your reading of the reply (better than the keyword fallback): dispute = they contest the invoice or the amount; paid = they say they already paid; promise = they promise to pay by a date; other = anything else (a question, a request to resend the invoice)"},
     "promised_date": {"type": "string", "description": "kind=promise: the date they promised, YYYY-MM-DD. No reminder goes to them until two days after it."}},
    ["customer_id", "text"],
)
HOLD_SCHEMA = _schema(
    "cashdesk_hold",
    "Put a customer on hold: their pending drafts are held and nothing is approved or sent to them. Only a "
    "person can release a hold (/cashdesk release or the Desktop page).",
    {"customer_id": {"type": "string"}, "reason": {"type": "string"}},
    ["customer_id"],
)
SEND_SCHEMA = _schema(
    "cashdesk_send",
    "Send one approved draft as an .eml file (default) or through the configured SMTP server. Refused unless "
    "a person approved exactly this text (draft_hash); Hermes asks the person once more before the call "
    "runs. Never available in scheduled runs.",
    {"draft_id": {"type": "string"}, "draft_hash": {"type": "string", "description": "The hash of the approved text"}},
    ["draft_id", "draft_hash"],
)

EMPTY_LEDGER = {
    "en": ("Cash Desk brief for {as_of}\n"
           "No ledger yet: import customers, invoices and a bank statement with cashdesk_import. "
           "Nothing is awaiting approval and no customer is on hold."),
    "cs": ("Přehled Cash Desk k {as_of}\n"
           "Zatím žádná evidence: nahrajte zákazníky, faktury a bankovní výpis nástrojem cashdesk_import. "
           "Nic nečeká na schválení a žádný zákazník není pozastaven."),
}


# --- engine access -------------------------------------------------------------------------------------

def _engine() -> Any:
    """The engine package, or None when it cannot be imported."""
    try:
        from . import cashdesk
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cash-desk: engine import failed: %s", exc)
        return None
    return cashdesk


def engine_version() -> str:
    engine = _engine()
    return str(getattr(engine, "ENGINE_VERSION", "unknown") if engine else "unknown")


def _store() -> Any:
    """The engine's Store over this profile's ledger, or None without the engine."""
    engine = _engine()
    if engine is None:
        return None
    from .paths import store_path, today

    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = engine.Store(path, today=today, ignore_balances_below=_small_balance())
    _sync_payment_iban(engine, store, str(_config("payment_iban", "") or ""))
    return store


def _sync_payment_iban(engine: Any, store: Any, setting: str) -> None:
    """Keep invoices without an export IBAN on the current payment_iban setting every time the ledger
    is opened, so drafting, approval and sending all see the account the setting names now."""
    own = "".join(setting.split()).upper()
    if own and not engine.iban_valid(own):
        return  # drafting refuses with a clear message; never write an invalid account
    try:
        store.sync_payment_iban(own)
    except Exception:
        logger.warning("cash-desk: payment_iban could not be applied", exc_info=True)


def _close(store: Any) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # pragma: no cover - defensive
            pass


def _config(key: str, default: Any = None) -> Any:
    getter = getattr(_ctx, "get_config", None)
    if getter is None:
        return default
    try:
        value = getter(key, default=default)
    except TypeError:
        value = getter(key, default)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cash-desk: get_config(%s) failed: %s", key, exc)
        return default
    return default if value is None else value


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fail(message: str, **extra: Any) -> str:
    return _json({"ok": False, "error": message, **extra})


def _error(what: str, exc: Exception) -> str:
    return _fail(f"{what}: {exc.__class__.__name__}: {exc}")


def _lang(value: Any) -> str:
    value = str(value or "en").lower()
    return value if value in LANGS else "en"


def _as_of(value: Any) -> date:
    text = str(value or "").strip()
    from .paths import today

    return date.fromisoformat(text) if text else today()


def _draft_dict(store: Any, draft: Any) -> dict[str, Any]:
    """The JSON shape the tools, the slash command and the Desktop page share."""
    try:
        customer = store.get_customer(draft.customer_id).name
    except Exception:
        customer = draft.customer_id
    return {
        "id": draft.id, "kind": draft.kind, "customer_id": draft.customer_id, "customer": customer,
        "lang": draft.language, "subject": draft.subject, "body": draft.body,
        "recipients": list(draft.recipients), "invoice_numbers": list(draft.invoice_numbers),
        "created_at": draft.created_at.isoformat(), "status": draft.status, "hash": draft.hash,
        "figures": _figure_badges(store, draft),
    }


def _figure_badges(store: Any, draft: Any) -> list[dict[str, Any]]:
    """Each stated figure with whether today's ledger still holds it (never a hard-coded tick)."""
    try:
        engine = _engine()
        invoices = [store.get_invoice(n) for n in draft.invoice_numbers]
        allowed = engine.ledger_figures(invoices)
        states = engine.figure_status(draft, allowed)
        # Totals and amounts received are sums the ledger implies, not single ledger figures.
        sums: dict[str, list] = {}
        for inv in invoices:
            sums.setdefault(inv.currency, []).append(inv)
        derived = set()
        for cur, group in sums.items():
            derived.add(f"{cur} {sum((i.balance for i in group), engine.to_money('0')):.2f}")
            derived.add(f"{cur} {sum((i.amount for i in group), engine.to_money('0')):.2f}")
        paid = [m for m in store.list_matches() if m.invoice_number in draft.invoice_numbers]
        for m in paid:
            derived.add(f"{m.currency} {m.amount:.2f}")
        for cur in {m.currency for m in paid}:
            derived.add(f"{cur} {sum((m.amount for m in paid if m.currency == cur), engine.to_money('0')):.2f}")
        states = [ok or (f.kind == "money" and f.canonical in derived) for f, ok in zip(draft.figures, states)]
    except Exception:
        states = [None] * len(draft.figures)
    return [{"kind": f.kind, "value": f.canonical, "ok": ok} for f, ok in zip(draft.figures, states)]


def _payment_dict(payment: Any) -> dict[str, Any]:
    return {"id": payment.id, "date": payment.date.isoformat(), "amount": str(payment.amount), "currency": payment.currency,
            "reference": payment.reference, "counterparty": payment.counterparty, "memo": payment.memo}


def _review_dict(item: Any) -> dict[str, Any]:
    return {
        "payment": _payment_dict(item.payment), "kind": item.kind, "detail": item.detail, "named": list(item.named), "unknown_references": list(item.unknown_references),
        "candidates": [{"invoice_number": c.invoice_number, "customer_id": c.customer_id, "score": round(c.score, 2),
                        "balance": str(c.balance), "currency": c.currency} for c in item.candidates],
        "holds": list(item.holds),
    }


def _match_result(store: Any, engine: Any, as_of: date) -> Any:
    """Read-only matching pass over the unapplied payments."""
    return store.match_result(as_of)


# --- read-only tools -----------------------------------------------------------------------------------

def handle_status(args: dict[str, Any], **_kw: Any) -> str:
    try:
        store = _store()
        if store is None:
            return _json({"plugin": PLUGIN_NAME, "engine": False, "engine_version": engine_version(), "ledger": "empty"})
        try:
            snapshot = store.status_snapshot(_as_of(args.get("as_of"))).to_dict()
            snapshot["receipt_overlap_questions"] = len(store.payment_overlaps())
            snapshot["receipt_placement_questions"] = len(store.receipt_placement_reviews())
            snapshot["receipt_update_questions"] = len(store.receipt_conflicts())
            snapshot["customer_identity_questions"] = len(store.customer_identity_reviews())
            snapshot["return_questions"] = len(store.return_reviews())
            snapshot["surplus_questions"] = len(store.surplus_reviews())
            snapshot["invoice_import_questions"] = len(store.invoice_import_questions())
            snapshot["bank_evidence_questions"] = len(store.bank_evidence_questions())
            snapshot["balances_need_review"] = bool(snapshot["bank_evidence_questions"] or snapshot["customer_identity_questions"] or snapshot["return_questions"] or snapshot["surplus_questions"] or snapshot["invoice_import_questions"] or snapshot["receipt_update_questions"] or snapshot["receipt_overlap_questions"] or snapshot["receipt_placement_questions"])
            imports = store.list_imports()
        finally:
            _close(store)
        snapshot.update({"plugin": PLUGIN_NAME, "engine": True, "engine_version": engine_version(),
                         "ledger": "ok" if imports else "empty", "imports": len(imports)})
        return _json(snapshot)
    except Exception as exc:
        return _error("status failed", exc)


_RECENT_BRIEFS: list[str] = []  # the last briefs this process made: the reply check lets the agent quote them


def handle_brief(args: dict[str, Any], **_kw: Any) -> str:
    lang = _lang(args.get("lang"))
    try:
        as_of = _as_of(args.get("as_of"))
        engine, store = _engine(), _store()
        if engine is None or store is None:
            text = EMPTY_LEDGER[lang].format(as_of=as_of.isoformat())
        else:
            try:
                if store.list_imports():
                    text = engine.morning_brief(store, as_of, lang)
                    if store.payment_overlaps() or store.receipt_placement_reviews():
                        text = "Possible receipt overlap: uncertain observations and allocations are excluded from received-cash totals. Read /cashdesk overlaps before chasing or applying them.\n\n" + text
                    if store.customer_identity_reviews():
                        text = "Customer identity review: retained customer mappings need verification via /cashdesk identities. Balances and allocations have not been reassigned.\n\n" + text
                    if store.return_reviews():
                        text = "Returned payment review: retained receipts may have bounced; verify /cashdesk returns. Balances and allocations have not been reversed.\n\n" + text
                    if store.surplus_reviews():
                        text = "Customer surplus review: overpayment evidence may offset collection; verify /cashdesk surpluses. Observations are not extra cash and are not netted automatically.\n\n" + text
                    if store.invoice_import_questions():
                        text = "Invoice import uncertainty: failed updates may change the balances below; verify /cashdesk import-errors before relying on them.\n\n" + text
                    if store.bank_evidence_questions():
                        text = store._bank_evidence_reason().capitalize() + ". Balances and sent history are unchanged.\n\n" + text
                    if store.receipt_conflicts():
                        text = "Receipt update conflict: retained balances and allocations need bank verification. Read /cashdesk receipt-updates; uncertain cash is excluded from this brief.\n\n" + text
                    store.record_brief()
                    _RECENT_BRIEFS.append(text)
                    del _RECENT_BRIEFS[:-5]
                else:
                    text = EMPTY_LEDGER[lang].format(as_of=as_of.isoformat())
            finally:
                _close(store)
        return _json({"as_of": as_of.isoformat(), "lang": lang, "brief": text})
    except Exception as exc:
        return _error("brief failed", exc)


def handle_queue(args: dict[str, Any], **_kw: Any) -> str:
    status = str(args.get("status") or "pending").lower()
    if status not in QUEUE_STATUSES:
        return _fail(f"status must be one of {', '.join(QUEUE_STATUSES)}")
    try:
        store = _store()
        if store is None:
            return _fail("the Cash Desk engine is not available")
        try:
            drafts = store.list_drafts(status=None if status == "all" else status,
                                       customer_id=str(args.get("customer_id") or "") or None)
            return _json({"ok": True, "status": status, "count": len(drafts), "drafts": [_draft_dict(store, d) for d in drafts]})
        finally:
            _close(store)
    except Exception as exc:
        return _error("queue failed", exc)


# --- tools that change the ledger ------------------------------------------------------------------------

def _handle_import(args: dict[str, Any], **_kw: Any) -> str:
    kind = str(args.get("kind") or "").strip().lower()
    if kind not in IMPORT_KINDS:
        return _fail(f"kind must be one of {', '.join(IMPORT_KINDS)}")
    raw = str(args.get("path") or "").strip()
    if not raw:
        return _fail("path is required")
    from .paths import allowed_import_path

    path = allowed_import_path(Path(raw), str(_config("import_dirs", "") or ""))
    if path is None:
        # Checked before the file is touched: nothing outside these folders is opened or probed.
        return _fail(f"{raw} is outside the folders Cash Desk may read (its data folder, its demo/ folder and the "
                     "import_dirs setting); add the folder to import_dirs in the plugin settings first")
    if not path.is_file():
        return _fail(f"no such file: {path}")
    source = str(args.get("source") or "generic").strip().lower()
    if source not in INVOICE_SOURCES:
        return _fail(f"source must be one of {', '.join(INVOICE_SOURCES)}")
    from .paths import chosen_currency, in_currency, is_demo_file, on_today

    setting = chosen_currency(str(_config("currency", "") or ""))
    currency = str(args.get("default_currency") or setting).strip().upper()
    date_order = str(args.get("date_order") or _config("date_order", "") or "auto").strip().lower()
    open_list = args.get("open_list", True)
    open_list = open_list if isinstance(open_list, bool) else str(open_list).strip().lower() not in ("false", "0", "no", "")
    allow_errors = bool(args.get("allow_errors"))
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        if kind == "customers":
            result = engine.read_customers(path)
        else:
            read = ((lambda order: engine.read_invoices(source, path, default_currency=currency, date_order=order)) if kind == "invoices"
                    else (lambda order: engine.read_bank_csv(path, default_currency=currency, date_order=order)))
            result = read(date_order)
            if date_order in ("dmy", "mdy") and not args.get("date_order") and not is_demo_file(path):
                doubt = _implausible_setting_order(read, result, date_order)
                if doubt:
                    return _fail(doubt, kind=kind)
        if is_demo_file(path):
            result = on_today(in_currency(result, setting))
        elif kind in ("customers", "invoices"):
            result = _default_language(engine, result, store)
        elif kind == "bank" and not args.get("default_currency") and any(getattr(w, "code", "") == "bank_currency_default" for w in getattr(result, "warnings", ())):
            # The setting is not evidence of which currency an account holds: a GBP account read as EUR
            # would apply GBP 1,200 to a EUR 1,200 invoice. Ask once per file.
            return _fail(f"this bank statement has no currency column or currency in its headers (or some lines leave it blank); Cash Desk will not assume "
                         f"the account is in {currency}. Ask which currency this account is in and import again with "
                         f"default_currency set (for example default_currency={currency}); nothing was imported", kind=kind)
        errors = [{"row": e.row, "message": e.message} for e in result.errors]
        if kind == "bank" and any(e.code == "bank_status_conflict" for e in result.errors):
            return _fail("bank status evidence conflicts: the file cannot be partially imported; verify the bank and re-export consistent lifecycle fields",
                         kind=kind, rows=result.rows, errors=errors[:50])
        if result.errors and any("read both ways" in e.message or "reads both ways" in e.message or "mix day-first" in e.message for e in result.errors):
            return _fail("every date in this file reads both ways (day/month or month/day); ask how one date should be read "
                         "(UK/EU banks: date_order=dmy, US: date_order=mdy) and import it again with date_order set; nothing was imported",
                         kind=kind, rows=result.rows, errors=errors[:5])
        if kind == "invoices" and open_list and "open_list" not in args:
            listed = [r.number for r in result.records] + [c.number for c in getattr(result, "invoice_closures", ())]
            gone, open_now = store.would_go_missing(result.source, listed)
            if len(gone) > 2 and len(gone) * 2 > open_now:
                return _fail(f"this file leaves out {len(gone)} of the {open_now} open invoices Cash Desk has from {result.source}. "
                             "If it lists every open invoice (they were paid or credited), import it again with open_list=true and they "
                             "are held as 'not in the latest export'; if it is a partial export (new invoices only, one month), import "
                             "it with open_list=false and they keep being chased. Nothing was imported.", kind=kind,
                             missing=gone[:20])
        if result.errors and not allow_errors:
            return _fail(f"the file has {len(result.errors)} row error(s); fix it or pass allow_errors=true",
                         kind=kind, rows=result.rows, errors=errors[:50])
        counts = store.ingest(result, allow_errors=allow_errors, file_name=path.name, open_list=open_list and kind == "invoices")
        bank_evidence_reason = store._bank_evidence_reason() if counts.get("bank_evidence_questions") else ""
    except engine.DuplicateImportError as exc:
        released = []
        if kind == "invoices" and open_list:
            try:
                released = store.release_missing([inv.number for inv in result.records], file_name=path.name)
            except Exception:
                released = []
        extra = (f"; it lists {len(released)} invoice(s) that were held as not in the latest export, so they are chased again: "
                 + ", ".join(released)) if released else ""
        return _fail(f"already imported: {exc}{extra}", duplicate=True, released=released)
    except Exception as exc:
        return _error("import failed", exc)
    finally:
        _close(store)
    skipped_by_reason: dict[str, int] = {}
    for row in result.skipped:
        skipped_by_reason[row.message] = skipped_by_reason.get(row.message, 0) + 1
    return _json({"ok": not bool(counts.get("bank_evidence_questions") or counts.get("customer_identity_questions") or counts.get("return_questions") or counts.get("receipt_update_questions") or counts.get("invoice_import_questions")), "quarantined": bool(counts.get("bank_evidence_questions") or counts.get("customer_identity_questions") or counts.get("return_questions") or counts.get("receipt_update_questions") or counts.get("invoice_import_questions")), "kind": kind, "source": result.source, "file": path.name, "rows": result.rows,
                  "imported": counts, "skipped": len(result.skipped), "skipped_by_reason": skipped_by_reason,
                  "credit_notes": len(getattr(result, "credit_notes", ()) or ()),
                  "credit_updates": len(getattr(result, "credit_updates", ()) or ()),
                  "warnings": ([{"row": w.row, "message": w.message} for w in (getattr(result, "warnings", ()) or ())][:50] +
                               ([{"row": 0, "message": bank_evidence_reason}] if counts.get("bank_evidence_questions") else []) +
                               ([{"row": 0, "message": "receipt update conflict: observations retained and quarantined; verify /cashdesk receipt-updates"}] if counts.get("receipt_update_questions") else []) +
                               ([{"row": 0, "message": "invoice import uncertainty: failed updates retained; verify /cashdesk import-errors"}] if counts.get("invoice_import_questions") else []) +
                               ([{"row": 0, "message": "customer identity review: verify original customer IDs via /cashdesk identities before collection"}] if counts.get("customer_identity_questions") else []) +
                               ([{"row": 0, "message": "returned payment review: retained bank evidence needs verification via /cashdesk returns"}] if counts.get("return_questions") else []) +
                               ([{"row": 0, "message": "customer surplus review: collection paused; verify /cashdesk surpluses"}] if counts.get("surplus_questions") else []) +
                               ([{"row": 0, "message": f"{counts['not_in_export']} open invoice(s) from this source are not in this file and are held as "
                                  "'not in the latest export' (probably paid). If this file is deliberately partial, say so and re-import with "
                                  "open_list=false next time; to chase one anyway use /cashdesk still-open <invoice> <why>"}] if counts.get("not_in_export") else [])),
                  "date_order": getattr(result, "date_order", None),
                  "errors": errors, "fingerprint": result.fingerprint[:12]})


def _bank_freshness(store: Any, as_of: date) -> str:
    """Reminders are only as good as the newest bank statement: say so when it is old or missing."""
    row = store._conn.execute("SELECT MAX(date) AS d FROM payments").fetchone()
    if not row or not row["d"]:
        return ("No bank statement has been imported, so payments are unknown: a customer who already paid may be reminded. "
                "Import a current statement and run cashdesk_match before approving reminders.")
    newest = date.fromisoformat(str(row["d"])[:10])
    days = (as_of - newest).days
    if days > 7:
        return (f"The newest bank line is {days} days old ({newest.isoformat()}); payments since then are unknown, "
                "so a customer who already paid may be reminded. Import a current statement (check that it was not refused) "
                "and run cashdesk_match before approving reminders.")
    return ""


def handle_import(args: dict[str, Any], **kw: Any) -> str:
    """Import, and remember a refused bank statement until one imports cleanly: reminders drafted on a
    ledger whose newest statement was refused would chase customers who paid."""
    raw = _handle_import(args, **kw)
    if str(args.get("kind") or "").strip().lower() != "bank":
        return raw
    try:
        out = json.loads(raw)
    except ValueError:
        return raw
    store = _store()
    if store is None:
        return raw
    try:
        name = Path(str(args.get("path") or "")).name
        error = str(out.get("error") or "")
        if out.get("ok") or out.get("quarantined"):
            store.clear_bank_refusals(name)  # only the refused file itself, imported cleanly, clears its hold
        elif not out.get("duplicate") and name and not error.startswith(("no such file", "path is required")) \
                and "outside the folders" not in error:
            store.record_bank_refusal(name, error[:300] or "refused")
    except Exception:
        logger.warning("cash-desk: could not record the bank import outcome", exc_info=True)
    finally:
        _close(store)
    return raw


def _default_language(engine: Any, result: Any, store: Any) -> Any:
    """A file with no language column says nothing about language: existing customers keep theirs,
    new ones get the default_language setting."""
    if any(col in ("language", "lang", "jazyk") for col in getattr(result, "columns", ())):
        return result
    lang = str(_config("default_language", "en") or "en").strip().lower()
    if lang not in getattr(engine, "LANGUAGES", ("en", "cs")):
        lang = "en"
    known = {c.id: c.language for c in store.list_customers()}
    from dataclasses import replace as _replace
    pick = lambda c: _replace(c, language=known.get(c.id, lang))
    customers = tuple(pick(c) for c in getattr(result, "customers", ()))
    records = tuple(pick(c) for c in result.records) if result.kind == "customers" else result.records
    return _replace(result, customers=customers, records=records)


def _file_dates(result: Any) -> list[date]:
    out: list[date] = []
    for r in (*getattr(result, "records", ()), *getattr(result, "invoice_closures", ()), *getattr(result, "credit_notes", ())):
        for name in ("issue_date", "due_date", "date"):
            value = getattr(r, name, None)
            if isinstance(value, date):
                out.append(value)
    return out


def _implausible_setting_order(read: Any, result: Any, order: str) -> str:
    """The date_order SETTING decided a file whose dates all read both ways. If the other reading
    is the recent one (newest date over 45 days newer, nothing in the future), the setting is
    probably wrong for this file: a US export read day-first states months of false lateness."""
    try:
        auto = read(None)
    except Exception:
        return ""
    if not any("reads both ways" in getattr(e, "message", "") for e in getattr(auto, "errors", ())):
        return ""  # the file decides its own order; the setting only agreed with it or was not needed
    try:
        other = read("mdy" if order == "dmy" else "dmy")
    except Exception:
        return ""
    if getattr(other, "errors", ()) and not getattr(result, "errors", ()):
        return ""
    mine, theirs = _file_dates(result), _file_dates(other)
    if not mine or not theirs:
        return ""
    from .paths import today as _today
    now = _today()
    if max(theirs) <= now + timedelta(days=3) and (max(theirs) - max(mine)).days > 45:
        alt = "mdy" if order == "dmy" else "dmy"
        return (f"every date in this file reads both ways, and your date_order setting ({order}) makes its newest date "
                f"{max(mine).isoformat()} while {alt} makes it {max(theirs).isoformat()}. Check one date in the file and import it "
                f"again with date_order={alt} or date_order={order}; nothing was imported")
    return ""


def handle_match(args: dict[str, Any], **_kw: Any) -> str:
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        as_of = _as_of(args.get("as_of"))
        from .paths import today as _today
        if as_of > _today():
            return _fail(f"matching cannot run for a future date ({as_of.isoformat()}; today is {_today().isoformat()}): "
                         "a payment dated after today is not received yet; leave as_of out")
        result = _match_result(store, engine, as_of)
        applied = [m for m in result.applied if store.apply_match(m)]
        totals = {k: {cur: str(v) for cur, v in by_cur.items()} for k, by_cur in result.totals().items()}
        return _json({
            "ok": True, "as_of": as_of.isoformat(),
            "applied": [{"payment_id": m.payment_id, "invoice_number": m.invoice_number, "amount": str(m.amount),
                         "currency": m.currency, "method": m.method, "confidence": m.confidence, "reason": m.reason} for m in applied],
            "review": [_review_dict(i) for i in result.review],
            "credit_notes": [_review_dict(i) for i in result.credit_notes],
            "unmatched": [_review_dict(i) for i in result.unmatched],
            "duplicates": [p.id for p in result.duplicates],
            "customer_identity_questions": store.customer_identity_reviews(),
            "bank_evidence_questions": store.bank_evidence_questions(),
            "return_questions": store.return_reviews(),
            "receipt_placements": store.receipt_placement_reviews(),
            "receipt_overlaps": store.payment_overlaps(),
            "skipped": [[p.id, reason] for p, reason in result.skipped],
            "totals": totals,
        })
    except Exception as exc:
        return _error("matching failed", exc)
    finally:
        _close(store)


def handle_draft(args: dict[str, Any], **_kw: Any) -> str:
    sender = str(args.get("sender") or _config("sender_name", "") or "").strip()
    if not sender:
        return _fail("set sender_name in the plugin settings (the business that signs the reminders) or pass sender")
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        own = "".join(str(_config("payment_iban", "") or "").split()).upper()
        if own and not engine.iban_valid(own):
            return _fail("the payment_iban setting is not a valid IBAN; fix it in the plugin settings before drafting")
        if args.get("bank_checked") and store.bank_refusals():
            store.clear_bank_refusals()  # the person checked that no payment from the refused statement is missing
        with store.transaction():
            store.sync_payment_iban(own)
            as_of = _as_of(args.get("as_of"))
            from .paths import today as _today
            if as_of != _today():
                return _fail(f"drafts are written for today only ({_today().isoformat()}): a reminder dated "
                             f"{as_of.isoformat()} would state days overdue that are not true when it is sent; "
                             "leave as_of out (use cashdesk_status or cashdesk_brief to look at another date)")
            store.refresh_reminders(as_of)
            invoices, customers = store.list_invoices(), store.list_customers()
            superseded = []
            for stale, why in store.stale_drafts():
                if stale.status != "sent":
                    store.reject_draft(stale.id, f"superseded: {why}")
                    superseded.append({"draft_id": stale.id, "reason": why})
            review = _match_result(store, engine, as_of)
            credit_notes = {number: item.detail for item in review.credit_notes for number in item.named}
            holds = store.reminder_holds(as_of)
            plan = engine.plan_followups(invoices, customers, store.verified_matches(), store.chase_state(), as_of,
                                         sender=sender, credit_notes=credit_notes, holds=holds,
                                         reminders_sent=store.reminders_sent(), final_sent=store.final_reminders_sent())
            created = store.create_drafts(plan.all_drafts, as_of=as_of)
            store.save_chase_state(plan.state)
            reminder_kinds = ("stage07", "stage14", "stage21", "stage28")
            new_ids = {d.id for d in plan.all_drafts}
            for newer in plan.all_drafts:
                if newer.kind not in reminder_kinds:
                    continue
                for older in store.list_drafts(customer_id=newer.customer_id):
                    if (older.id not in new_ids and older.kind in reminder_kinds and older.status in ("pending", "approved", "held")
                            and set(older.invoice_numbers) & set(newer.invoice_numbers)):
                        store.reject_draft(older.id, f"superseded by {newer.id}", reset_stage=False)
                        superseded.append({"draft_id": older.id, "reason": f"replaced by the newer reminder {newer.id}"})
            bank_warning = _bank_freshness(store, as_of) if any(d.kind.startswith("stage") for d in plan.all_drafts) else ""
            return _json({
                "ok": True, "as_of": as_of.isoformat(), "created": created,
                "drafts": [_draft_dict(store, d) for d in plan.all_drafts],
                "held": [{"customer_id": h.customer_id, "invoice_numbers": list(h.invoice_numbers), "reason": h.reason, "detail": h.detail} for h in plan.held],
                "not_due": list(plan.not_due), "voided": list(plan.voided),
                "already_drafted": [list(x) for x in plan.already_drafted],
                "superseded": superseded,
                "bank_warning": bank_warning,
                "message": ("Drafts wait in the approval queue (cashdesk_queue, the Desktop page or /cashdesk queue). Nothing was sent."
                            + (" Tell the person first: " + bank_warning if bank_warning else "")),
            })
    except Exception as exc:
        return _error("drafting failed", exc)
    finally:
        _close(store)


def handle_revise(args: dict[str, Any], **_kw: Any) -> str:
    draft_id = str(args.get("draft_id") or "").strip()
    new_text = str(args.get("new_text") or "")
    new_subject = args.get("new_subject")
    if not draft_id or not new_text.strip():
        return _fail("draft_id and new_text are required")
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        draft = store.get_draft(draft_id)
        invoices = []
        for number in draft.invoice_numbers:
            try:
                invoices.append(store.get_invoice(number))
            except engine.NotFoundError:
                pass
        result = engine.revise(draft, new_text, engine.ledger_figures(invoices),
                               new_subject=None if new_subject is None else str(new_subject))
        if not result.ok:
            lock = result.lock
            return _json({
                "ok": False, "rejected": True, "draft_id": draft_id,
                "diff": list(lock.diff),
                "missing": [engine.describe(f) for f in lock.missing],
                "extra": [engine.describe(f) for f in lock.extra],
                "changed": [[engine.describe(a), engine.describe(b)] for a, b in lock.changed],
                "message": ("Figure Lock rejected the revision; the draft is unchanged. " +
                            ("Keep every figure exactly as the ledger has it." if (lock.missing or lock.extra or lock.changed)
                             else "Reword without adding new terms (payee, deadline, fee, link, legal or credit wording) - see diff.")),
            })
        store.replace_draft(result.draft)
        stored = store.get_draft(result.draft.id)
        message = ("Revision accepted, but the reminder stays paused. Resolve the reason and run cashdesk_draft; "
                   "then read and approve the replacement." if stored.status == "held" else
                   "Revision accepted: same figures, new hash. The draft needs a fresh approval.")
        return _json({"ok": True, "draft": _draft_dict(store, stored), "message": message})
    except engine.NotFoundError as exc:
        return _fail(str(exc))
    except engine.StoreError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _error("revision failed", exc)
    finally:
        _close(store)


def handle_reply(args: dict[str, Any], **_kw: Any) -> str:
    customer_id = str(args.get("customer_id") or "").strip()
    text = str(args.get("text") or "")
    if not customer_id or not text.strip():
        return _fail("customer_id and text are required")
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        store.get_customer(customer_id)
        given = str(args.get("kind") or "").strip().lower()
        label = given if given in ("dispute", "paid", "promise", "other") else engine.classify_reply(text, _lang(args.get("lang")))
        held = False
        snoozed_until = None
        if label == "dispute":
            store.set_hold(customer_id, f"dispute raised in a reply: {text.strip()[:120]}")
            held = True
        else:
            store.log_event("reply", customer_id, {"class": label, "text": text.strip()[:500], "by": "model" if given else "keywords"})
        if label == "promise" and args.get("promised_date"):
            try:
                snoozed_until = date.fromisoformat(str(args.get("promised_date")).strip())
            except ValueError:
                return _fail("promised_date must be YYYY-MM-DD")
            if snoozed_until > _as_of(None) + timedelta(days=90):
                return _fail("a promise more than 90 days ahead is not a pause Cash Desk sets on its own; put the customer on hold instead")
            current = store.snoozed(_as_of(None)).get(customer_id)
            if current and current[0] > snoozed_until:
                snoozed_until = current[0]  # an earlier, later-dated promise still stands
            else:
                store.snooze(customer_id, snoozed_until, text.strip()[:120])
        elif label in ("promise", "paid"):
            # Undated promise: "not this week". "We already paid": give the bank statement a few days.
            # A pause only ever extends: a later vague reply never shortens a dated promise.
            wanted = _as_of(None) + timedelta(days=5 if label == "promise" else 3)
            current = store.snoozed(_as_of(None)).get(customer_id)
            if current and current[0] >= wanted:
                snoozed_until = current[0]
            else:
                snoozed_until = wanted
                store.snooze(customer_id, snoozed_until, ("promise without a date: " if label == "promise" else "says already paid: ") + text.strip()[:100])
        advice = {
            "dispute": "Customer put on hold; their drafts are held until a person releases them with /cashdesk release.",
            "paid": (f"No reminder goes to them before {(snoozed_until + timedelta(days=3)).isoformat() if snoozed_until else 'the next run'}. "
                     "Import the next bank statement and run cashdesk_match; if the money is not there, the stages continue."),
            "promise": ("No reminder goes to them until two days after the promised date; then the stages continue."
                        if args.get("promised_date") else
                        f"No reminder goes to them before {(snoozed_until + timedelta(days=3)).isoformat() if snoozed_until else 'the next run'}. "
                        "Pass promised_date (YYYY-MM-DD) to pause until two days after the promised date instead."),
            "other": "Noted; no change to the ledger.",
        }[label]
        return _json({"ok": True, "customer_id": customer_id, "classification": label, "held": held,
                      "paused_until": snoozed_until.isoformat() if snoozed_until else None, "advice": advice})
    except engine.NotFoundError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _error("reply handling failed", exc)
    finally:
        _close(store)


def handle_hold(args: dict[str, Any], **_kw: Any) -> str:
    customer_id = str(args.get("customer_id") or "").strip()
    if not customer_id:
        return _fail("customer_id is required")
    reason = str(args.get("reason") or "held by the agent").strip()
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        store.set_hold(customer_id, reason)
        return _json({"ok": True, "customer_id": customer_id, "reason": reason,
                      "message": "Only a person can release the hold (/cashdesk release or the Desktop page)."})
    except engine.NotFoundError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _error("hold failed", exc)
    finally:
        _close(store)


def _file_label(store: Any, engine: Any, draft: Any) -> str:
    """'2026-11-05 Northgate Sample Studio Ltd - Reminder 7 - INV-2026-0041'"""
    try:
        customer = store.get_customer(draft.customer_id).name
    except Exception:
        customer = draft.customer_id
    label = engine.stage_label(draft.kind, "en")
    from .paths import today

    return f"{today():%Y-%m-%d} {customer} - {label} - {', '.join(draft.invoice_numbers)}"


def handle_send(args: dict[str, Any], **_kw: Any) -> str:
    """Send one approved draft. The hook already gates this call; the handler fails closed again."""
    draft_id = str(args.get("draft_id") or "").strip()
    draft_hash = str(args.get("draft_hash") or "").strip()
    if not draft_id or not draft_hash:
        return _fail("draft_id and draft_hash are required")
    scheduled = _in_scheduled_run()
    if scheduled is None or scheduled:
        return _fail("Cash Desk never sends from a scheduled run or an unknown run context")
    if _host_approvals_off():
        return _fail("Hermes approvals are switched off in this profile; Cash Desk never sends without a human gate")
    weakened = _host_gate_weakened()
    if weakened:
        return _fail(f"Cash Desk will not send: {weakened}")
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        from . import sending
        from .paths import outbox_dir

        draft = store.get_draft(draft_id)
        if draft.hash != draft_hash:
            return _fail("no approval covers this exact text; a person must approve the draft's current hash first")
        if not engine.verify_hash(draft, draft.body, draft.recipients, draft.subject):
            # The stored text was changed outside Cash Desk (e.g. straight in the SQLite file): the
            # approved hash no longer covers what would go out.
            return _fail(f"the stored text of {draft_id} does not match its approved hash; it was changed outside "
                         "Cash Desk. Nothing was sent; re-draft it and have a person approve it again")
        store.check_draft(draft_id, draft_hash)
        approval = store.approval(draft_id)
        if approval is None or approval.hash != draft_hash:
            return _fail("no approval covers this exact text; a person must approve the draft's current hash first")
        if draft.status == "sent":
            return _fail(f"draft {draft_id} was already sent; a draft goes out once")
        if draft.status != "approved":
            return _fail(f"draft {draft_id} is {draft.status}, not approved")
        recipients = sending.clean_recipients(draft.recipients)
        if not recipients:
            return _fail(f"draft {draft_id} has no recipient: the customer needs an e-mail address")
        sender = str(_config("smtp_from", "") or "").strip()
        if not sender:
            return _fail("set the From address (smtp_from) in the plugin settings before sending")
        mode = str(_config("send_mode", "eml") or "eml").strip().lower()
        if mode not in ("eml", "smtp"):
            return _fail(f"send_mode must be eml or smtp, not {mode!r}")
        out = sending.Outgoing(draft_id=draft_id, draft_hash=draft_hash, subject=draft.subject, body=draft.body,
                               recipients=recipients, sender=sender, reply_to=str(_config("reply_to", "") or ""),
                               language=draft.language, file_label=_file_label(store, engine, draft))
        key = store.reserve_send(draft_id, draft_hash, mode)
        try:
            if mode == "smtp":
                from agent.secret_scope import get_secret  # Hermes-provided; the only way the plugin reads a secret

                result = sending.send_smtp(out, host=str(_config("smtp_host", "") or ""), port=int(_config("smtp_port", 587) or 587),
                                           user=str(_config("smtp_user", "") or ""), password=str(get_secret("CASHDESK_SMTP_PASSWORD") or ""),
                                           ca_file=str(_config("smtp_ca_file", "") or ""))
            else:
                result = sending.write_eml(out, outbox_dir(str(_config("eml_outbox", "") or "")))
        except Exception as exc:
            store.fail_send(key, f"{exc.__class__.__name__}: {exc}"[:200])
            return _fail(f"send failed and was recorded as failed: {exc.__class__.__name__}: {exc}", key=key)
        store.confirm_send(key)
        return _json({"ok": True, "draft_id": draft_id, "mode": result.mode, "detail": result.detail, "path": result.path,
                      "message_id": result.message_id, "key": key})
    except engine.StoreError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _error("send failed", exc)
    finally:
        _close(store)


# --- the gate --------------------------------------------------------------------------------------------

def _in_scheduled_run() -> bool | None:
    """True inside a cron execution, False outside, None when Hermes cannot tell us."""
    probe = getattr(_ctx, "current_cron_execution", None)
    if probe is None:
        return None
    try:
        return probe() is not None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cash-desk: current_cron_execution() failed: %s", exc)
        return None


def _host_gate_weakened() -> str | None:
    """Why Hermes's approval prompt would pass a send without a person, or None.

    Hermes auto-approves an escalated tool call with approvals.mode off, with HERMES_YOLO_MODE
    (``hermes chat --yolo``), and in single-query, cron or unattended-platform runs whose
    approvals.*_mode is "approve". Cash Desk refuses to send in all of them. A per-session
    ``/yolo`` toggle is not visible to plugins; the README says so."""
    if str(os.environ.get("HERMES_YOLO_MODE", "")).strip().lower() in ("1", "true", "yes", "on"):
        return "Hermes runs in yolo mode (HERMES_YOLO_MODE), which skips its approval prompt"
    approvals = _host_approvals() or {}
    if str(approvals.get("mode", "manual")).strip().lower() == "off":
        return "Hermes approvals are switched off in this profile (approvals.mode: off)"
    for key in ("single_query_mode", "cron_mode", "unattended_mode"):
        if str(approvals.get(key, "deny")).strip().lower() == "approve":
            return f"approvals.{key} is \"approve\", which lets Hermes pass its prompt with nobody there"
    return None


def _host_approvals() -> dict[str, Any] | None:
    try:
        from hermes_cli.config import load_config_readonly as _load  # documented plugin-side read
    except Exception:
        try:
            from hermes_cli.config import load_config as _load
        except Exception:
            return None
    try:
        value = (_load() or {}).get("approvals", {})
        return value if isinstance(value, dict) else {}
    except Exception:  # pragma: no cover - defensive
        return None


def _host_approvals_off() -> bool | None:
    """True when this profile runs with approvals.mode off (Hermes's gate would auto-pass).

    Read through the documented read-only config loader; None when it cannot be read."""
    try:
        from hermes_cli.config import load_config_readonly as _load  # documented plugin-side read
    except Exception:
        try:
            from hermes_cli.config import load_config as _load
        except Exception:
            return None
    try:
        mode = (_load() or {}).get("approvals", {}).get("mode", "manual")
        return str(mode).strip().lower() == "off"
    except Exception:  # pragma: no cover - defensive
        return None


def _block(message: str) -> dict[str, Any]:
    return {"action": "block", "message": message}


# Subagent sessions. Hermes hides the cron run from a delegated subagent (current_cron_execution()
# returns None there), so a subagent of a scheduled job would look interactive. The public
# subagent_start/subagent_stop hooks name each child session; write tools never run inside one.
_SUBAGENT_SESSIONS: dict[str, None] = {}
_SUBAGENT_LOCK = threading.Lock()
_SUBAGENT_CAP = 2048


def track_subagent_start(child_session_id: str | None = None, **_kw: Any) -> None:
    """subagent_start hook: remember the child's session id."""
    if not child_session_id:
        return
    with _SUBAGENT_LOCK:
        _SUBAGENT_SESSIONS[str(child_session_id)] = None
        while len(_SUBAGENT_SESSIONS) > _SUBAGENT_CAP:
            _SUBAGENT_SESSIONS.pop(next(iter(_SUBAGENT_SESSIONS)))


def track_subagent_stop(child_session_id: str | None = None, **_kw: Any) -> None:
    """subagent_stop hook: forget the child's session id."""
    if not child_session_id:
        return
    with _SUBAGENT_LOCK:
        _SUBAGENT_SESSIONS.pop(str(child_session_id), None)


def _in_subagent(session_id: str | None) -> bool:
    if not session_id:
        return False
    with _SUBAGENT_LOCK:
        return str(session_id) in _SUBAGENT_SESSIONS


def gate_tool_call(tool_name: str, args: dict[str, Any], task_id: str = "", session_id: str | None = None,
                   **_kw: Any) -> dict[str, Any] | None:
    """pre_tool_call hook. Read-only tools pass; writes never run from a scheduled job; the send
    tool is escalated to Hermes's human-approval gate with a rule key bound to the draft's hash.

    Fails closed: when Hermes cannot say whether this is a scheduled run, nothing changes. Write tools
    never run inside a delegated subagent either, because Hermes hides the cron run from subagents."""
    if tool_name not in WRITE_TOOLS:
        return None
    scheduled = _in_scheduled_run()
    if scheduled is None:
        return _block("Cash Desk cannot tell whether this is a scheduled run, so it will not change the ledger or send anything.")
    if scheduled:
        return _block(f"Cash Desk never runs {tool_name} from a scheduled job; scheduled runs only read.")
    if _in_subagent(session_id):
        return _block(f"Cash Desk never runs {tool_name} inside a delegated subagent; ask in the main conversation.")
    if tool_name != SEND_TOOL:
        return None
    weakened = _host_gate_weakened()
    if weakened:
        if "approvals.mode: off" in weakened:
            return _block("Hermes approvals are switched off in this profile (approvals.mode: off). "
                          "Cash Desk never sends without a human gate; set approvals.mode to manual or smart.")
        return _block(f"Cash Desk will not send: {weakened}. It never sends without a person answering Hermes's prompt.")
    draft_hash = str((args or {}).get("draft_hash") or "").strip()
    draft_id = str((args or {}).get("draft_id") or "").strip()
    if not draft_hash or not draft_id:
        return _block("cashdesk_send needs the draft_id and draft_hash of an approved draft.")
    refused = _send_preflight(draft_id, draft_hash)
    if refused:
        # Never ask a person to allow a send that Cash Desk would refuse anyway.
        return _block(f"Cash Desk will not send {draft_id}: {refused}")
    return {
        "action": "approve",
        "message": _send_prompt(str((args or {}).get("draft_id") or "").strip(), draft_hash),
        "rule_key": f"cashdesk:send:{draft_hash}",
    }


def _send_preflight(draft_id: str, draft_hash: str) -> str:
    """The same checks the send makes, before Hermes asks anyone: approval of this exact text,
    current holds, changed figures, stale age. Returns the refusal, or "" when it may be asked."""
    engine, store = _engine(), _store()
    if engine is None or store is None:
        _close(store)
        return ""
    try:
        draft = store.get_draft(draft_id)
        approval = store.approval(draft_id)
        if draft.status == "sent":
            return "it was already sent"
        if approval is None or approval.hash != draft_hash or draft.hash != draft_hash:
            why = store._conn.execute("SELECT reason FROM draft_invalidations WHERE draft_id = ?", (draft_id,)).fetchone()
            if why and why["reason"]:
                return f"its approval was withdrawn: {why['reason']}"
            return "no approval covers this exact text; read the draft and approve it first"
        store.check_draft(draft_id, draft_hash)
        return ""
    except Exception as exc:
        return str(exc)
    finally:
        _close(store)


def _send_prompt(draft_id: str, draft_hash: str) -> str:
    """What Hermes's approval prompt says: which reminder, to whom, for how much, which text."""
    generic = "Cash Desk wants to send a reminder a person approved. Check the queue once more before allowing it."
    engine, store = _engine(), _store()
    if engine is None or store is None or not draft_id:
        _close(store)
        return generic
    try:
        draft = store.get_draft(draft_id)
        customer = _draft_dict(store, draft)["customer"]
        label = engine.stage_label(draft.kind, "en")
        amounts = sorted({f.raw or f.canonical for f in draft.figures if f.kind == "money"}, key=len)
        last = next((f for f in reversed(draft.figures) if f.kind == "money"), None)
        total = engine.format_money(last.amount, last.currency, "en") if last is not None and last.currency else ""
        to = ", ".join(draft.recipients) or "no recipient"
        same = "" if draft.hash.startswith(draft_hash[:12]) else " WARNING: this hash is not the stored draft's; the send will be refused."
        return (f"Cash Desk: send {label} to {customer} <{to}>, subject \"{draft.subject}\""
                f"{(', total ' + total) if total else ''}, text hash {draft.hash[:12]}.{same} "
                "A person approved this exact text; allow only if it should go out now.")
    except Exception:
        return generic
    finally:
        _close(store)


# --- the slash command (human-only actions) --------------------------------------------------------------

def handle_command(raw_args: str = "", **_kw: Any) -> str:
    """/cashdesk: the person's own controls. The model has no tool for approving or releasing."""
    parts = str(raw_args or "").split()
    verb = parts[0].lower() if parts else "status"
    if verb in ("help", "?", "commands"):
        return HELP_TEXT
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return "Cash Desk: the engine is not available in this profile."
    try:
        if verb == "status":
            snap = store.status_snapshot(_as_of(None))
            lines = [f"Cash Desk status for {snap.as_of.isoformat()}"]
            if store.bank_evidence_questions():
                lines.append(store._bank_evidence_reason().capitalize())
            if store.customer_identity_reviews():
                lines.append("Customer IDs disagree between exports: check /cashdesk identities. Reminders, thank-yous and payments for those customers wait.")
            if store.return_reviews():
                lines.append("Returned payment review: verify /cashdesk returns before collection or acknowledgement.")
            if store.surplus_reviews():
                lines.append("Customer surplus review: verify /cashdesk surpluses; customer collection is paused.")
            if store.invoice_import_questions():
                lines.append("Invoice import uncertainty: verify /cashdesk import-errors; affected balances and drafts are held.")
            for cur, buckets in snap.open_by_bucket.items():
                total = sum(buckets.values())
                overdue = sum(v for b, v in buckets.items() if b != "current")
                lines.append(f"  {cur}: open {engine.format_money(total, cur)}, overdue {engine.format_money(overdue, cur)}")
            lines.append(f"  {snap.awaiting_approval} awaiting approval, {snap.approved_unsent} approved and unsent, "
                         f"{snap.held_drafts} held, {snap.on_hold} customer(s) on hold")
            if store.payment_overlaps() or store.receipt_placement_reviews():
                lines.append("Receipt identities need review; balances and received cash are not confirmed. Read /cashdesk overlaps.")
            if store.receipt_conflicts():
                lines.append("Receipt update conflict: retained balances/allocations need bank verification; read /cashdesk receipt-updates.")
            return "\n".join(lines)
        if verb == "queue":
            drafts = store.list_drafts(status="pending")
            if not drafts:
                return "Cash Desk: nothing is waiting for approval."
            lines = [f"{len(drafts)} draft(s) waiting for approval:"]
            for d in drafts:
                customer = _draft_dict(store, d)["customer"]
                label = engine.stage_label(d.kind, "en") if engine else d.kind
                lines.append(f"  {label} · {customer} · {d.subject}\n    /cashdesk show {d.id}")
            lines.append("Read one with /cashdesk show <draft-id>: it prints the exact text and, at the end, the approve command for it.")
            return "\n".join(lines)
        if verb == "show":
            if len(parts) < 2:
                return "Usage: /cashdesk show <draft-id>"
            d = store.get_draft(parts[1])
            customer = _draft_dict(store, d)["customer"]
            return (f"To: {', '.join(d.recipients) or '(internal note, not sent)'}\nSubject: {d.subject}\n\n{d.body}\n"
                    f"---\n{customer} · status {d.status}\n"
                    + (f"If this is the text you want sent: /cashdesk approve {d.id} {d.hash[:12]}" if d.status == "pending" and d.recipients
                       else "Not approvable from here."))
        if verb in ("approve", "reject") and len(parts) < 2:
            return f"Usage: /cashdesk {verb} <draft-id>" + (" <hash-prefix>" if verb == "approve" else " [reason]")
        if verb == "approve":
            draft = store.get_draft(parts[1])
            prefix = parts[2] if len(parts) > 2 else ""
            if len(prefix) < 6:
                return (f"Refused: read the text first with /cashdesk show {draft.id}; it ends with the approve command for that "
                        "exact text, so the approval is for the text you saw.")
            if not draft.hash.startswith(prefix.lower()):
                return f"Refused: {draft.id} now has hash {draft.hash[:12]}, not {prefix}. Re-read the draft before approving."
            store.approve_draft(draft.id, draft.hash, approved_by="slash-command")
            customer = _draft_dict(store, draft)["customer"]
            return (f"Approved {draft.id} ({engine.stage_label(draft.kind, 'en') if engine else draft.kind}, {customer}) with hash {draft.hash[:12]}. "
                    "Ask the agent to send it with cashdesk_send; Hermes will ask you once more.")
        if verb == "reject":
            store.reject_draft(parts[1], " ".join(parts[2:]) or "rejected by the reviewer")
            return f"Rejected {parts[1]}. Draft again later with cashdesk_draft if needed."
        if verb == "overlaps":
            questions = store.payment_overlaps()
            placements = store.receipt_placement_reviews()
            if not questions and not placements:
                return "Cash Desk: no receipt identity questions are pending."
            import json
            lines = ["Possible receipt overlap: these are observed rows, not confirmed extra cash. Verify bank evidence before choosing same or distinct."]
            for question in questions:
                lines.append(f"  {question['id1']} / {question['id2']} · snapshot {question['snapshot']}")
                for row in question['rows']:
                    allocations = ', '.join(f"{m['invoice']} {m['amount']}" for m in row['allocations']) or 'none'
                    lines.append(f"    {row['id']} · {row['date']} · observed {row['currency']} {row['amount']} · payer {row['counterparty'] or 'unknown'} · reference {row['reference'] or 'none'} · file {row['file'] or 'direct/unknown'} · allocated {allocations}" + (" · dismissed" if row['dismissed'] else ""))
                lines.append(f"    /cashdesk receipt {json.dumps(question['id1'], ensure_ascii=False)} {json.dumps(question['id2'], ensure_ascii=False)} <same|distinct> {question['snapshot']}")
            for item in placements:
                lines.append(f"  {item['canonical']} · {item['message']} · snapshot {item['snapshot']}")
                for row in item['rows']:
                    allocations = ', '.join(f"{m['invoice']} {m['amount']}" for m in row['allocations']) or 'none'
                    lines.append(f"    {row['id']} · {row['date']} · observed {row['currency']} {row['amount']} · payer {row['payer']} · reference {row['reference']} · allocated {allocations}")
                lines.append(f"    /cashdesk receipt-keep {json.dumps(item['canonical'], ensure_ascii=False)} {item['snapshot']}")
            lines.append("Same keeps the allocated original (otherwise the older row); it does not move payments. If both already have allocations, Cash Desk refuses same and keeps the conflict paused: an audited allocation correction is required.")
            return "\n".join(lines)
        if verb == "identities":
            import json
            questions=store.customer_identity_reviews()
            if not questions:return "No customer identity questions await verification."
            lines=["Customer identity review: compare original accounting customer IDs, invoice ownership and contacts. Equal names do not prove identity. Unknown historical remaps cannot be reconstructed. A verified owner change is permitted only for an unallocated, wholly unpaid, unsent sale; settled/credit/allocation/sent history needs an audited correction. No approval is restored."]
            for q in questions:
                lines.extend([f"{q['number']}: {q['detail']} · unknown legacy origin {q['legacy_unknown']}",
                              f"  Customers: {q['customer_ids']}; affected invoices: {q['affected_invoices']}",
                              f"  Current invoice: {q['current']}; allocations: {q['allocations']}",
                              f"  Raw observations: {q['observations']}",
                              f"  /cashdesk identity-verify {json.dumps(q['number'])} <verified-customer-id> {q['snapshot']} <verified-accounting-explanation>"])
            return "\n".join(lines)
        if verb == "identity-verify":
            import shlex
            values=shlex.split(str(raw_args))
            if len(values)<5:return "Usage: /cashdesk identity-verify <invoice> <customer-id> <snapshot> <verified-accounting-explanation>; first read /cashdesk identities"
            store.confirm_customer_identity(values[1],customer_id=values[2],expected_snapshot=values[3],reason=" ".join(values[4:]),by="slash-command")
            return "Customer identity verification recorded. Allocation/sent history unchanged; no approval restored. Draft, read and approve fresh text."
        if verb == "returns":
            questions = store.return_reviews()
            if not questions: return "No returned payment questions await verification."
            lines = ["Returned payment review: compare the bank return, original incoming receipt and accounting records. Balances/allocations/sent history are unchanged. A genuine bounced allocation stays held for an audited correction outside this command; do not clear it as unrelated. Only confirm unrelated after verifying that every affected retained receipt remains settled."]
            for q in questions:
                lines.extend([f"{q['id']} · scope {q['scope']} · plausible receipts {q['receipts']}",
                              f"  Affected customers: {q['customer_ids']}; invoices: {q['affected_invoices']}",
                              f"  Raw evidence: {q['evidence']}; bank observations: {q['bank_observations']}",
                              f"  Snapshot: {q['snapshot']}",
                              f"  /cashdesk return-unrelated {q['id']} {q['snapshot']} <verified-bank-explanation>"])
                if q.get("quarantined"):
                    lines.extend([f"  Not yet allocated and held back from matching (may be the returned money): {q['quarantined']}",
                                  *[f"  /cashdesk return-reverses {q['id']} {pid} {q['snapshot']} <verified-bank-explanation>" for pid in q["quarantined"]]])
            return "\n".join(lines)
        if verb == "return-unrelated":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) < 4:
                return "Usage: /cashdesk return-unrelated <id> <snapshot> <verified-bank-explanation>; first verify /cashdesk returns"
            store.confirm_return_unrelated(values[1], expected_snapshot=values[2], reason=" ".join(values[3:]), by="slash-command")
            return "Verified unrelated bank observation recorded. No balances, allocations or sent history changed. Draft/read/approve fresh text; old approval stays revoked."
        if verb == "return-reverses":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) < 5:
                return "Usage: /cashdesk return-reverses <id> <payment-id> <snapshot> <verified-bank-explanation>; first verify /cashdesk returns"
            store.confirm_return_reverses(values[1], values[2], expected_snapshot=values[3], reason=" ".join(values[4:]), by="slash-command")
            return (f"Recorded: the bank returned {values[2]}. It is closed as not received; both bank rows are kept, nothing was allocated, "
                    "and the invoice keeps its full open balance, so chasing resumes on the next draft run.")
        if verb == "not-duplicate":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) < 4:
                return "Usage: /cashdesk not-duplicate <invoice> <other-invoice> <why they are different invoices>"
            store.confirm_distinct_invoices(values[1], values[2], reason=" ".join(values[3:]), by="slash-command")
            return f"Recorded: {values[1]} and {values[2]} are different invoices. Both are chased again from the next draft run."
        if verb == "send-status":
            if len(parts) < 4 or parts[2] not in ("sent", "failed"):
                return ("Usage: /cashdesk send-status <draft-id> sent|failed <what you checked>  (only for a send left in progress, "
                        "for example after the gateway stopped mid-send: check your Sent folder or outbox first)")
            draft = store.get_draft(parts[1])
            key = engine.idempotency_key(draft.id, draft.hash)
            row = store._conn.execute("SELECT status FROM sends WHERE idempotency_key=?", (key,)).fetchone()
            if row is None or row["status"] != "reserved":
                return f"Refused: {draft.id} has no send in progress; nothing to resolve."
            note = " ".join(parts[3:])
            if parts[2] == "sent":
                store.confirm_send(key)
                store.log_event("send_resolved_by_person", draft.id, {"outcome": "sent", "note": note})
                return f"Recorded: {draft.id} went out ({note}). It will not be sent again."
            store.fail_send(key, f"resolved by a person: {note}"[:200])
            return f"Recorded: {draft.id} did not go out ({note}). It can be sent again with cashdesk_send."
        if verb == "still-open":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) < 3:
                return "Usage: /cashdesk still-open <invoice> <why you know it is still owed>; only for an invoice held as not in the latest export"
            store.confirm_still_open(values[1], reason=" ".join(values[2:]), by="slash-command")
            return (f"Recorded: {values[1]} is still owed although the latest export did not list it. It is chased again from the next "
                    "draft run; nothing else changed.")
        if verb == "surpluses":
            questions = store.surplus_reviews()
            if not questions: return "No customer surplus questions await verification."
            import json
            lines = ["Customer surplus review: verify the accounting export, all versions/currencies and any refund or set-off outside Cash Desk. Observations are NOT summed or allocated. Confirm only once the surplus is accounted for in the accounting records; no money or sent history is changed."]
            for q in questions:
                lines.extend([f"{q['number']}: {q['message']}", f"  Affected customers: {q['customer_ids']}; invoices: {q['affected_invoices']}", f"  Current invoice: {q['current']}",
                              f"  Settlement basis: {q['settlements']}; local allocations: {q['allocations']}",
                              f"  Raw observations: {q['observations']}",
                              f"  /cashdesk surplus-accounted {json.dumps(q['number'],ensure_ascii=False)} {q['snapshot']}"])
            return "\n".join(lines)
        if verb == "surplus-accounted":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) != 3:
                return "Usage: /cashdesk surplus-accounted <invoice> <snapshot>; first verify the credit is accounted for via /cashdesk surpluses"
            store.confirm_surplus(values[1], expected_snapshot=values[2], by="slash-command")
            return "Customer surplus accounting verification recorded. No money, allocations or sent history changed. Draft/read/approve fresh text; old approval stays revoked."
        if verb == "bank-evidence":
            questions = store.bank_evidence_questions()
            if not questions: return "No bank payer/reference questions await verification."
            lines = [store._bank_evidence_reason().capitalize()+". Compare every alias/currency source with the original bank statement and current ledger. Correct and import verified source rows separately; no automatic undo or restored approval."]
            for q in questions:
                lines.extend([f"{q['id']} · {q['file']} row {q['row_number']} · {q['detail']}",
                              f"  Named invoices: {q['named']}; plausible customers: {q['customer_ids']}; scope: {q['scope']}",
                              f"  Raw evidence: {q['raw']}\n  Original cells: {q['evidence']}",
                              f"  /cashdesk bank-evidence-verify {q['id']} {q['snapshot']} <verified-bank-explanation>"])
            return "\n".join(lines)
        if verb == "bank-evidence-verify":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) < 4:
                return "Usage: /cashdesk bank-evidence-verify <id> <snapshot> <verified-bank-explanation>; first verify /cashdesk bank-evidence"
            store.confirm_bank_evidence(values[1],expected_snapshot=values[2],reason=" ".join(values[3:]),by="slash-command")
            return "Bank evidence verification recorded. No balances, allocations or sent history changed. Import corrected cash separately, then draft/read/approve fresh text; old approvals remain revoked."
        if verb == "import-errors":
            questions = store.invoice_import_questions()
            if not questions: return "No invoice import errors await verification."
            lines = ["Invoice import uncertainty: compare each failed row with the original accounting export. Correct and re-import its source row, or explicitly confirm that the current ledger is verified. Confirmation changes no financial rows and never restores approval."]
            for q in questions:
                lines.append(f"{q['id']} · {q['source']} / {q['file']} row {q['row_number']} · invoice {q['number'] or 'unknown: source-wide hold'} · {q['detail']}\n  Raw evidence: {q['raw']}\n  /cashdesk import-error {q['id']} confirm {q['snapshot']}")
            return "\n".join(lines)
        if verb == "import-error":
            if len(parts) != 4 or parts[2] != "confirm":
                return "Usage: /cashdesk import-error <id> confirm <snapshot>; first verify the original export and current ledger via /cashdesk import-errors"
            store.confirm_invoice_import_error(parts[1], expected_snapshot=parts[3], by="slash-command")
            return "Invoice import evidence verified and audited. No financial rows changed. Draft again, read and approve fresh text; old approvals remain revoked."
        if verb == "receipt-updates":
            import json
            questions = store.receipt_conflicts()
            if not questions: return "No receipt update conflicts await verification."
            lines = ["Receipt update conflicts: verify bank/account, transaction and settlement evidence. Allocated corrections need reconciliation; no automatic undo."]
            for q in questions:
                lines.extend([f"{q['original']} -> {q['observation']}: {q['reason']} ({q['status']})",
                              f"  Before: {q['before']}",f"  Observed: {q['after']}",f"  Raw evidence: {q['evidence']}", f"  Currency evidence: {q['currency_evidence']}", f"  Allocations: {q['allocations']}; dismissals: {q['dismissals']}",
                              f"  /cashdesk receipt-update {json.dumps(q['original'],ensure_ascii=False)} {json.dumps(q['observation'],ensure_ascii=False)} keep|distinct|replace {q['snapshot']}"])
            return "\n".join(lines)
        if verb == "receipt-update":
            import shlex
            values = shlex.split(str(raw_args))
            if len(values) != 5:
                return "Usage: /cashdesk receipt-update <original> <observation> keep|distinct|replace <snapshot>; verify the bank evidence first"
            store.resolve_receipt_update(values[1],values[2],values[3],expected_snapshot=values[4],by="slash-command")
            return "Receipt evidence decision recorded. No allocation was reversed. Run match/draft and approve fresh text; old approval remains revoked."
        if verb == "receipt-keep":
            import shlex
            try:
                values = shlex.split(str(raw_args))
            except ValueError:
                values = []
            if len(values) != 3:
                return "Usage: /cashdesk receipt-keep <canonical-id> <snapshot-from-overlaps> (confirms existing completed allocation/disposition only)"
            store.confirm_receipt_placement(values[1], expected_snapshot=values[2], by="slash-command")
            return "Existing receipt allocation/disposition confirmed. No money moved; draft again, read and approve fresh text."
        if verb == "receipt":
            import shlex
            try:
                values = shlex.split(str(raw_args))
            except ValueError:
                values = []
            if len(values) != 5:
                return "Usage: /cashdesk receipt <id1> <id2> same|distinct <snapshot-from-overlaps> (quote IDs containing spaces)"
            result = store.resolve_overlap(values[1], values[2], values[3], expected_snapshot=values[4], by="slash-command")
            detail = f"Same receipt; use {result['canonical']}" if result['decision'] == 'same' else "Distinct receipts; both original records remain"
            return detail + ". Decisions are audited. Run cashdesk_match, then draft again, read and approve fresh text; old approvals stay invalid."
        if verb == "settlements":
            reviews = store.settlement_reviews()
            if not reviews:
                return "Cash Desk: no invoice settlements need reconciliation."
            lines = ["Verify payments outside Cash Desk, excluding all local allocations below. Nothing changes until you reconcile:"]
            for number, reason in reviews.items():
                inv = store.get_invoice(number)
                local = sum((m.amount for m in store.list_matches() if m.invoice_number == number), engine.to_money("0"))
                token = store.settlement_snapshot(number)
                lines.append(f"  {number} · customer {inv.customer_id} · gross {engine.format_money(inv.amount, inv.currency)} · "
                             f"open {engine.format_money(inv.balance, inv.currency)} · local {engine.format_money(local, inv.currency)} · snapshot {token}\n    {reason}")
                latest = next((e for e in reversed(store.events(1000)) if e.kind == "settlement_review" and e.ref == number), None)
                if latest is not None:
                    try:
                        exported = engine.to_money(latest.detail["incoming_amount"]) - engine.to_money(latest.detail["incoming_balance"])
                        likely = exported - local
                    except Exception:
                        likely = None
                    if likely is not None and likely >= 0:
                        lines.append(f"    The latest export shows {engine.format_money(exported, inv.currency)} paid. If that already includes the "
                                     f"{engine.format_money(local, inv.currency)} applied here (the usual case), run:")
                        lines.append(f"    /cashdesk reconcile {number} {likely} {token}")
                        lines.append("    Otherwise enter what was paid outside Cash Desk:")
                lines.append(f"    /cashdesk reconcile {number} <verified-external-paid> {token}")
            return "\n".join(lines)
        if verb == "reconcile":
            if len(parts) != 4:
                return "Usage: /cashdesk reconcile <invoice> <verified-external-paid-excluding-local> <snapshot-from-settlements>"
            inv = store.get_invoice(parts[1])
            if parts[3] != store.settlement_snapshot(inv.number):
                return "Refused: settlement snapshot changed; read /cashdesk settlements again"
            parsed = engine.parse_amount(parts[2], "en")
            if parsed is None or (parsed.currency and parsed.currency != inv.currency):
                return "Refused: enter a verified external settlement in the invoice currency"
            store.reconcile_settlement(inv.number, parsed.value, expected_balance=inv.balance, currency=inv.currency,
                                       expected_snapshot=parts[3], by="slash-command")
            current = store.get_invoice(inv.number)
            return (f"Reconciled {inv.number}: {engine.format_money(current.balance, current.currency)} open. "
                    "Local payments are unchanged. Run cashdesk_draft, read and approve the fresh text.")
        if verb == "review":
            result = _match_result(store, engine, _as_of(None))
            items = [*result.review, *result.unmatched]
            if store.bank_evidence_questions():
                return store._bank_evidence_reason().capitalize()
            if not items:
                if store.customer_identity_reviews():
                    return "Customer identity review: verify /cashdesk identities before collection or acknowledgement."
                if store.return_reviews():
                    return "Returned payment review: bank observations await verification via /cashdesk returns; no return is incoming cash to Apply."
                return "Cash Desk: no payments are waiting for a person."
            lines = [f"{len(items)} payment(s) waiting for a person (nothing here is applied):"]
            for item in items:
                p = item.payment
                lines.append(f"  {p.id} · {p.date.isoformat()} · {engine.format_money(p.amount, p.currency)} from {p.counterparty or 'unknown payer'} · {item.kind}: {item.detail}")
                for c in item.candidates[:3]:
                    lines.append(f"    closest: {c.invoice_number} ({engine.format_money(c.balance, c.currency)} open)")
            if store.return_reviews():
                lines.append("Returned payment review: verify /cashdesk returns before collection or acknowledgement. Return observations cannot be applied as incoming cash.")
            if any(item.kind == "POSSIBLE_DUPLICATE" for item in items):
                lines.append("Resolve possible receipt overlap with /cashdesk overlaps; these rows cannot be applied or dismissed yet.")
            if any(i.payment.amount > 0 and i.kind not in ("RETURNED_PAYMENT", "POSSIBLE_DUPLICATE", "RECEIPT_UPDATE", "INVOICE_IMPORT", "CUSTOMER_IDENTITY", "BANK_EVIDENCE") for i in items):
                lines.append("Apply one with /cashdesk apply <payment-id> <invoice> [amount], or /cashdesk dismiss <payment-id> [reason].")
            return "\n".join(lines)
        if verb == "apply":
            if len(parts) < 3:
                return "Usage: /cashdesk apply <payment-id> <invoice> [amount]"
            amount = None
            if len(parts) > 3:
                parsed = engine.parse_amount(parts[3], "en")
                if parsed is None:
                    return f"Refused: {parts[3]!r} is not an amount."
                amount = parsed.value
            match = store.apply_by_person(parts[1], parts[2], amount, by="slash-command")
            inv = store.get_invoice(parts[2])
            left = "paid in full" if inv.status == "paid" else f"{engine.format_money(inv.balance, inv.currency)} still open"
            return (f"Applied {engine.format_money(match.amount, match.currency)} of {parts[1]} to {parts[2]}: {left}. "
                    "Reminders that stated the old amount are now stale and are redrafted on the next cashdesk_draft.")
        if verb == "dismiss":
            if len(parts) < 2:
                return "Usage: /cashdesk dismiss <payment-id> [reason]"
            store.dismiss_payment(parts[1], " ".join(parts[2:]) or "dismissed by the reviewer")
            return f"Dismissed {parts[1]}: it no longer waits for review and pays no invoice."
        if verb in ("hold", "release") and len(parts) < 2:
            return f"Usage: /cashdesk {verb} <customer-id>" + (" [reason]" if verb == "hold" else "")
        if verb == "hold":
            store.set_hold(parts[1], " ".join(parts[2:]) or "held by the reviewer")
            return f"{parts[1]} is on hold; their drafts are held and nothing goes out."
        if verb == "release":
            store.clear_hold(parts[1])
            return f"{parts[1]} released; held drafts are pending again and need a fresh approval."
        return f"Unknown command {verb!r}.\n\n" + HELP_TEXT
    except engine.StoreError as exc:
        return f"Refused: {exc}"
    except Exception as exc:
        return f"Cash Desk command failed: {exc.__class__.__name__}: {exc}"
    finally:
        _close(store)


# --- registration ----------------------------------------------------------------------------------------

TOOLS: tuple[tuple[str, dict[str, Any], Any, str, str], ...] = (
    ("cashdesk_status", STATUS_SCHEMA, handle_status, "Cash Desk ledger summary (read-only)", "💶"),
    ("cashdesk_brief", BRIEF_SCHEMA, handle_brief, "Cash Desk morning brief (read-only)", "☕"),
    ("cashdesk_queue", QUEUE_SCHEMA, handle_queue, "Drafts by status with exact text and hash (read-only)", "📋"),
    ("cashdesk_import", IMPORT_SCHEMA, handle_import, "Import a customers, invoices or bank CSV", "📥"),
    ("cashdesk_match", MATCH_SCHEMA, handle_match, "Match payments to invoices", "🔗"),
    ("cashdesk_draft", DRAFT_SCHEMA, handle_draft, "Plan and store today's reminder drafts", "✍️"),
    ("cashdesk_revise", REVISE_SCHEMA, handle_revise, "Reword a draft under Figure Lock", "🔒"),
    ("cashdesk_reply", REPLY_SCHEMA, handle_reply, "Classify a customer reply; a dispute holds the customer", "📨"),
    ("cashdesk_hold", HOLD_SCHEMA, handle_hold, "Put a customer on hold", "⏸️"),
    (SEND_TOOL, SEND_SCHEMA, handle_send, "Send one approved draft (gated)", "📤"),
)
TOOL_NAMES = tuple(t[0] for t in TOOLS)


def _small_balance() -> Any:
    """Open balances under this are not chased automatically (bank fees, rounding). Default 10.00."""
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(_config("ignore_balances_below", "10.00") or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("10.00")


def _register_blueprint(ctx: Any) -> None:
    register = getattr(ctx, "register_automation_blueprint", None)
    if register is None:
        logger.info("cash-desk: this Hermes has no automation blueprints; skipping the morning brief")
        return
    try:
        register(
            "morning-brief",
            title="Cash Desk morning brief",
            description=("Every weekday morning: payments received, newly overdue invoices, drafts "
                         "awaiting your approval and customers on hold. Read-only; it never sends a reminder."),
            category="work",
            schedule_template="{minute} {hour} * * {dow}",
            prompt_template=("Run the Cash Desk morning brief in {lang}: call the cashdesk_brief tool with "
                             "lang={lang} and relay its text exactly, with nothing added. Do not import, match, draft, "
                             "revise, approve or send anything in this run."),
            slots=[
                {"name": "time", "type": "time", "label": "What time?", "default": "06:43"},
                {"name": "recurrence", "type": "weekdays", "label": "Repeat on", "default": "weekdays",
                 "options": ["everyday", "weekdays", "weekends"]},
                {"name": "lang", "type": "enum", "label": "Language", "default": "en", "options": list(LANGS)},
                {"name": "deliver", "type": "enum", "label": "Where to deliver?", "default": "origin",
                 "options": ["origin", "local"], "strict": False},
            ],
            tags=["finance", "daily"],
        )
    except Exception as exc:
        logger.warning("cash-desk: morning-brief blueprint not registered: %s", exc)


def _register_command(ctx: Any) -> None:
    register = getattr(ctx, "register_command", None)
    if register is None:
        logger.info("cash-desk: this Hermes has no slash commands; approve from the Desktop page")
        return
    try:
        register("cashdesk", handle_command,
                 description="Cash Desk: your own controls (approve, review, hold...). /cashdesk help lists every command",
                 args_hint="help | status | queue | approve <draft-id> <hash-prefix> | reject <draft-id> | review | apply <payment-id> <invoice> | dismiss <payment-id> | hold <customer-id> | release <customer-id>")
    except Exception as exc:
        logger.warning("cash-desk: /cashdesk not registered: %s", exc)


# --- the reply guard (Figure Lock on the agent's own replies) -------------------------------------

_INVOICE_NO_HINT = re.compile(r"\b[A-Z]{2,5}-\d{2,}(?:-\d+)*\b")
_CZECH_LETTERS = re.compile(r"[ěščřžýáíéůúňťď]", re.IGNORECASE)


def _guard_on() -> bool:
    value = _config("guard_outgoing_messages", False)
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _withheld(reason: str) -> str:
    return (f"Cash Desk withheld this reply: {reason} Nothing was sent. Check the figures with "
            "cashdesk_queue or cashdesk_status and answer again with the ledger's figures.")


def guard_reply(response_text: str = "", **_kw: Any) -> str | None:
    """transform_llm_output: withhold a reply about a ledger customer that states an amount, IBAN
    or invoice number the ledger does not hold for that customer. Returns None to leave it alone."""
    if not _guard_on() or not response_text or not response_text.strip():
        return None
    text = response_text
    engine, store = _engine(), None
    try:
        store = _store()
        if engine is None or store is None:
            raise RuntimeError("the ledger is not available")
        customers = store.list_customers()
        invoices = store.list_invoices()
        payments = store.list_payments()
        try:  # what the brief and the status say are ledger figures too (totals, buckets, payments in review)
            ledger_text = engine.morning_brief(store, _as_of(None), "en")
        except Exception:  # pragma: no cover - defensive
            ledger_text = ""
    except Exception as exc:
        if _INVOICE_NO_HINT.search(text):
            logger.warning("cash-desk: reply guard could not read the ledger: %s", exc)
            return _withheld("it quotes an invoice number and Cash Desk could not read the ledger to check it.")
        return None
    finally:
        if store is not None:
            _close(store)
    lang = "cs" if _CZECH_LETTERS.search(text) else "en"
    lowered = text.lower()
    found = engine.extract_figures(text, lang)
    known_numbers = {inv.number.upper(): inv for inv in invoices}
    quoted = [f for f in found if f.kind == "invoice_no"]
    named = {inv.customer_id for f in quoted if (inv := known_numbers.get(f.canonical.upper()))}
    for c in customers:
        first = c.name.split()[0] if c.name.split() else ""
        if c.name.lower() in lowered or (len(first) >= 5 and re.search(rf"\b{re.escape(first.lower())}\b", lowered)):
            named.add(c.id)
    if not named and not quoted:
        return None  # not about the ledger: never touched
    scope = [inv for inv in invoices if inv.customer_id in named] if named else []
    totals: dict[tuple[str, str], Any] = {}
    for inv in scope:
        if inv.balance > 0:
            key = (inv.customer_id, inv.currency)
            totals[key] = totals.get(key, 0) + inv.balance
    extra = [engine.Figure.money(amount, cur) for (_cid, cur), amount in totals.items()]
    names_of = {c.id: c.name for c in customers}
    for p in payments:
        # a payment's own amount and id (B1-02, EUR 700.00) are facts from the bank file: allowed in a summary,
        # or in a reply about the one customer whose name fits the payer
        if len(named) != 1 or engine.payer_fits(names_of.get(next(iter(named)), ""), p.counterparty):
            extra.append(engine.Figure.money(abs(p.amount), p.currency))
            extra.extend(engine.extract_figures(p.id, "en"))
    if len(named) > 1:  # a summary across customers (the morning brief): its totals and buckets are ledger figures
        for brief in (ledger_text, *_RECENT_BRIEFS):
            extra.extend(engine.extract_figures(brief, "en"))
    allowed = engine.ledger_figures(scope, extra)
    unknown = engine.unknown_figures(text, lang, allowed)
    if not unknown:
        return None
    names = ", ".join(sorted(c.name for c in customers if c.id in named))
    listed = "; ".join(dict.fromkeys(engine.describe(f) for f in unknown))
    whose = f" for {names}" if names else ""
    return _withheld(f"it states figures the ledger does not hold{whose}: {listed}.")


def _register_reply_guard(ctx: Any) -> None:
    """Always registered (so the declared hooks match); it only acts, and only holds the stream so
    a wrong figure is never shown before the rewrite, when guard_outgoing_messages is on."""
    hold = _guard_on()
    try:
        ctx.register_hook("transform_llm_output", guard_reply, hold_stream=hold)
    except TypeError:  # a Hermes without hold_stream (before 0.22.6)
        ctx.register_hook("transform_llm_output", guard_reply)


def register(ctx: Any) -> None:
    """Called once by the plugin loader. Declared tools and hooks match plugin.yaml exactly."""
    global _ctx
    _ctx = ctx
    for name, schema, handler, description, emoji in TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler, description=description, emoji=emoji)
    ctx.register_hook("pre_tool_call", gate_tool_call)
    ctx.register_hook("subagent_start", track_subagent_start)
    ctx.register_hook("subagent_stop", track_subagent_stop)
    _register_reply_guard(ctx)
    _register_command(ctx)
    _register_blueprint(ctx)
    logger.info("cash-desk registered (engine %s)", engine_version())
