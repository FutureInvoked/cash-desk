"""Cash Desk Desktop backend, mounted by Hermes at /api/plugins/cash-desk/ for every
profile that lists the plugin under ``plugins.enabled``.

Reads the ledger for the Desktop page and status chip, and performs the human actions
the page offers: approve a draft (bound to the hash of its exact text), reject it, and
hold or release a customer. The model has no tool for approving or releasing; only a
person clicking here or typing the slash command can. Nothing here sends anything.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("cash-desk.api")
router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
SYMBOLS = {"EUR": "€", "CZK": "Kč", "GBP": "£", "USD": "$"}
NOT_FOUND = ("NotFoundError",)
REFUSED = ("HashMismatchError", "ConflictError", "NotApprovedError", "AlreadySentError", "DuplicateImportError", "StoreError")


def _load_sibling(name: str) -> Any:
    """Import a module from the plugin root by file. paths.py has no postponed-annotation
    dataclasses, so it needs no sys.modules entry."""
    spec = importlib.util.spec_from_file_location(f"cash_desk_api_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine() -> Any | None:
    """The engine package: the copy the plugin loader already imported if there is one,
    else a normal import of ``cashdesk`` with the plugin root on sys.path."""
    target = (ROOT / "cashdesk" / "__init__.py").resolve()
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if file and Path(file).resolve() == target:
            return module
    root = str(ROOT)
    if root not in sys.path:
        sys.path.append(root)
    try:
        return importlib.import_module("cashdesk")
    except Exception as exc:
        logger.warning("cash-desk: engine import failed: %s", exc)
        return None


def _store() -> Any | None:
    engine = _engine()
    store_cls = getattr(engine, "Store", None) if engine else None
    if store_cls is None:
        return None
    paths = _load_sibling("paths")
    path = paths.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = store_cls(path, today=paths.today, ignore_balances_below=_setting("ignore_balances_below") or "10.00")
    own = "".join(_setting("payment_iban").split()).upper()
    if (not own or engine.iban_valid(own)) and hasattr(store, "sync_payment_iban"):
        try:
            store.sync_payment_iban(own)  # the same account the agent's tools use, before any approval here
        except Exception:
            pass
    return store


def _store_or_503() -> Any:
    store = _store()
    if store is None:
        raise HTTPException(status_code=503, detail="Cash Desk engine is not available in this profile")
    return store


def _close(store: Any) -> None:
    close = getattr(store, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # pragma: no cover - defensive
            pass


def _refused(exc: Exception) -> HTTPException:
    name = exc.__class__.__name__
    if name in NOT_FOUND:
        return HTTPException(status_code=404, detail=str(exc))
    if name in REFUSED:
        return HTTPException(status_code=409, detail=str(exc))
    logger.exception("cash-desk api: unexpected error")
    return HTTPException(status_code=500, detail=f"{name}: {exc}")


def _money(engine: Any, value: Any, currency: str) -> str:
    try:
        return engine.format_money(Decimal(str(value)), currency, "en")
    except Exception:
        return f"{currency} {value}"


def _compact(amount: Any, currency: str) -> str:
    value = Decimal(str(amount))
    symbol = SYMBOLS.get(currency, currency + " ")
    if value >= 1000:
        return f"{symbol}{value / 1000:.1f}k"
    return f"{symbol}{value:.0f}"


def _chip(snapshot: dict[str, Any]) -> str:
    """'€18.4k overdue · 4 to approve' from the snapshot; the backend owns every figure."""
    parts: list[str] = []
    overdue_by_currency = []
    for cur, buckets in (snapshot.get("open_by_bucket") or {}).items():
        overdue = sum(Decimal(str(v)) for b, v in buckets.items() if b != "current")
        overdue_by_currency.append((overdue, cur))
    overdue_by_currency.sort(reverse=True)
    if overdue_by_currency and overdue_by_currency[0][0] > 0:
        parts.append(f"{_compact(overdue_by_currency[0][0], overdue_by_currency[0][1])} overdue")
    pending = int(snapshot.get("awaiting_approval") or 0)
    if pending:
        parts.append(f"{pending} to approve")
    held = int(snapshot.get("on_hold") or 0)
    if held and not pending:
        parts.append(f"{held} on hold")
    placements = int(snapshot.get("receipt_placement_questions") or 0)
    if placements:
        return f"Review {placements} receipt allocation question(s); balances need checking"
    questions = int(snapshot.get("receipt_overlap_questions") or 0)
    if questions:
        return f"Review {questions} receipt identity question(s); balances need checking"
    return " · ".join(parts) if parts else "Cash Desk"


def _label(kind: str) -> str:
    """"Reminder 21", "Thank-you", "Escalation": the short name people see for a draft's kind."""
    engine = _engine()
    label = getattr(engine, "stage_label", None) if engine else None
    return label(kind, "en") if label else kind


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


def _draft_dict(store: Any, draft: Any) -> dict[str, Any]:
    try:
        customer = store.get_customer(draft.customer_id).name
    except Exception:
        customer = draft.customer_id
    return {
        "id": draft.id, "kind": draft.kind, "label": _label(draft.kind), "customer_id": draft.customer_id, "customer": customer,
        "lang": draft.language, "subject": draft.subject, "body": draft.body,
        "recipients": list(draft.recipients), "invoice_numbers": list(draft.invoice_numbers),
        "created_at": draft.created_at.isoformat(), "status": draft.status, "hash": draft.hash,
        "figures": _figure_badges(store, draft),
    }


EMPTY_STATUS: dict[str, Any] = {
    "ledger": "empty", "engine": False, "open_by_bucket": {}, "open_by_bucket_display": {}, "open_total_display": {},
    "awaiting_approval": 0, "approved_unsent": 0, "held_drafts": 0, "on_hold": 0, "last_import": None, "chip": "Cash Desk",
}


@router.get("/status")
def status() -> dict[str, Any]:
    store = _store()
    if store is None:
        return dict(EMPTY_STATUS)
    engine = _engine()
    try:
        snapshot = store.status_snapshot(_load_sibling("paths").today()).to_dict()
        snapshot["receipt_overlap_questions"] = len(store.payment_overlaps())
        snapshot["receipt_placement_questions"] = len(store.receipt_placement_reviews())
        snapshot["customer_identity_questions"] = len(store.customer_identity_reviews())
        snapshot["surplus_questions"] = len(store.surplus_reviews())
        snapshot["invoice_import_questions"] = len(store.invoice_import_questions())
        snapshot["bank_evidence_questions"] = len(store.bank_evidence_questions())
        snapshot["balances_need_review"] = bool(snapshot["bank_evidence_questions"] or snapshot["customer_identity_questions"] or snapshot["surplus_questions"] or snapshot["invoice_import_questions"] or snapshot["receipt_overlap_questions"] or snapshot["receipt_placement_questions"])
        imports = store.list_imports()
    finally:
        _close(store)
    snapshot["open_by_bucket_display"] = {
        cur: {bucket: _money(engine, value, cur) for bucket, value in buckets.items()}
        for cur, buckets in (snapshot.get("open_by_bucket") or {}).items()
    }
    snapshot["open_total_display"] = {
        cur: _money(engine, sum(Decimal(str(v)) for v in buckets.values()), cur)
        for cur, buckets in (snapshot.get("open_by_bucket") or {}).items()
    }
    snapshot.update({"engine": True, "engine_version": str(getattr(engine, "ENGINE_VERSION", "")),
                     "ledger": "ok" if imports else "empty", "chip": _chip(snapshot)})
    return snapshot


@router.get("/drafts")
def drafts(status: str = "pending") -> dict[str, Any]:
    store = _store()
    if store is None:
        return {"drafts": [], "engine": False}
    try:
        rows = store.list_drafts(status=None if status == "all" else status)
        return {"drafts": [_draft_dict(store, d) for d in rows], "engine": True}
    finally:
        _close(store)


@router.get("/holds")
def holds() -> dict[str, Any]:
    store = _store()
    if store is None:
        return {"customers": [], "engine": False}
    try:
        # The same two sources the status count uses: an explicit hold, or an open disputed invoice.
        disputed: dict[str, list[str]] = {}
        for inv in store.list_invoices():
            if str(getattr(inv, "status", "")) == "disputed" and Decimal(str(getattr(inv, "balance", 0) or 0)) > 0:
                disputed.setdefault(inv.customer_id, []).append(inv.number)
        customers = []
        for c in store.list_customers():
            if not (c.on_hold or c.id in disputed):
                continue
            reasons = [c.hold_reason] if c.on_hold and c.hold_reason else []
            if c.id in disputed:
                reasons.append("dispute on " + ", ".join(sorted(disputed[c.id])) + " (resolve it in your accounting system)")
            customers.append({"id": c.id, "name": c.name, "hold_reason": "; ".join(reasons) or "on hold",
                              "dispute": c.id in disputed, "releasable": bool(c.on_hold)})
        return {"customers": customers, "engine": True}
    finally:
        _close(store)


_IMPORT_NOUNS = {"customers": ("customer", "customers"), "invoices": ("invoice", "invoices"), "bank": ("bank line", "bank lines")}
_HIDDEN_EVENTS = {"send_reserved"}  # bookkeeping between "approved" and "sent"; nothing for a person to read


def _event_text(store: Any, engine: Any, e: dict[str, Any]) -> str:
    """One plain sentence per ledger event: files, customers, labels and figures, never
    fingerprints or draft ids."""
    kind, ref, d = e.get("kind", ""), str(e.get("ref") or ""), e.get("detail") or {}
    if isinstance(d, str):
        import json
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    if kind in _HIDDEN_EVENTS:
        return ""

    def customer_name(cid: str) -> str:
        try:
            return store.get_customer(cid).name
        except Exception:
            return cid

    def about(draft_id: str) -> str:
        try:
            draft = store.get_draft(draft_id)
        except Exception:
            return "a draft"
        return f"{_label(draft.kind)} for {customer_name(draft.customer_id)}"

    if kind == "import":
        one, many = _IMPORT_NOUNS.get(d.get("kind", ""), ("row", "rows"))
        n = int(d.get("rows") or 0)
        what = d.get("file") or f"a {d.get('kind', '')} file"
        skipped = f", {d['errors']} rows skipped" if d.get("errors") else ""
        return f"Imported {what} · {n} {one if n == 1 else many}{skipped}"
    if kind == "match":
        amount = _money(engine, d.get("amount", "0"), d.get("currency", "")) if engine else f"{d.get('currency', '')} {d.get('amount', '')}"
        who = " (applied by a person)" if d.get("method") == "person" else ""
        return f"Matched payment {d.get('payment', '?')} to {ref} · {amount}{who}"
    if kind == "invoice_surplus_observed":
        return f"Customer surplus needs verification for {ref}; read /cashdesk surpluses before collection"
    if kind == "invoice_surplus_accounted":
        return f"Customer surplus accounting verified for {ref}; no money moved, fresh approval required"
    if kind == "dismiss":
        return f"Dismissed payment {ref}: it pays none of the invoices" + (f" ({d['reason']})" if d.get("reason") else "")
    if kind == "receipt_overlap":
        return f"Receipt identity question: {ref} and {d.get('other', '')}; read /cashdesk overlaps before applying or chasing"
    if kind == "receipt_placement_confirmed":
        return f"Receipt allocation/disposition verified for {ref}; no money moved; fresh approval required"
    if kind == "receipt_identity_resolved":
        return f"Receipt identity verified as {d.get('decision', '')}: {ref} and {d.get('other', '')}; old approvals stay invalid"
    if kind == "receipt_identity_conflict":
        return f"Receipt identity still paused: {d.get('reason', '')}"
    if kind == "settlement_review":
        return f"Paused {ref}: {d.get('reason', '')}"
    if kind == "settlement_reconciled":
        return f"Reconciled {ref}: external settlement {d.get('external_paid', '')}, local payments {d.get('local_applied', '')}, open {d.get('balance', '')}; redraft and approve"
    if kind == "import_kept_payment":
        kept = _money(engine, d.get("kept_balance", "0"), d.get("currency", "")) if engine else d.get("kept_balance", "")
        return f"Kept the payment already applied to {ref}: the new export still showed more open, so its open balance stays {kept}"
    if kind == "draft":
        held = " (customer on hold)" if d.get("status") == "held" else ""
        return f"Drafted {_label(d.get('kind', ''))} for {customer_name(d.get('customer', ''))}{held}"
    if kind == "revise":
        return f"Reworded {about(ref)} (Figure Lock passed; needs a fresh approval)"
    if kind == "approve":
        by = {"desktop": " on the Desktop", "slash-command": " with /cashdesk"}.get(str(d.get("by", "")), "")
        return f"Approved {about(ref)}{by}"
    if kind == "invalidate":
        return (f"Paused {about(ref)}: {d.get('reason', 'the ledger changed')}. "
                "Draft again, read the replacement and approve it before sending")
    if kind == "reject":
        return f"Rejected {about(ref)}" + (f": {d['reason']}" if d.get("reason") else "")
    if kind == "send":
        how = "as an email file (.eml)" if d.get("mode") == "eml" else "by SMTP"
        return f"Sent {about(ref)} {how}"
    if kind == "send_failed":
        return f"Sending {about(ref)} failed: {d.get('reason', 'unknown error')}"
    if kind == "hold":
        return f"Put {customer_name(ref)} on hold" + (f": {d['reason']}" if d.get("reason") else "")
    if kind == "unhold":
        return f"Released {customer_name(ref)}"
    return kind.replace("_", " ").capitalize()


@router.get("/events")
def events(limit: int = 30) -> dict[str, Any]:
    store = _store()
    if store is None:
        return {"events": [], "engine": False}
    try:
        rows = [asdict(e) if is_dataclass(e) else dict(e) for e in store.events(limit=max(1, min(int(limit), 200)))]
        shown = []
        for e in rows:
            text = _event_text(store, _engine(), e)
            if text:
                shown.append({**e, "text": text})
        return {"events": shown, "engine": True}
    finally:
        _close(store)


@router.get("/review")
def review() -> dict[str, Any]:
    """Payments a person has to place: a read-only matching pass that applies nothing. Judged as
    of today or the newest bank line, whichever is later, so a statement is never 'in the future'."""
    store = _store()
    if store is None:
        return {"payments": [], "engine": False}
    engine = _engine()
    try:
        unapplied = store.unapplied_payments()
        customers = {c.id: c for c in store.list_customers()}
        as_of = max([_load_sibling("paths").today(), *(p.date for p in unapplied)])
        result = store.match_result(as_of)
        items = []
        for item in (*result.review, *result.unmatched, *result.credit_notes):
            p = item.payment
            items.append({
                "id": p.id, "date": p.date.isoformat(), "amount_display": _money(engine, p.amount, p.currency),
                "counterparty": p.counterparty, "reference": p.reference, "kind": item.kind, "why": item.detail, "unknown_references": list(item.unknown_references), "named": list(item.named),
                "incoming": p.amount > 0 and item.kind not in ("POSSIBLE_DUPLICATE", "RECEIPT_UPDATE", "INVOICE_IMPORT", "RETURNED_PAYMENT", "CUSTOMER_IDENTITY", "BANK_EVIDENCE"),
                "held_invoices": list(item.holds),
                "candidates": [{"invoice_number": c.invoice_number, "score": round(c.score, 2),
                                "customer": customers[c.customer_id].name if c.customer_id in customers else c.customer_id,
                                "balance_display": _money(engine, c.balance, c.currency)} for c in item.candidates[:3]],
            })
        return {"payments": items, "as_of": as_of.isoformat(), "engine": True, "bank_evidence_questions": store.bank_evidence_questions(), "customer_identity_questions": store.customer_identity_reviews(), "return_questions": store.return_reviews(), "receipt_overlaps": store.payment_overlaps(), "receipt_placements": store.receipt_placement_reviews(), "receipt_updates": store.receipt_conflicts(),
                "cash_totals": {k: {c: str(v) for c, v in totals.items()} for k, totals in result.totals().items()}}
    finally:
        _close(store)


def _setting(key: str) -> str:
    """One of Cash Desk's settings (plugins.entries.cash-desk.settings), read through Hermes's
    read-only config loader; empty when it cannot be read."""
    try:
        from hermes_cli.config import load_config_readonly
        settings = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get("cash-desk") or {}
        value = (settings.get("settings") or {}).get(key)
        return "" if value is None else str(value)
    except Exception:
        return ""


DEMO_FILES = (("customers", "customers.csv"), ("invoices", "invoices.csv"),
              ("payments", "bank_day1.csv"), ("payments", "bank_day2.csv"))


@router.post("/demo")
def load_demo() -> dict[str, Any]:
    """The empty state's "Load demo data" button: import the plugin's own made-up demo CSVs
    (demo/, nothing else is read), moved to today, and match payments as of today. Only an
    empty ledger accepts it, so it can never mix demo rows into real books. No drafts are made
    and nothing is sent: drafting stays with the agent and every send still needs two people."""
    engine = _engine()
    store = _store_or_503()
    try:
        if store.list_imports() or store.list_customers() or store.list_invoices():
            raise HTTPException(status_code=409, detail="the ledger is not empty; the demo loads only into an empty ledger")
        demo = ROOT / "demo"
        paths = _load_sibling("paths")
        currency = paths.chosen_currency(_setting("currency"))
        imported = {"customers": 0, "invoices": 0, "payments": 0}
        for key, name in DEMO_FILES:
            path = demo / name
            if key == "customers":
                result = engine.read_customers(path)
            elif key == "invoices":
                result = engine.read_invoices("generic", path)
            else:
                result = engine.read_bank_csv(path)
            if result.errors:
                raise HTTPException(status_code=500, detail=f"demo file {name} has {len(result.errors)} row error(s)")
            result = paths.on_today(paths.in_currency(result, currency))
            imported[key] += store.ingest(result, file_name=name).get(key, 0)
        as_of = paths.today()
        matched = store.match_result(as_of)
        applied = [m for m in matched.applied if store.apply_match(m)]
        return {"ok": True, "as_of": as_of.isoformat(), "imported": imported,
                "applied": [{"payment_id": m.payment_id, "invoice_number": m.invoice_number} for m in applied],
                "review": len(matched.review)}
    except HTTPException:
        raise
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/drafts/{draft_id}/approve")
def approve(draft_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approve a draft. The caller must send the hash it saw, so an approval can never
    attach to text that changed after the person read it."""
    draft_hash = str((body or {}).get("hash") or "").strip()
    if not draft_hash:
        raise HTTPException(status_code=400, detail="hash is required")
    store = _store_or_503()
    try:
        store.approve_draft(draft_id, draft_hash, approved_by="desktop")
        return {"ok": True, "draft": _draft_dict(store, store.get_draft(draft_id))}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/payments/{payment_id}/apply")
def apply_payment(payment_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """A person applies a payment waiting for review to one invoice: never more than is left of
    the payment or open on the invoice. Reminders stating the old amount become stale."""
    number = str((body or {}).get("invoice_number") or "").strip()
    if not number:
        raise HTTPException(status_code=400, detail="invoice_number is required")
    engine = _engine()
    raw = (body or {}).get("amount")
    amount = None
    if raw not in (None, ""):
        parsed = engine.parse_amount(str(raw), "en") if engine else None
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"{raw!r} is not an amount")
        amount = parsed.value
    store = _store_or_503()
    try:
        match = store.apply_by_person(payment_id, number, amount, by="desktop")
        inv = store.get_invoice(number)
        return {"ok": True, "applied": _money(engine, match.amount, match.currency), "invoice_number": number,
                "left_display": _money(engine, inv.balance, inv.currency), "status": inv.status}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/payments/{payment_id}/dismiss")
def dismiss_payment(payment_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """A person says this receipt pays none of the invoices (other income, a transfer)."""
    store = _store_or_503()
    try:
        store.dismiss_payment(payment_id, str((body or {}).get("reason") or "dismissed from the Desktop page"))
        return {"ok": True, "payment_id": payment_id}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/drafts/{draft_id}/reject")
def reject(draft_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    store = _store_or_503()
    reason = str((body or {}).get("reason") or "rejected from the Desktop page")
    try:
        store.reject_draft(draft_id, reason)
        return {"ok": True, "draft": _draft_dict(store, store.get_draft(draft_id))}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/drafts/{draft_id}/hold")
def hold_draft(draft_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Holding a draft holds its customer: every pending draft of theirs is held too."""
    store = _store_or_503()
    reason = str((body or {}).get("reason") or "held from the Desktop page")
    try:
        draft = store.get_draft(draft_id)
        store.set_hold(draft.customer_id, reason)
        return {"ok": True, "customer_id": draft.customer_id, "draft": _draft_dict(store, store.get_draft(draft_id))}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/customers/{customer_id}/hold")
def hold_customer(customer_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    store = _store_or_503()
    reason = str((body or {}).get("reason") or "held from the Desktop page")
    try:
        store.set_hold(customer_id, reason)
        return {"ok": True, "customer_id": customer_id}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)


@router.post("/customers/{customer_id}/release")
def release_customer(customer_id: str) -> dict[str, Any]:
    store = _store_or_503()
    try:
        store.clear_hold(customer_id)
        return {"ok": True, "customer_id": customer_id}
    except Exception as exc:
        raise _refused(exc) from exc
    finally:
        _close(store)
