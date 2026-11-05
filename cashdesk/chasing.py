"""Graduated follow-up: who gets which draft today, and who gets nothing.

Stages are 7, 14, 21 and 28 days overdue. The customer's oldest overdue
invoice sets the stage and one draft lists all their overdue invoices. The
chase state remembers the highest stage drafted per invoice, so a run only
writes a draft when a new stage is reached; a stage passed between two runs
(weekly runs: day 27, then day 34) is not skipped, the highest stage reached
is drafted. An invoice past 30 days that never had its stage-28 reminder (a
backlog on the first run, or a catch-up) gets that customer reminder first;
the internal escalation note follows once the stage-28 reminder exists and,
if it was drafted past 30 days, a week has passed. Escalation is per invoice:
an escalated invoice is left to the reviewer and drops out of reminders,
while the customer's newer invoices are still chased on their own schedule.

Wording is truthful about history: a draft refers to earlier reminders only
when ``reminders_sent`` says one was really sent for one of its invoices.

Holds: a dispute (a disputed invoice, a customer on hold, or a reply that
reads as a dispute) holds the whole customer; a payment in review holds just
the invoice it may cover; a refund request is a credit-note question that
holds the invoice and suppresses its thank-you; an open credit note (a
negative balance) holds that customer's reminders, so no reminder states a
gross total that is not owed; void invoices are never chased; a customer
without an e-mail gets no draft and is listed (no contact name is fine: the
draft opens "Hello,"). Thank-you drafts follow applied payments, once per
invoice.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .matching import invoices_named, is_refund
from .model import Customer, Draft, Invoice, Match
from .render import DraftLine, RenderContext, render_draft

STAGES = ((7, "stage07"), (14, "stage14"), (21, "stage21"), (28, "stage28"))
ESCALATE_AFTER = 30
# A stage-28 reminder drafted when the invoice was already past 30 days gets
# this long before the escalation note follows it.
ESCALATION_WAIT = 7
RANK = {"stage07": 1, "stage14": 2, "stage21": 3, "stage28": 4, "escalation": 5}
HOLD_REASONS = ("dispute", "on_hold", "credit_note", "payment_in_review", "no_email", "no_contact_name", "no_customer",
                "promise", "not_in_export", "small_balance")

# Reply classes, checked in this order: an auto-reply is never a promise, a
# dispute wins over anything else in the same mail, "paid" beats "promise".
OUT_OF_OFFICE_WORDS = ("out of office", "automatic reply", "auto-reply", "autoreply", "on leave until", "mimo kancelář", "automatická odpověď", "dovolen")
DISPUTE_WORDS = (
    "dispute", "do not agree", "don't agree", "disagree", "incorrect", "wrong", "overbill", "overcharg", "charged twice", "not correct",
    "not ordered", "never received", "reklamac", "reklamuj", "nesouhlas", "chybn", "nesprávn", "špatn", "rozpor", "neuznáv", "neobjednal", "nedodal",
)
PAID_WORDS = (
    "already paid", "have paid", "has been paid", "was paid", "we paid", "payment was made", "payment has been made", "went out", "transferred",
    "sent the payment", "paid on", "paid yesterday", "paid today", "uhrazen", "zaplacen", "uhradili", "zaplatili", "odeslali", "odesláno", "odeslána", "odešla",
)
PROMISE_WORDS = ("will pay", "we'll pay", "pay by", "payment scheduled", "will be paid", "transfer on", "uhradíme", "zaplatíme", "zaplatím", "uhradím", "odešleme platbu", "pošleme platbu")
REPLY_CLASSES = (("other", OUT_OF_OFFICE_WORDS), ("dispute", DISPUTE_WORDS), ("paid", PAID_WORDS), ("promise", PROMISE_WORDS))


def stage_for(days: int) -> str | None:
    """Highest stage reached at ``days`` overdue; None below day 7."""
    stage = None
    for threshold, name in STAGES:
        if days >= threshold:
            stage = name
    return stage


@dataclass
class ChaseState:
    """What has been drafted so far.

    ``drafted`` holds the highest reminder stage per invoice (``stage07`` ..
    ``stage28`` or ``escalation``); ``thanked`` holds ``invoice:payment`` keys
    for thank-yous already drafted. The two never mix, so a thank-you for a
    part payment cannot stop later reminders or the escalation.
    """

    drafted: dict[str, str] = field(default_factory=dict)
    thanked: set[str] = field(default_factory=set)

    def stage_of(self, number: str) -> str:
        """The stage without its date: ``stage28@2026-11-10`` reads ``stage28``."""
        return self.drafted.get(number, "").partition("@")[0]

    def rank(self, number: str) -> int:
        return RANK.get(self.stage_of(number), 0)

    def late_since(self, number: str) -> dt.date | None:
        """The day a stage-28 reminder was drafted past 30 days (``stage28@<date>``), else None."""
        _, _, since = self.drafted.get(number, "").partition("@")
        try:
            return dt.date.fromisoformat(since) if since else None
        except ValueError:
            return None

    def record(self, number: str, stage: str, on: dt.date | None = None) -> None:
        """Remember ``stage`` when it is higher than what is there; ``on`` dates a late stage-28 reminder."""
        if stage not in RANK:
            raise ValueError(f"unknown stage {stage!r}; thank-yous go through record_thanks")
        if RANK[stage] > self.rank(number):
            self.drafted[number] = f"{stage}@{on.isoformat()}" if on is not None else stage

    @staticmethod
    def thanks_key(number: str, payment_id: str) -> str:
        return f"{number}:{payment_id}"

    def record_thanks(self, number: str, payment_id: str) -> None:
        self.thanked.add(self.thanks_key(number, payment_id))

    def was_thanked(self, number: str, payment_id: str) -> bool:
        return self.thanks_key(number, payment_id) in self.thanked

    def copy(self) -> "ChaseState":
        return ChaseState(dict(self.drafted), set(self.thanked))

    def to_json(self) -> str:
        return json.dumps({"drafted": self.drafted, "thanked": sorted(self.thanked)}, indent=1, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "ChaseState":
        if not (text or "").strip():
            return cls()
        data = json.loads(text)
        drafted = {str(k): str(v) for k, v in dict(data.get("drafted", {})).items() if str(v).partition("@")[0] in RANK}
        return cls(drafted, {str(k) for k in data.get("thanked", [])})


@dataclass(frozen=True)
class Reply:
    customer_id: str
    date: dt.date
    text: str
    lang: str = "en"


def classify_reply(text: str, lang: str = "en") -> str:
    """'dispute', 'paid', 'promise' or 'other' (questions and auto-replies are 'other').

    Both languages' word lists are always checked; ``lang`` is accepted for
    symmetry with the rest of the engine and for future per-language lists.
    """
    low = (text or "").lower()
    for label, words in REPLY_CLASSES:
        if any(w in low for w in words):
            return label
    return "other"


@dataclass(frozen=True)
class HeldItem:
    customer_id: str
    invoice_numbers: tuple[str, ...]
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class FollowupPlan:
    as_of: dt.date
    drafts: tuple[Draft, ...] = ()
    thanks: tuple[Draft, ...] = ()
    escalations: tuple[Draft, ...] = ()
    held: tuple[HeldItem, ...] = ()
    not_due: tuple[str, ...] = ()
    voided: tuple[str, ...] = ()
    already_drafted: tuple[tuple[str, str], ...] = ()
    replies: tuple[tuple[Reply, str], ...] = ()
    state: ChaseState = field(default_factory=ChaseState)

    @property
    def all_drafts(self) -> tuple[Draft, ...]:
        return self.drafts + self.thanks + self.escalations


def _customers(customers: Mapping[str, Customer] | Iterable[Customer]) -> dict[str, Customer]:
    if isinstance(customers, Mapping):
        return dict(customers)
    return {c.id: c for c in customers}


def _contact_problem(customer: Customer) -> str | None:
    """Only a missing e-mail stops a draft; without a contact name it opens "Hello," / "Dobrý den,"."""
    if not customer.email:
        return "no_email"
    return None


def _is_open_credit(inv: Invoice) -> bool:
    """An open credit note: a negative balance the customer can still set off."""
    return inv.balance < 0 and inv.status in ("open", "disputed")


def _may_escalate(state: ChaseState, number: str, as_of: dt.date) -> bool:
    """Past 30 days an invoice is escalated once its stage-28 reminder exists (a week after it, when that was drafted late)."""
    if state.rank(number) < RANK["stage28"]:
        return False
    since = state.late_since(number)
    return since is None or (as_of - since).days >= ESCALATION_WAIT


def _line(inv: Invoice, days: int, amount=None) -> DraftLine:
    return DraftLine(inv.number, inv.due_date, inv.balance if amount is None else amount, inv.currency, max(days, 0), inv.reference, inv.iban)


def plan_followups(
    invoices: Iterable[Invoice],
    customers: Mapping[str, Customer] | Iterable[Customer],
    payments_applied: Iterable[Match],
    state: ChaseState,
    as_of: dt.date,
    *,
    sender: str,
    credit_notes: Mapping[str, str] | Iterable[str] = (),
    holds: Mapping[str, str] | None = None,
    replies: Iterable[Reply] = (),
    created_at: dt.datetime | None = None,
    reminders_sent: Mapping[str, int] | Iterable[str] = (),
) -> FollowupPlan:
    """Decide today's drafts. Pure: returns a plan with an updated copy of ``state``.

    ``payments_applied`` are the matches to thank for; ``credit_notes`` names
    invoices under a credit-note question (held, no thank-you); ``holds`` maps
    invoices to a payment-in-review reason; ``replies`` are classified and a
    dispute holds its customer. ``sender`` signs every draft.

    ``reminders_sent`` says which invoices already had a customer reminder
    really sent (invoice number -> how many, or just the numbers for one
    each). Only then may a draft refer to earlier reminders; the default
    (nothing sent) keeps every draft a first contact that claims no history.
    """
    if not (sender or "").strip():
        raise ValueError("sender is required: every draft is signed by the business that issued the invoices")
    cust_map = _customers(customers)
    inv_map: dict[str, Invoice] = {}
    for inv in invoices:
        inv_map.setdefault(inv.number, inv)
    applied = [m for m in payments_applied]
    applied_numbers = {m.invoice_number for m in applied}
    new_state = state.copy()
    credit: dict[str, str] = dict(credit_notes) if isinstance(credit_notes, Mapping) else {str(n): "credit-note question" for n in credit_notes}
    review_holds = dict(holds or {})
    stamp = created_at or dt.datetime.now(dt.timezone.utc)
    if isinstance(reminders_sent, Mapping):
        sent_counts = {str(k): int(v) for k, v in reminders_sent.items()}
    else:
        sent_counts = {str(n): 1 for n in reminders_sent}

    customer_hold: dict[str, tuple[str, str]] = {}
    for c in cust_map.values():
        if c.on_hold:
            customer_hold[c.id] = ("on_hold", c.hold_reason or "customer on hold")
    for inv in inv_map.values():
        if inv.is_disputed and inv.balance > 0:
            customer_hold.setdefault(inv.customer_id, ("dispute", f"{inv.number} is disputed"))
    for inv in sorted(inv_map.values(), key=lambda i: i.number):
        if _is_open_credit(inv):
            customer_hold.setdefault(inv.customer_id, ("credit_note", f"open credit note {inv.number}: a reminder would state a gross total that is not owed. "
                                                       "Set the credit note off against an invoice in the accounting system and re-import, then draft again"))
    classified: list[tuple[Reply, str]] = []
    for reply in replies:
        if reply.date > as_of:
            continue
        label = classify_reply(reply.text, reply.lang)
        classified.append((reply, label))
        own = [i for i in inv_map.values() if i.customer_id == reply.customer_id]
        if label == "dispute":
            customer_hold.setdefault(reply.customer_id, ("dispute", f"reply of {reply.date.isoformat()}: {reply.text[:80]}"))
        elif is_refund(reply.text):
            named = invoices_named(reply.text, own)
            targets = named or [i.number for i in own if not i.is_void and (i.is_open or i.number in applied_numbers)]
            for number in targets:
                credit.setdefault(number, f"refund requested in reply of {reply.date.isoformat()}: credit-note question, not a dispute")

    held: dict[tuple[str, str], tuple[list[str], str]] = {}

    def hold(cid: str, numbers: Iterable[str], reason: str, detail: str) -> None:
        entry = held.setdefault((cid, reason), ([], detail))
        for n in numbers:
            if n not in entry[0]:
                entry[0].append(n)

    voided: list[str] = []
    not_due: list[str] = []
    by_cust: dict[str, list[tuple[Invoice, int]]] = {}
    for inv in inv_map.values():
        cid = inv.customer_id
        if inv.is_void:
            voided.append(inv.number)
            continue
        if inv.number in credit:
            hold(cid, [inv.number], "credit_note", credit[inv.number])
            continue
        if _is_open_credit(inv) and cid in customer_hold:
            hold(cid, [inv.number], *customer_hold[cid])
            continue
        if not inv.is_open:
            continue
        days = inv.days_overdue(as_of)
        if cid in customer_hold:
            hold(cid, [inv.number], *customer_hold[cid])
            continue
        if inv.number in review_holds:
            why = review_holds[inv.number]
            # a hold is "reason: detail" when the plugin layer names its own reason (promise, not_in_export, ...)
            code, _, rest = str(why).partition(": ")
            if code in HOLD_REASONS and rest:
                hold(cid, [inv.number], code, rest)
            else:
                hold(cid, [inv.number], "payment_in_review", why)
            continue
        if days < 1:
            not_due.append(inv.number)
            continue
        by_cust.setdefault(cid, []).append((inv, days))

    drafts: list[Draft] = []
    escalations: list[Draft] = []
    already: list[tuple[str, str]] = []
    for cid, items in sorted(by_cust.items()):
        items.sort(key=lambda x: (-x[1], x[0].number))
        numbers = [i.number for i, _ in items]
        customer = cust_map.get(cid)
        if customer is None:
            hold(cid, numbers, "no_customer", "no customer record for this id")
            continue
        active = [(i, d) for i, d in items if new_state.rank(i.number) < RANK["escalation"]]
        parked = len(active) < len(items)
        escalate = [(i, d) for i, d in active if d > ESCALATE_AFTER and _may_escalate(new_state, i.number, as_of)]
        escalating = {i.number for i, _ in escalate}
        chase = [(i, d) for i, d in active if i.number not in escalating]
        if chase:
            oldest, oldest_days = chase[0]
            stage = stage_for(oldest_days)
            if stage is None:
                not_due.extend(i.number for i, _ in chase)
            elif new_state.rank(oldest.number) >= RANK[stage]:
                already.append((cid, stage))
            elif problem := _contact_problem(customer):
                hold(cid, [i.number for i, _ in chase], problem, "reminder not drafted: the customer record has no e-mail")
            else:
                history = max((sent_counts.get(i.number, 0) for i, _ in chase), default=0)
                context = RenderContext(customer, tuple(_line(i, d) for i, d in chase), sender, as_of, {}, stamp, reminders_sent=history)
                drafts.append(render_draft(stage, customer.language, context))
                for i, d in chase:
                    own_stage = stage_for(d)
                    if own_stage:
                        new_state.record(i.number, own_stage, as_of if d > ESCALATE_AFTER else None)
        if escalate:
            active_numbers = [i.number for i, _ in active]
            stages = {n: new_state.stage_of(n) for n in active_numbers if new_state.stage_of(n)}
            context = RenderContext(customer, tuple(_line(i, d) for i, d in active), sender, as_of, stages, stamp)
            escalations.append(render_draft("escalation", customer.language, context))
            for i, _ in escalate:
                new_state.record(i.number, "escalation")
        elif parked:
            already.append((cid, "escalation"))

    thanks: list[Draft] = []
    thank_groups: dict[str, list[tuple[Invoice, Match]]] = {}
    thanked_already: set[str] = set()
    for m in applied:
        inv = inv_map.get(m.invoice_number)
        if inv is None or inv.is_void or inv.number in credit:
            continue
        if new_state.was_thanked(inv.number, m.payment_id):
            thanked_already.add(inv.customer_id)
            continue
        thank_groups.setdefault(inv.customer_id, []).append((inv, m))
    for cid in sorted(thanked_already - set(thank_groups)):
        already.append((cid, "thanks"))
    for cid, pairs in sorted(thank_groups.items()):
        numbers = [i.number for i, _ in pairs]
        customer = cust_map.get(cid)
        if customer is None:
            hold(cid, numbers, "no_customer", "no customer record for this id")
            continue
        problem = _contact_problem(customer)
        if problem:
            hold(cid, numbers, problem, "thank-you not drafted: the customer record has no e-mail")
            continue
        context = RenderContext(customer, tuple(_line(i, 0, m.amount) for i, m in pairs), sender, as_of, {}, stamp)
        thanks.append(render_draft("thanks", customer.language, context))
        for i, m in pairs:
            new_state.record_thanks(i.number, m.payment_id)

    held_items = tuple(HeldItem(cid, tuple(numbers), reason, detail) for (cid, reason), (numbers, detail) in held.items())
    return FollowupPlan(as_of, tuple(drafts), tuple(thanks), tuple(escalations), held_items, tuple(not_due), tuple(voided), tuple(already), tuple(classified), new_state)


__all__ = [
    "STAGES",
    "ESCALATE_AFTER",
    "ESCALATION_WAIT",
    "RANK",
    "HOLD_REASONS",
    "stage_for",
    "ChaseState",
    "Reply",
    "classify_reply",
    "HeldItem",
    "FollowupPlan",
    "plan_followups",
]
