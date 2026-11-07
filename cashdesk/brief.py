"""The morning cash and receivables brief: a short, read-only page per run.

Payments applied since the previous brief, invoices that became overdue in
the window, drafts waiting for approval (with STALE and DO NOT APPROVE tags),
customers on hold, and the open balance per currency. Amounts are never
added across currencies. The brief reads a :class:`Store` (or a
``BriefSnapshot`` taken from one) and writes nothing; the caller records
that a brief was produced with ``Store.record_brief``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from .dates import format_date
from .model import Draft
from .money import cs_days, format_money, format_totals, sum_by_currency
from .render import stage_label
from .store import BriefSnapshot

T = {
    "en": dict(
        title="Morning cash & AR brief",
        window="Covers {a} to {b}. Nothing has been sent.",
        window_since="Covers {a} to {b} (since the previous brief). Nothing has been sent.",
        hold="HOLD: disputes and customers on hold - no chasing",
        pay="Payments applied (not yet in a brief)", pay_since="Payments applied since the previous brief", od="Newly overdue", appr="Awaiting approval", open="Open per currency",
        none="- none", held="HOLD", donot="DO NOT APPROVE (paused; check the hold and redraft)",
        stale="STALE: invoice now paid - do not send", stale_part="STALE IN PART: {} now paid - redraft before approving",
        totals="Total per currency", disputed="{no} disputed",
        review="Payments to review ({n}) - Apply or Dismiss on the Cash Desk page", review_line="{d} {amt} from {who} ({id})",
        paused="Reminders paused (no draft until the reason clears)", paused_line="{who}: {nos} ({why})",
    ),
    "cs": dict(
        title="Ranní přehled hotovosti a pohledávek",
        window="Období {a} až {b}, nic nebylo odesláno.",
        window_since="Období {a} až {b} (od předchozího přehledu), nic nebylo odesláno.",
        hold="POZASTAVENO: spory a pozastavení zákazníci - neupomínat",
        pay="Spárované platby (dosud v žádném přehledu)", pay_since="Platby spárované od předchozího přehledu", od="Nově po splatnosti", appr="Čeká na schválení", open="Otevřeno podle měny",
        none="- nic", held="POZASTAVENO", donot="NESCHVALOVAT (pozastaveno; ověřte důvod a vytvořte nový návrh)",
        stale="NEAKTUÁLNÍ: faktura už je zaplacena - neodesílat", stale_part="ČÁSTEČNĚ NEAKTUÁLNÍ: {} už zaplaceno - před schválením upravit",
        totals="Součet podle měny", disputed="{no} ve sporu",
        review="Platby ke kontrole ({n}) - Použít nebo Zamítnout na stránce Cash Desk", review_line="{d} {amt} od {who} ({id})",
        paused="Upomínky pozastaveny (žádný návrh, dokud důvod trvá)", paused_line="{who}: {nos} ({why})",
    ),
}


def brief_window(day: dt.date, since: dt.datetime | None = None) -> tuple[dt.date, dt.date]:
    """(first, last) day the brief covers: from the previous brief's day, or the
    previous business day when there was none (a Monday covers Fri-Sun), up to yesterday."""
    last = day - dt.timedelta(days=1)
    if since is not None:
        return min(since.date(), last), last
    first = last
    while first.weekday() >= 5:
        first -= dt.timedelta(days=1)
    return first, last


def _count(n: int, lang: str) -> str:
    if lang == "cs":
        return f"{n} faktura" if n == 1 else f"{n} faktury" if 2 <= n <= 4 else f"{n} faktur"
    return f"{n} invoice{'s' if n != 1 else ''}"


def _days(n: int, lang: str) -> str:
    return f"{cs_days(n)} po splatnosti" if lang == "cs" else f"{n} day{'s' if n != 1 else ''} overdue"


def morning_brief(store_or_snapshot, as_of: dt.date, lang: str = "en") -> str:
    """Render the brief as Markdown. Reads only."""
    snapshot: BriefSnapshot = store_or_snapshot.brief_snapshot(as_of) if hasattr(store_or_snapshot, "brief_snapshot") else store_or_snapshot
    t = T.get(lang, T["en"])
    a, b = brief_window(as_of, snapshot.previous_brief_at)
    customers, invoices = snapshot.customers, snapshot.invoices
    money = lambda value, currency: format_money(value, currency, lang)  # noqa: E731
    day = lambda d: format_date(d, lang)  # noqa: E731
    name = lambda cid: customers[cid].name if cid in customers else cid  # noqa: E731

    held: dict[str, str] = {c.id: (c.hold_reason or t["held"]) for c in customers.values() if c.on_hold}
    for inv in invoices.values():
        if inv.is_disputed and inv.balance > 0:
            held.setdefault(inv.customer_id, t["disputed"].format(no=inv.number))

    lines = [f"# {t['title']} - {day(as_of)}", (t["window_since"] if snapshot.previous_brief_at else t["window"]).format(a=day(a), b=day(b)), ""]
    if held:
        lines += [f"## {t['hold']}"] + [f"- {name(cid)} ({reason})" for cid, reason in sorted(held.items(), key=lambda kv: name(kv[0]))] + [""]

    review = tuple(getattr(snapshot, "review", ()) or ())
    if review:
        lines.append(f"## {t['review'].format(n=len(review))}")
        for p in sorted(review, key=lambda p: (p.date or as_of, p.id)):
            lines.append("- " + t["review_line"].format(d=day(p.date) if p.date else "?", amt=money(p.amount, p.currency), who=p.counterparty or "?", id=p.id))
        lines.append("")
    paused: dict[str, list[str]] = {}
    why: dict[str, str] = {}
    for no, reason in sorted((getattr(snapshot, "paused", None) or {}).items()):
        inv = invoices.get(no)
        if inv is None or inv.customer_id in held or not inv.is_open:
            continue
        paused.setdefault(inv.customer_id, []).append(no)
        why.setdefault(inv.customer_id, reason.split(":", 1)[0].strip().replace("_", " "))
    if paused:
        lines.append(f"## {t['paused']}")
        for cid in sorted(paused, key=name):
            lines.append("- " + t["paused_line"].format(who=name(cid), nos=", ".join(paused[cid]), why=why[cid]))
        lines.append("")

    lines.append(f"## {t['pay_since'] if snapshot.previous_brief_at else t['pay']}")
    if snapshot.payments:
        for view in snapshot.payments:
            when = view.payment.date if view.payment else view.applied_at.date()
            who = view.customer.name if view.customer else (view.invoice.customer_id if view.invoice else "?")
            lines.append(f"- {day(when)} {view.match.invoice_number} {who}: {money(view.match.amount, view.match.currency)} ({view.match.payment_id})")
        lines.append(f"- {t['totals']}: {format_totals(sum_by_currency((v.match.currency, v.match.amount) for v in snapshot.payments), lang)}")
    else:
        lines.append(t["none"])

    lines += ["", f"## {t['od']}"]
    newly = [inv for inv in sorted(invoices.values(), key=lambda i: (i.due_date, i.number)) if inv.is_open and a <= inv.due_date + dt.timedelta(days=1) <= b]
    for inv in newly:
        tag = f" - {t['held']}" if inv.customer_id in held else ""
        lines.append(f"- {inv.number} {name(inv.customer_id)}: {money(inv.balance, inv.currency)}, {_days(inv.days_overdue(as_of), lang)}{tag}")
    if not newly:
        lines.append(t["none"])

    queue: list[Draft] = sorted((*snapshot.pending_drafts, *snapshot.held_drafts), key=lambda d: (d.created_at, d.id))
    paid = {no for no, inv in invoices.items() if inv.is_paid}
    lines += ["", f"## {t['appr']} ({len(queue)})"]
    for d in queue:
        gone = [n for n in d.invoice_numbers if n in paid]
        if d.kind == "thanks":
            tag = ""
        elif gone and len(gone) == len(d.invoice_numbers):
            tag = f" - **{t['stale']}**"
        elif gone:
            tag = f" - **{t['stale_part'].format(', '.join(gone))}**"
        elif d.status == "held" or d.customer_id in held:
            tag = f" - **{t['donot']}**"
        else:
            tag = ""
        lines.append(f"- {stage_label(d.kind, lang)} · {name(d.customer_id)}: {', '.join(d.invoice_numbers)}{tag}")
    if not queue:
        lines.append(t["none"])

    lines += ["", f"## {t['open']}"]
    open_invoices = [inv for inv in invoices.values() if inv.is_open]
    totals = sum_by_currency((inv.currency, inv.balance) for inv in open_invoices)
    for currency, total in sorted(totals.items()):
        lines.append(f"- {money(total, currency)} ({_count(sum(1 for i in open_invoices if i.currency == currency), lang)})")
    if not totals:
        lines.append(t["none"])
    return "\n".join(lines) + "\n"


__all__ = ["T", "brief_window", "morning_brief"]
