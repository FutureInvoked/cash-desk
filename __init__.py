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
import re
import threading
import logging
from datetime import date
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
COMMAND_HELP = (
    "/cashdesk status | queue | approve <draft-id> [hash-prefix] | reject <draft-id> [reason] | "
    "hold <customer-id> [reason] | release <customer-id>"
)

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
        "default_currency": {"type": "string", "description": "Currency for rows without one (default: the currency setting, EUR unless changed)"},
        "date_order": {"type": "string", "enum": ["auto", "dmy", "mdy"], "description": "How to read slashed dates (default auto)"},
        "allow_errors": {"type": "boolean", "description": "Import the good rows even when some rows have errors (default false)"},
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
    "days overdue in the customer's language, thank-you notes for payments received, and an internal "
    "escalation note past 30 days. Customers on hold or in dispute get nothing. Every draft waits for a "
    "person's approval; nothing is sent.",
    {"as_of": AS_OF, "sender": {"type": "string", "description": "Business name that signs the drafts (default: the sender_name setting)"}},
)
QUEUE_SCHEMA = _schema(
    "cashdesk_queue",
    "List drafts by status (pending by default) with their exact subject, body, recipients, verified figures "
    "and hash. Use the hash when asking a person to approve or when sending.",
    {"status": {"type": "string", "enum": list(QUEUE_STATUSES)}, "customer_id": {"type": "string"}},
)
REVISE_SCHEMA = _schema(
    "cashdesk_revise",
    "Reword a draft. Figure Lock checks the new text: every amount, invoice number, due date, IBAN and "
    "payment reference must still match the ledger exactly, with nothing added, changed or missing. A "
    "rejected revision returns the precise diff and leaves the draft untouched; an accepted one gets a new "
    "hash and needs a fresh approval.",
    {
        "draft_id": {"type": "string"},
        "new_text": {"type": "string", "description": "The complete new body"},
        "new_subject": {"type": "string", "description": "Optional new subject line"},
    },
    ["draft_id", "new_text"],
)
REPLY_SCHEMA = _schema(
    "cashdesk_reply",
    "Classify a customer's reply as dispute, paid, promise or other. A dispute puts the customer on hold so "
    "no reminder goes out until a person releases them.",
    {"customer_id": {"type": "string"}, "text": {"type": "string"}, "lang": {"type": "string", "enum": list(LANGS)}},
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
    from .paths import store_path

    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return engine.Store(path)


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
        "figures": [{"kind": f.kind, "value": f.canonical, "ok": True} for f in draft.figures],
    }


def _payment_dict(payment: Any) -> dict[str, Any]:
    return {"id": payment.id, "date": payment.date.isoformat(), "amount": str(payment.amount), "currency": payment.currency,
            "reference": payment.reference, "counterparty": payment.counterparty, "memo": payment.memo}


def _review_dict(item: Any) -> dict[str, Any]:
    return {
        "payment": _payment_dict(item.payment), "kind": item.kind, "detail": item.detail, "named": list(item.named),
        "candidates": [{"invoice_number": c.invoice_number, "customer_id": c.customer_id, "score": round(c.score, 2),
                        "balance": str(c.balance), "currency": c.currency} for c in item.candidates],
        "holds": list(item.holds),
    }


def _match_result(store: Any, engine: Any, as_of: date) -> Any:
    """Read-only matching pass over the unapplied payments."""
    return engine.match_payments(store.list_invoices(), store.unapplied_payments(), store.list_customers(), as_of)


# --- read-only tools -----------------------------------------------------------------------------------

def handle_status(args: dict[str, Any], **_kw: Any) -> str:
    try:
        store = _store()
        if store is None:
            return _json({"plugin": PLUGIN_NAME, "engine": False, "engine_version": engine_version(), "ledger": "empty"})
        try:
            snapshot = store.status_snapshot(_as_of(args.get("as_of"))).to_dict()
            imports = store.list_imports()
        finally:
            _close(store)
        snapshot.update({"plugin": PLUGIN_NAME, "engine": True, "engine_version": engine_version(),
                         "ledger": "ok" if imports else "empty", "imports": len(imports)})
        return _json(snapshot)
    except Exception as exc:
        return _error("status failed", exc)


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
                    store.record_brief()
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

def handle_import(args: dict[str, Any], **_kw: Any) -> str:
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
    date_order = str(args.get("date_order") or "auto").strip().lower()
    allow_errors = bool(args.get("allow_errors"))
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        if kind == "customers":
            result = engine.read_customers(path)
        elif kind == "invoices":
            result = engine.read_invoices(source, path, default_currency=currency, date_order=date_order)
        else:
            result = engine.read_bank_csv(path, default_currency=currency, date_order=date_order)
        if is_demo_file(path):
            result = on_today(in_currency(result, setting))
        errors = [{"row": e.row, "message": e.message} for e in result.errors]
        if result.errors and not allow_errors:
            return _fail(f"the file has {len(result.errors)} row error(s); fix it or pass allow_errors=true",
                         kind=kind, rows=result.rows, errors=errors[:50])
        counts = store.ingest(result, allow_errors=allow_errors, file_name=path.name)
    except engine.DuplicateImportError as exc:
        return _fail(f"already imported: {exc}", duplicate=True)
    except Exception as exc:
        return _error("import failed", exc)
    finally:
        _close(store)
    return _json({"ok": True, "kind": kind, "source": result.source, "file": path.name, "rows": result.rows,
                  "imported": counts, "skipped": len(result.skipped), "errors": errors, "fingerprint": result.fingerprint[:12]})


def handle_match(args: dict[str, Any], **_kw: Any) -> str:
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        as_of = _as_of(args.get("as_of"))
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
        as_of = _as_of(args.get("as_of"))
        invoices, customers = store.list_invoices(), store.list_customers()
        review = _match_result(store, engine, as_of)
        credit_notes = {number: item.detail for item in review.credit_notes for number in item.named}
        plan = engine.plan_followups(invoices, customers, store.list_matches(), store.chase_state(), as_of,
                                     sender=sender, credit_notes=credit_notes, holds=review.held_invoices)
        created = store.create_drafts(plan.all_drafts)
        store.save_chase_state(plan.state)
        return _json({
            "ok": True, "as_of": as_of.isoformat(), "created": created,
            "drafts": [_draft_dict(store, d) for d in plan.all_drafts],
            "held": [{"customer_id": h.customer_id, "invoice_numbers": list(h.invoice_numbers), "reason": h.reason, "detail": h.detail} for h in plan.held],
            "not_due": list(plan.not_due), "voided": list(plan.voided),
            "already_drafted": [list(x) for x in plan.already_drafted],
            "message": "Drafts wait in the approval queue (cashdesk_queue, the Desktop page or /cashdesk queue). Nothing was sent.",
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
                "message": "Figure Lock rejected the revision; the draft is unchanged. Keep every figure exactly as the ledger has it.",
            })
        store.replace_draft(result.draft)
        return _json({"ok": True, "draft": _draft_dict(store, result.draft),
                      "message": "Revision accepted: same figures, new hash. The draft needs a fresh approval."})
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
        label = engine.classify_reply(text, _lang(args.get("lang")))
        held = False
        if label == "dispute":
            store.set_hold(customer_id, f"dispute raised in a reply: {text.strip()[:120]}")
            held = True
        else:
            store.log_event("reply", customer_id, {"class": label, "text": text.strip()[:500]})
        advice = {
            "dispute": "Customer put on hold; their drafts are held until a person releases them with /cashdesk release.",
            "paid": "Import the next bank statement and run cashdesk_match before drafting again.",
            "promise": "Noted in the ledger's events; the next reminder stage still follows the due dates.",
            "other": "Noted; no change to the ledger.",
        }[label]
        return _json({"ok": True, "customer_id": customer_id, "classification": label, "held": held, "advice": advice})
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
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return _fail("the Cash Desk engine is not available")
    try:
        from . import sending
        from .paths import outbox_dir

        draft = store.get_draft(draft_id)
        approval = store.approval(draft_id)
        if draft.hash != draft_hash or approval is None or approval.hash != draft_hash:
            return _fail("no approval covers this exact text; a person must approve the draft's current hash first")
        if not engine.verify_hash(draft, draft.body, draft.recipients, draft.subject):
            # The stored text was changed outside Cash Desk (e.g. straight in the SQLite file): the
            # approved hash no longer covers what would go out.
            return _fail(f"the stored text of {draft_id} does not match its approved hash; it was changed outside "
                         "Cash Desk. Nothing was sent; re-draft it and have a person approve it again")
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
    if _host_approvals_off():
        return _block("Hermes approvals are switched off in this profile (approvals.mode: off). "
                      "Cash Desk never sends without a human gate; set approvals.mode to manual or smart.")
    draft_hash = str((args or {}).get("draft_hash") or "").strip()
    if not draft_hash:
        return _block("cashdesk_send needs the draft_hash of an approved draft.")
    return {
        "action": "approve",
        "message": "Cash Desk wants to send a reminder a person approved. Check the queue once more before allowing it.",
        "rule_key": f"cashdesk:send:{draft_hash}",
    }


# --- the slash command (human-only actions) --------------------------------------------------------------

def handle_command(raw_args: str = "", **_kw: Any) -> str:
    """/cashdesk: the person's own controls. The model has no tool for approving or releasing."""
    parts = str(raw_args or "").split()
    verb = parts[0].lower() if parts else "status"
    engine, store = _engine(), _store()
    if engine is None or store is None:
        return "Cash Desk: the engine is not available in this profile."
    try:
        if verb == "status":
            snap = store.status_snapshot(_as_of(None))
            lines = [f"Cash Desk status for {snap.as_of.isoformat()}"]
            for cur, buckets in snap.open_by_bucket.items():
                total = sum(buckets.values())
                overdue = sum(v for b, v in buckets.items() if b != "current")
                lines.append(f"  {cur}: open {engine.format_money(total, cur)}, overdue {engine.format_money(overdue, cur)}")
            lines.append(f"  {snap.awaiting_approval} awaiting approval, {snap.approved_unsent} approved and unsent, "
                         f"{snap.held_drafts} held, {snap.on_hold} customer(s) on hold")
            return "\n".join(lines)
        if verb == "queue":
            drafts = store.list_drafts(status="pending")
            if not drafts:
                return "Cash Desk: nothing is waiting for approval."
            lines = [f"{len(drafts)} draft(s) waiting for approval:"]
            for d in drafts:
                customer = _draft_dict(store, d)["customer"]
                label = engine.stage_label(d.kind, "en") if engine else d.kind
                lines.append(f"  {label} · {customer} · {d.subject}\n    id {d.id} · hash {d.hash[:12]}")
            lines.append("Approve one with /cashdesk approve <draft-id> [hash-prefix] after reading it (cashdesk_queue shows the text).")
            return "\n".join(lines)
        if verb in ("approve", "reject") and len(parts) < 2:
            return f"Usage: /cashdesk {verb} <draft-id>" + (" [hash-prefix]" if verb == "approve" else " [reason]")
        if verb == "approve":
            draft = store.get_draft(parts[1])
            prefix = parts[2] if len(parts) > 2 else ""
            if prefix and not draft.hash.startswith(prefix.lower()):
                return f"Refused: {draft.id} now has hash {draft.hash[:12]}, not {prefix}. Re-read the draft before approving."
            store.approve_draft(draft.id, draft.hash, approved_by="slash-command")
            customer = _draft_dict(store, draft)["customer"]
            return (f"Approved {draft.id} ({engine.stage_label(draft.kind, 'en') if engine else draft.kind}, {customer}) with hash {draft.hash[:12]}. "
                    "Ask the agent to send it with cashdesk_send; Hermes will ask you once more.")
        if verb == "reject":
            store.reject_draft(parts[1], " ".join(parts[2:]) or "rejected by the reviewer")
            return f"Rejected {parts[1]}. Draft again later with cashdesk_draft if needed."
        if verb in ("hold", "release") and len(parts) < 2:
            return f"Usage: /cashdesk {verb} <customer-id>" + (" [reason]" if verb == "hold" else "")
        if verb == "hold":
            store.set_hold(parts[1], " ".join(parts[2:]) or "held by the reviewer")
            return f"{parts[1]} is on hold; their drafts are held and nothing goes out."
        if verb == "release":
            store.clear_hold(parts[1])
            return f"{parts[1]} released; held drafts are pending again and need a fresh approval."
        return COMMAND_HELP
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
                 description="Cash Desk: status, queue, approve, reject, hold, release (human-only actions)",
                 args_hint="status | queue | approve <draft-id> [hash-prefix] | reject <draft-id> | hold <customer-id> | release <customer-id>")
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
