"""Payment matching: exact application only, everything else goes to a person.

A payment is applied automatically when its reference names exactly one ledger
invoice (invoice number with any separators, or the Czech variable symbol),
in that invoice's currency, for exactly its open balance, from a payer whose
name fits that invoice's customer (or a bank line with no payer name), dated
no earlier than the invoice. Every other receipt
becomes a review item with up to three fuzzy candidates scored by amount,
customer name similarity and date window, or is reported unmatched. Refunds
(outgoing lines, or anything with refund words) are credit-note questions and
never applied; a refund is not a dispute and holds nothing but its own invoice.

Totals are kept per currency, with money in and money out apart.
"""

from __future__ import annotations

import datetime as dt
import difflib
import itertools
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .model import Customer, Invoice, Match, Payment
from .money import format_money, sum_by_currency

REFUND_WORDS = re.compile(r"\b(refund(?:ed|s)?|vratk[ay]|vrácení|vraceni|dobropis(?:u|em)?|credit\s*note|chargeback|storno)\b", re.IGNORECASE)
LEGAL_SUFFIXES = re.compile(r"\b(spol s r o|s ?r ?o|a ?s|gmbh|ltd|limited|b ?v|oy|llc|llp|inc|co|plc|ag|kg|sa|sp z o o|z s)\b")
INVOICE_LIKE = re.compile(r"\b(?:inv(?:oice)?|fa(?:ktura)?|fi|vs)[ .#:/-]*\d+\b", re.IGNORECASE)
NAME_MIN = 0.6
AMOUNT_TOLERANCE = Decimal("0.05")  # a candidate within 5 % of the balance scores half
DATE_WINDOW = dt.timedelta(days=60)
MAX_CANDIDATES = 3
MAX_COMBINATION = 4
MIN_REFERENCE_DIGITS = 3

REVIEW_KINDS = ("MISSING", "AMBIGUOUS", "PARTIAL", "OVERPAID", "MULTI_INVOICE", "CURRENCY", "ALREADY_PAID", "VOID", "AFTER_AS_OF", "CREDIT_NOTE", "UNMATCHED",
                "PAYER", "EARLY")
EARLY_GRACE = dt.timedelta(days=3)  # a payment this much before the invoice date is still believable (clock or booking lag)


@dataclass(frozen=True)
class Candidate:
    invoice_number: str
    customer_id: str
    score: float
    amount_score: float
    name_score: float
    date_score: float
    balance: Decimal
    currency: str


@dataclass(frozen=True)
class ReviewItem:
    payment: Payment
    kind: str
    detail: str
    named: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    holds: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchResult:
    applied: tuple[Match, ...] = ()
    review: tuple[ReviewItem, ...] = ()
    credit_notes: tuple[ReviewItem, ...] = ()
    unmatched: tuple[ReviewItem, ...] = ()
    skipped: tuple[tuple[Payment, str], ...] = ()
    duplicates: tuple[Payment, ...] = ()
    held_invoices: dict[str, str] = field(default_factory=dict)

    def totals(self) -> dict[str, dict[str, Decimal]]:
        """Per currency: applied, review_in (receipts waiting for a person), out (refunds)."""
        return {
            "applied": sum_by_currency((m.currency, m.amount) for m in self.applied),
            "review_in": sum_by_currency((i.payment.currency, i.payment.amount) for i in (*self.review, *self.unmatched) if i.payment.amount > 0),
            "out": sum_by_currency((i.payment.currency, -i.payment.amount) for i in self.credit_notes if i.payment.amount < 0),
        }


# ------------------------------------------------------------------ helpers


def normalise_name(name: str) -> str:
    """Lower-case ASCII without punctuation or legal suffixes: ``Nová Ukázka s.r.o.`` -> ``nova ukazka``."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9 ]", " ", ascii_name)
    text = LEGAL_SUFFIXES.sub(" ", " ".join(text.split()))
    return " ".join(text.split())


def name_similarity(a: str, b: str) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def payer_fits(customer_name: str, counterparty: str) -> bool:
    """True when the bank's payer name could be this customer: no payer name at all, a close
    name, or a shared distinctive word (bank lines often truncate: ``BLUEBELL DESIGN ST``)."""
    if not (counterparty or "").strip():
        return True
    if name_similarity(customer_name, counterparty) >= NAME_MIN:
        return True
    words = lambda text: {w for w in normalise_name(text).split() if len(w) >= 4}
    return bool(words(customer_name) & words(counterparty))


def is_refund(text: str) -> bool:
    return bool(REFUND_WORDS.search(text or ""))


def _number_pattern(number: str) -> str:
    parts = [p for p in re.split(r"[\s\-/]+", number.strip()) if p]
    return r"[\s\-/]?".join(re.escape(p) for p in parts)


def invoices_named(text: str, invoices: Iterable[Invoice]) -> list[str]:
    """Ledger invoices named in ``text``: the number (separators optional, case
    ignored) or the variable symbol / reference as a whole token."""
    t = text or ""
    hits: list[str] = []
    if not t.strip():
        return hits
    for inv in invoices:
        if inv.number in hits:
            continue
        if re.search(rf"(?<![A-Za-z0-9]){_number_pattern(inv.number)}(?![0-9A-Za-z])", t, re.IGNORECASE):
            hits.append(inv.number)
            continue
        ref = inv.reference
        if ref and ref != inv.number:
            if ref.isdigit():
                if len(ref) >= MIN_REFERENCE_DIGITS and re.search(rf"(?<![0-9]){re.escape(ref)}(?![0-9])", t):
                    hits.append(inv.number)
            elif re.search(rf"(?<![A-Za-z0-9]){_number_pattern(ref)}(?![0-9A-Za-z])", t, re.IGNORECASE):
                hits.append(inv.number)
    return hits


def _customer_names(customers: Mapping[str, Customer] | Iterable[Customer] | None) -> dict[str, str]:
    if customers is None:
        return {}
    if isinstance(customers, Mapping):
        return {cid: c.name for cid, c in customers.items()}
    return {c.id: c.name for c in customers}


# ---------------------------------------------------------------- matching


class _Ledger:
    """Working view of the invoices: balances change as payments are applied."""

    def __init__(self, invoices: Iterable[Invoice], names: Mapping[str, str]):
        self.invoices: dict[str, Invoice] = {}
        for inv in invoices:
            self.invoices.setdefault(inv.number, inv)
        self.balance: dict[str, Decimal] = {no: inv.balance for no, inv in self.invoices.items()}
        self.paid: set[str] = {no for no, inv in self.invoices.items() if inv.is_paid}
        self.names = names

    def name(self, inv: Invoice) -> str:
        return self.names.get(inv.customer_id, inv.customer_id)

    def open_in(self, currency: str) -> list[Invoice]:
        return [inv for no, inv in self.invoices.items() if inv.currency == currency and not inv.is_void and no not in self.paid and self.balance[no] > 0]

    def is_open(self, no: str) -> bool:
        inv = self.invoices[no]
        return not inv.is_void and no not in self.paid and self.balance[no] > 0

    def candidates(self, payment: Payment) -> list[Candidate]:
        out: list[Candidate] = []
        amt = payment.amount
        for inv in self.open_in(payment.currency):
            bal = self.balance[inv.number]
            if bal == amt:
                a_s = 1.0
            elif abs(bal - amt) <= bal * AMOUNT_TOLERANCE:
                a_s = 0.5
            else:
                a_s = 0.0
            n_s = name_similarity(self.name(inv), payment.counterparty)
            d_s = 1.0 if inv.issue_date <= payment.date <= inv.due_date + DATE_WINDOW else 0.0
            if a_s == 0 and n_s < NAME_MIN:
                continue
            out.append(Candidate(inv.number, inv.customer_id, round(0.5 * a_s + 0.35 * n_s + 0.15 * d_s, 2), a_s, round(n_s, 2), d_s, bal, inv.currency))
        out.sort(key=lambda c: (-c.score, c.invoice_number))
        return out[:MAX_CANDIDATES]

    def combination(self, payment: Payment) -> tuple[list[Invoice], float] | None:
        """Open invoices of one customer whose balances add up to the payment, if exactly one such set exists."""
        by_customer: dict[str, list[Invoice]] = {}
        for inv in self.open_in(payment.currency):
            by_customer.setdefault(inv.customer_id, []).append(inv)
        found: list[tuple[list[Invoice], float]] = []
        for cid, invs in by_customer.items():
            if len(invs) < 2:
                continue
            n_s = name_similarity(self.names.get(cid, cid), payment.counterparty)
            if payment.counterparty and n_s < NAME_MIN:
                continue
            invs = sorted(invs, key=lambda i: i.number)[:12]
            for size in range(2, min(MAX_COMBINATION, len(invs)) + 1):
                for combo in itertools.combinations(invs, size):
                    if sum((self.balance[i.number] for i in combo), Decimal("0")) == payment.amount:
                        found.append((list(combo), n_s))
        return found[0] if len(found) == 1 else None


def match_payments(
    invoices: Iterable[Invoice],
    payments: Iterable[Payment],
    customers: Mapping[str, Customer] | Iterable[Customer] | None = None,
    as_of: dt.date | None = None,
) -> MatchResult:
    """Match payments to invoices; apply only reference + exact amount, same currency.

    Pass every invoice you have (paid and void included) so a payment naming a
    settled invoice is explained as ALREADY_PAID or VOID instead of UNMATCHED.
    ``customers`` supplies names for similarity scoring. With ``as_of``, a
    payment dated later is queued (AFTER_AS_OF) rather than applied.
    """
    ledger = _Ledger(invoices, _customer_names(customers))
    applied: list[Match] = []
    review: list[ReviewItem] = []
    credit_notes: list[ReviewItem] = []
    unmatched: list[ReviewItem] = []
    skipped: list[tuple[Payment, str]] = []
    duplicates: list[Payment] = []
    held: dict[str, str] = {}
    seen: set[str] = set()

    def m(value: Decimal, currency: str) -> str:
        return format_money(value, currency, "en")

    def queue(payment: Payment, kind: str, detail: str, named: Sequence[str] = (), cands: Sequence[Candidate] = (), hold_named: bool = True) -> ReviewItem:
        holds: list[str] = []
        if hold_named:
            for no in named:
                if no in ledger.invoices and ledger.is_open(no):
                    held.setdefault(no, f"held: payment {payment.id} in review ({kind})")
                    holds.append(no)
        for c in cands:
            if c.amount_score == 1.0 or kind == "MULTI_INVOICE":
                held.setdefault(c.invoice_number, f"held: may be paid by {payment.id} ({kind})")
                holds.append(c.invoice_number)
        item = ReviewItem(payment, kind, detail, tuple(named), tuple(cands), tuple(dict.fromkeys(holds)))
        (unmatched if kind == "UNMATCHED" else credit_notes if kind == "CREDIT_NOTE" else review).append(item)
        return item

    for p in payments:
        if p.id in seen:
            duplicates.append(p)
            continue
        seen.add(p.id)
        amt, cur, text = p.amount, p.currency, p.text
        named = invoices_named(text, ledger.invoices.values())
        if as_of is not None and p.date > as_of:
            queue(p, "AFTER_AS_OF", f"dated {p.date.isoformat()}, after the as-of date {as_of.isoformat()}; not applied in this run", named)
            continue
        refund = is_refund(text)
        if amt <= 0:
            if named or refund:
                item = queue(p, "CREDIT_NOTE", f"outgoing {m(-amt, cur)} looks like a refund; decide whether a credit note is needed. Not a dispute: the customer is not put on hold", named, hold_named=False)
                for no in named:
                    held[no] = f"held: refund {p.id} - credit-note question"
                continue
            skipped.append((p, "outgoing line without an invoice or refund words: not a receipt"))
            continue
        if refund:
            queue(p, "CREDIT_NOTE", f"incoming {m(amt, cur)} mentions a refund; not applied automatically. Decide whether a credit note is needed. Not a dispute: the customer is not put on hold", named, hold_named=False)
            for no in named:
                held[no] = f"held: refund {p.id} - credit-note question"
            continue
        if not named:
            cands = ledger.candidates(p)
            exact = [c for c in cands if c.amount_score == 1.0]
            close = [c for c in exact if c.name_score >= NAME_MIN]
            looks = INVOICE_LIKE.search(text)
            why = "no ledger invoice number or variable symbol in the reference" + (f" (looks like an invoice number not in the ledger: '{looks.group(0)}')" if looks else "")
            if len(exact) > 1 and len(close) != 1:
                queue(p, "AMBIGUOUS", f"{len(exact)} open invoices for exactly this amount; the payer name does not single one out", (), cands)
            elif exact:
                queue(p, "MISSING", why, (), cands)
            else:
                combo = ledger.combination(p)
                if combo:
                    invs, n_s = combo
                    parts = " + ".join(i.number for i in invs)
                    sums = " + ".join(m(ledger.balance[i.number], cur) for i in invs)
                    who = ledger.name(invs[0])
                    cands = tuple(Candidate(i.number, i.customer_id, round(0.5 + 0.35 * n_s, 2), 1.0, round(n_s, 2), 0.0, ledger.balance[i.number], cur) for i in invs)
                    queue(p, "MULTI_INVOICE", f"{m(amt, cur)} equals the open balances of {parts} ({sums}) for {who}; split by hand", (), cands)
                elif cands:
                    queue(p, "MISSING", why, (), cands)
                else:
                    queue(p, "UNMATCHED", why + "; no candidate by amount or name", (), ())
            continue
        if len(named) > 1:
            same = [ledger.invoices[n] for n in named if ledger.invoices[n].currency == cur]
            tot = sum((ledger.balance[i.number] for i in same), Decimal("0"))
            exact_note = " - the balances add up exactly" if tot == amt and same else ""
            queue(p, "MULTI_INVOICE", f"reference names {', '.join(named)}; their open balances total {m(tot, cur)} vs {m(amt, cur)} received - split by hand{exact_note}", named)
            continue
        no = named[0]
        inv = ledger.invoices[no]
        bal = ledger.balance[no]
        if inv.currency != cur:
            queue(p, "CURRENCY", f"{m(amt, cur)} received but {no} is in {inv.currency}", named)
        elif inv.is_void:
            queue(p, "VOID", f"{no} is void - refund, or a re-issued invoice?", named, hold_named=False)
        elif no in ledger.paid or bal <= 0:
            queue(p, "ALREADY_PAID", f"{no} shows nothing open - possible double payment, refund or credit?", named, hold_named=False)
        elif amt < bal:
            queue(p, "PARTIAL", f"{m(amt, cur)} received, {m(bal, cur)} open; {m(bal - amt, cur)} would remain", named)
        elif amt > bal:
            queue(p, "OVERPAID", f"{m(amt, cur)} received, only {m(bal, cur)} open; {m(amt - bal, cur)} extra", named)
        elif not payer_fits(ledger.name(inv), p.counterparty):
            queue(p, "PAYER", f"reference names {no} ({ledger.name(inv)}) but the payer is '{p.counterparty}'; check who paid before applying", named)
        elif p.date < inv.issue_date - EARLY_GRACE:
            queue(p, "EARLY", f"dated {p.date.isoformat()}, before {no} was issued ({inv.issue_date.isoformat()}); check the dates before applying", named)
        else:
            applied.append(Match(p.id, no, amt, cur, "reference+exact", "high", f"reference names {no} and {m(amt, cur)} equals its open balance"))
            ledger.balance[no] = Decimal("0.00")
            ledger.paid.add(no)
    return MatchResult(tuple(applied), tuple(review), tuple(credit_notes), tuple(unmatched), tuple(skipped), tuple(duplicates), held)


__all__ = [
    "REFUND_WORDS",
    "NAME_MIN",
    "REVIEW_KINDS",
    "Candidate",
    "ReviewItem",
    "MatchResult",
    "normalise_name",
    "name_similarity",
    "is_refund",
    "payer_fits",
    "invoices_named",
    "match_payments",
]
