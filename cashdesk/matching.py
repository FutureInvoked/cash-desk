"""Payment matching: exact application only, everything else goes to a person.

A payment is applied automatically when its reference names exactly one ledger
invoice (invoice number with supported separators, or the Czech variable symbol),
in that invoice's currency, for exactly its open balance, from a payer whose
normalized name matches only that invoice's customer (or a bounded unique bank
truncation), dated no more than three days before the invoice. Every other receipt
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
from functools import lru_cache
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .model import Customer, Invoice, Match, Payment
from .money import format_money, sum_by_currency

RETURN_WORDS = re.compile(r"\b(returned|revers(?:ed|al)|reverted|chargeback|bounced|dishonou?red|unpaid\s+(?:cheque|check)|vr[aá]cen[aá]\s+platba)\b", re.IGNORECASE)


def is_return(text: str) -> bool:
    """Explicit return evidence; ordinary outgoing refunds are a separate question."""
    return bool(RETURN_WORDS.search(text))


REFUND_WORDS = re.compile(r"\b(refund(?:ed|s)?|vratk[ay]|vrácení|vraceni|dobropis(?:u|em)?|credit\s*note|chargeback|storno)\b", re.IGNORECASE)
INVOICE_LIKE = re.compile(r"\b(?:inv(?:oice)?|fa(?:ktura)?|fi|vs)[ .#:/-]*\d+\b", re.IGNORECASE)
NAME_MIN = 0.6
AMOUNT_TOLERANCE = Decimal("0.05")  # a candidate within 5 % of the balance scores half
DATE_WINDOW = dt.timedelta(days=60)
MAX_CANDIDATES = 3
MAX_COMBINATION = 4
MIN_REFERENCE_DIGITS = 3

REVIEW_KINDS = ("MISSING", "AMBIGUOUS", "PARTIAL", "OVERPAID", "MULTI_INVOICE", "CURRENCY", "ALREADY_PAID", "VOID", "AFTER_AS_OF", "CREDIT_NOTE", "UNMATCHED",
                "PAYER", "EARLY", "POSSIBLE_DUPLICATE", "RECEIPT_UPDATE", "INVOICE_IMPORT", "RETURNED_PAYMENT", "CUSTOMER_IDENTITY", "BANK_EVIDENCE", "REFERENCE", "ALREADY_COUNTED")
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
    unknown_references: tuple[str, ...] = ()


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
            "review_in": sum_by_currency((i.payment.currency, i.payment.amount) for i in (*self.review, *self.unmatched) if i.payment.amount > 0 and i.kind not in ("POSSIBLE_DUPLICATE", "RECEIPT_UPDATE", "RETURNED_PAYMENT", "BANK_EVIDENCE")),
            "out": sum_by_currency((i.payment.currency, -i.payment.amount) for i in self.credit_notes if i.payment.amount < 0),
        }


# ------------------------------------------------------------------ helpers


def _name_text(name: str) -> str:
    """Unicode case/punctuation normalization; fold Latin accents, not other scripts.

    Legal suffix handling is separate. Non-Latin combining marks remain
    significant (for example Japanese voiced characters are not interchangeable).
    """
    chars = []
    for ch in unicodedata.normalize("NFKC", name or "").casefold():
        if unicodedata.name(ch, "").startswith("LATIN"):
            chars.extend(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        else:
            chars.append(ch)
    text = " ".join("".join(c if c.isalnum() or unicodedata.category(c).startswith("M") else " "
                           for c in chars).split())
    return text


# Recognized trailing forms only: spelling equivalents share a canonical form;
# forms in different jurisdictions are not inferred to mean the same entity.
LEGAL_FORMS = (
    (r"spol s r o|s ?r ?o", "sro"), (r"sp z o o", "spzoo"),
    (r"limited|ltd", "ltd"), (r"incorporated|inc", "inc"), (r"corporation|corp", "corp"),
    (r"l ?l ?c", "llc"), (r"l ?l ?p", "llp"), (r"p ?l ?c", "plc"),
    (r"g ?m ?b ?h", "gmbh"), (r"b ?v", "bv"), (r"a ?s", "as"),
    (r"oy", "oy"), (r"ag", "ag"), (r"kg", "kg"), (r"sa", "sa"), (r"z s", "zs"),
    (r"co", "co"),
)
LEGAL_FORM_PATTERNS = tuple((re.compile(r"(?:^|\s)(?:"+pattern+r")$"), form) for pattern,form in LEGAL_FORMS)


def _name_parts(name: str) -> tuple[str, tuple[str, ...]]:
    text=_name_text(name)
    forms=[]
    while text:
        for pattern,form in LEGAL_FORM_PATTERNS:
            match=pattern.search(text)
            if match:
                if form != "co":forms.append(form)  # 'Co' alone does not establish a legal form.
                text=text[:match.start()].strip()
                break
        else:break
    return text, tuple(reversed(forms))


def normalise_name(name: str) -> str:
    """Name base for candidate ranking; not a standalone legal identity check."""
    return _name_parts(name)[0]


def payer_legal_conflict(customer_name: str, counterparty: str) -> bool:
    """Both names explicitly state recognized, incompatible trailing forms."""
    customer_forms=_name_parts(customer_name)[1]
    payer_forms=_name_parts(counterparty)[1]
    return bool(customer_forms and payer_forms and customer_forms != payer_forms)


def name_similarity(a: str, b: str) -> float:
    """Candidate ranking only; a fuzzy score never authorizes an allocation."""
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


_EXTRA_LATIN = str.maketrans({"ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i"})


def _squeezed(name: str) -> str:
    """Name base as banks write it: apostrophes removed (ZOË'S -> ZOES, O'BRIEN -> OBRIEN), extra
    Latin letters folded (Ł -> L), 'and'/'und' dropped. Spaces stay: KINGS WAY is not KINGSWAY."""
    text = re.sub(r"['’`´]", "", (name or "")).translate(_EXTRA_LATIN)
    return " ".join(w for w in normalise_name(text).split() if w not in ("and", "und"))


def _squeezed_spelled(name: str) -> str:
    return _squeezed(_GERMAN_SPELLING.sub(lambda m: _GERMAN_DIGRAPHS[m.group(0)], name or ""))


_GERMAN_DIGRAPHS = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
_GERMAN_SPELLING = re.compile("[äöüÄÖÜß]")


def payer_fits(customer_name: str, counterparty: str) -> bool:
    """Exact normalized identity or a bounded Latin bank-name truncation.

    Truncations must preserve at least two complete leading words and twelve
    characters; an optional last partial word must have at least two characters.
    The caller must also rule out another known customer fitting that payer.
    Missing names, shared words, reordered words and spelling similarity are not
    evidence of identity. Unstructured bank descriptions remain review evidence.
    """
    if payer_legal_conflict(customer_name, counterparty):
        return False
    name, payer = normalise_name(customer_name), normalise_name(counterparty)
    if not name or not payer:
        return False
    if name == payer:
        return True
    # Accounting systems file people as "Smith, John"; banks write "JOHN SMITH".
    if customer_name.count(",") == 1:
        last, first = (part.strip() for part in customer_name.split(","))
        if last and first and normalise_name(f"{first} {last}") == payer:
            return True
    # Bank lines drop apostrophes, spell umlauts out and write "and" for "&": compare a squeezed key.
    if _squeezed(customer_name) and _squeezed(customer_name) in {_squeezed(counterparty), _squeezed_spelled(counterparty)} \
            or _squeezed_spelled(customer_name) and _squeezed_spelled(customer_name) == _squeezed(counterparty):
        return True
    # "O'Brien & Sons" is "O'BRIEN AND SONS" on a bank line.
    drop_and = lambda s: " ".join(w for w in s.split() if w not in ("and", "und"))
    if drop_and(name) == drop_and(payer) and drop_and(name):
        return True
    # German banks often spell umlauts out: Müller GmbH pays as "MUELLER GMBH".
    spelled = normalise_name(_GERMAN_SPELLING.sub(lambda m: _GERMAN_DIGRAPHS[m.group(0)], customer_name or ""))
    if spelled and spelled == payer:
        return True
    return _truncation_fits(name, payer) or _truncation_fits(_name_text(customer_name), _name_text(counterparty))


def _truncation_fits(name: str, payer: str) -> bool:
    """A bank cut the name short: at least twelve characters and two whole leading words."""
    if not name or not payer or not name.isascii() or not payer.isascii() or len(payer) < 12 or not name.startswith(payer):
        return False
    words = payer.split()
    if name[len(payer):].startswith(" "):
        return len(words) >= 2
    return len(words) >= 3 and len(words[-1]) >= 2


_LEAD_WORDS = {"payment", "payments", "from", "credit", "bgc", "fp", "faster", "transfer", "sepa", "received", "money",
               "ach", "incoming", "giro", "bank", "trf", "tfr", "by", "of", "ref", "reference", "platba", "od", "prijata", "zahlung", "von"}
_TAIL_WORDS = _LEAD_WORDS | {"inv", "invoice", "faktura", "rechnung", "vs", "paid", "pmt", "remittance", "thank", "thanks", "for", "to"}


def description_names(customer_name: str, text: str) -> bool:
    """The description opens with the customer's whole name.

    Only used when the statement has no payer column at all: many UK and US banks put the
    payer first in the description ("ACME LTD BGC INV-0041"). The name may be preceded only by
    bank boilerplate ("PAYMENT FROM"), followed only by its own legal form, a reference or
    boilerplate; "JOHN SMITH OBO ACME LTD", "ACME HOLDINGS LTD" or a different legal form do
    not count. A name of at least two words or eight letters is required.
    """
    base = normalise_name(customer_name)
    if not base or (len(base.split()) < 2 and len(base) < 8):
        return False
    words, want = _name_text(text or "").split(), base.split()
    lead = 0
    while lead < len(words) and words[lead] in _LEAD_WORDS and words[lead:lead + len(want)] != want:
        lead += 1
    if words[lead:lead + len(want)] != want:
        return False
    end = lead + len(want)
    # Swallow trailing legal-form words ("ltd", "s r o", "gmbh"); they must not conflict.
    best = end
    for extra in range(1, 4):
        candidate = " ".join(words[lead:end + extra])
        if end + extra <= len(words) and _name_parts(candidate)[0] == base:
            best = end + extra
    if payer_legal_conflict(customer_name, " ".join(words[lead:best])):
        return False
    nxt = words[best] if best < len(words) else ""
    return not nxt or nxt in _TAIL_WORDS or any(ch.isdigit() for ch in nxt)


def _payer_ids(names: Mapping[str, str], payment, no_payer_files: frozenset = frozenset()) -> set[str]:
    """Known customers the payment's payer identifies: the payer column, or, when the payment's
    statement has no payer column at all, the description - and then only if exactly one customer
    is named (the longest name wins when one contains another)."""
    if (payment.counterparty or "").strip():
        return {cid for cid, name in names.items() if payer_fits(name, payment.counterparty)}
    if _statement_key(payment) not in no_payer_files:
        return set()
    text = f"{payment.memo} {payment.reference}"
    named = {cid: normalise_name(name) for cid, name in names.items() if description_names(name, text)}
    longest = {cid for cid, base in named.items()
               if not any(other != base and f" {base} " in f" {other} " for other in named.values())}
    return longest if len(longest) == 1 else set()


def no_payer_statements(payments: Iterable[Payment]) -> frozenset:
    """Statements (by receipt-id file key) in which no row has a payer at all."""
    seen: dict[str, bool] = {}
    for p in payments:
        key = _statement_key(p)
        from_column = bool((p.counterparty or "").strip()) and "orig co name:" not in (p.memo or "").lower()
        seen[key] = seen.get(key, False) or from_column
    return frozenset(k for k, has_payer in seen.items() if k and not has_payer)


def _statement_key(payment) -> str:
    return payment.id.rsplit("@", 1)[1] if "@" in payment.id else ""


def payer_may_fit(customer_name: str, counterparty: str) -> bool:
    """Conservative review holds only; never use this predicate to apply money.

    Keep the former broad similarity evidence for overlap/placement holds so
    stronger allocation rules cannot accidentally release an existing question.
    """
    if not (counterparty or "").strip():
        return False
    words = lambda text: {w for w in normalise_name(text).split() if len(w) >= 4}
    return (payer_fits(customer_name, counterparty) or name_similarity(customer_name, counterparty) >= NAME_MIN
            or bool(words(customer_name) & words(counterparty)))


def is_refund(text: str) -> bool:
    return bool(REFUND_WORDS.search(text or ""))


@lru_cache(maxsize=4096)
def _number_pattern(number: str) -> str:
    number=unicodedata.normalize('NFC',number)
    parts = [part for part in re.split(r"[\s\-/_]+|(?<=[^\W\d_])(?=\d)|(?<=\d)(?=[^\W\d_])",number.strip()) if part]
    if parts and all(part.isdigit() for part in parts):
        parts=list(''.join(parts))
    return r"[\s\-/_]*".join(re.escape(part) for part in parts)


@lru_cache(maxsize=4096)
def _identifier_pattern(identifier, *, reference=False):
    pattern = _number_pattern(identifier)
    if re.sub(r"[\s/_-]+","",identifier).isdigit():
        bank_label=r"ref(?:erence)?|payment[\s_-]*ref(?:erence)?"
        label = rf"vs|{bank_label}" if reference else rf"inv(?:oice)?|fa(?:ktura)?|fi|{bank_label}"
        pattern = rf"(?:(?:{label})[\s#:/_-]*)?{pattern}"
    return re.compile(rf"(?<!\w){pattern}(?!\w)",re.I)


def invoices_named(text: str, invoices: Iterable[Invoice]) -> list[str]:
    """Whole Unicode tokens, with supported optional/repeated separators.

    This is lexical candidate discovery, not authority to allocate a receipt.
    Numeric payment references also allow an explicit compact VS prefix.
    """
    hits=[]
    for inv in invoices:
        if inv.number in hits:continue
        if _identifier_in_text(inv.number,text or ""):
            hits.append(inv.number);continue
        ref=inv.reference
        key=re.sub(r"[\s/_-]+","",ref)
        if ref and ref!=inv.number and (not key.isdigit() or len(key)>=MIN_REFERENCE_DIGITS):
            if _identifier_in_text(ref,text or "",reference=True):hits.append(inv.number)
    return hits


def _identifier_in_text(identifier,text,*,reference=False):
    text=unicodedata.normalize('NFC',text)
    def word(char):return bool(char) and (char=='_' or char.isalnum() or unicodedata.category(char).startswith('M'))
    for match in _identifier_pattern(identifier,reference=reference).finditer(text):
        if not word(text[match.start()-1:match.start()] if match.start() else '') and not word(text[match.end():match.end()+1]):
            return True
    return False


@dataclass(frozen=True)
class ReferenceEvidence:
    named: tuple[str,...]
    unknown: tuple[str,...]=()
    conflicts: tuple[str,...]=()

    @property
    def problem(self):
        parts=list(self.conflicts)
        if self.unknown:parts.append("additional or unknown invoice/reference identifiers: "+", ".join(self.unknown))
        return "; ".join(parts)


REFERENCE_ID = re.compile(r"(?<!\w)(inv(?:oice)?|fa(?:ktura)?|fi|vs)[ .#:/_-]*(\d+(?:[\s/_-]+\d+)*)(?!\w)",re.I)
GENERIC_ID = re.compile(r"(?<!\w)([^\W\d_]{1,12})[-/_]+(?:\d+(?:[\s/_-]+\d+)*|[^\W_]+(?:[-/_][^\W_]+)*)(?!\w)",re.I)
LABELLED_ID = re.compile(r"(?<!\w)(?:invoice|faktura)(?:\s+(?:number|no|číslo|cislo))?[\s:#]+([^\W\d_]{1,12}[\s/_-]*\d+(?:[\s/_-]+\d+)*)(?!\w)",re.I)
EXPLICIT_ID = re.compile(r"(?<!\w)(?:invoice|faktura)(?:\s+(?:number|no|číslo|cislo))?\s*#\s*([^\W_][\w/-]*)(?!\w)",re.I)
NON_INVOICE_ROLE = r"(?:purchase\s+order|bank[\s_-]*ref(?:erence)?|order|project|po|transaction|txn|trf|booking|objedn[aá]vka|obj|zak[aá]zka|auftrag|bestellung)"
OTHER_ID_PREFIXES = {"ref","reference","payment","po","order","project","transaction","txn","trf","sepa","ach","swift","iban","bic","eur","usd","gbp","czk","pln"}
ADVANCE_INTENT = re.compile(r"\b(?:deposit|advance|prepayment|retainer)\b.{0,80}\b(?:next|future|new)\s+(?:project|order|work)\b|\bz[aá]loh\w*.{0,80}\bnov\w*\s+(?:projekt|zak[aá]zk)\w*|\b(?:not\s+for|do\s+not\s+apply|not\s+against)\b",re.I)


def _reference_key(value):
    return re.sub(r"[\s/_-]+","",unicodedata.normalize('NFC',value or "")).casefold()


def _complete_code(text,start,end):
    # Python's Unicode \w excludes combining marks. Retain the complete code,
    # rather than mistaking its prefix for a different known invoice identifier.
    def word(char):return char.isalnum() or char=='_' or unicodedata.category(char).startswith('M')
    while start and word(text[start-1]):start-=1
    while end<len(text) and (word(text[end]) or text[end] in '/-'):end+=1
    return text[start:end]


def _numeric_proof(text, identifier, *, reference=False):
    prefix=r"^(?:(?:sepa|bank|incoming|credit|payment|transfer|reference|ref|remittance|information|invoice|inv|faktura|vs|variable|symbol|no|number|nr)\b[\s#:/_-]*)+"
    stripped=re.sub(prefix,"",text.strip(),flags=re.I)
    return bool(_identifier_pattern(identifier,reference=reference).fullmatch(stripped))


def reference_evidence(payment: Payment, invoices: Iterable[Invoice]) -> ReferenceEvidence:
    """Keep every known/unknown candidate and reject conflicting identifier roles.

    Reference and memo never concatenate to manufacture an identifier. Invoice
    labels identify numbers; VS labels identify payment references. Free-text
    order/transaction IDs are not invoice proof solely because digits coincide.
    """
    invoices=list(invoices)
    fields=tuple(unicodedata.normalize('NFC',value or "") for value in (payment.reference,payment.memo))
    named=list(dict.fromkeys(n for text in fields for n in invoices_named(text,invoices)))
    unknown=[];conflicts=[]
    for text in fields:
        for match in REFERENCE_ID.finditer(text):
            label,body=match[1].casefold(),match[2]
            token=_complete_code(text,match.start(),match.end())
            exact=any(_reference_key(token) in {_reference_key(inv.number),_reference_key(inv.reference)} for inv in invoices)
            domain=any(_reference_key(body)==_reference_key(inv.reference if label=="vs" else inv.number) for inv in invoices)
            if not exact and not domain:unknown.append(token)
        for match in GENERIC_ID.finditer(text):
            token=_complete_code(text,match.start(),match.end())
            if re.split(r'[-/_]',token,1)[0].casefold() in OTHER_ID_PREFIXES:continue
            if not any(_reference_key(token) in {_reference_key(inv.number),_reference_key(inv.reference)} for inv in invoices):unknown.append(token)
        for match in LABELLED_ID.finditer(text):
            token=_complete_code(text,match.start(1),match.end(1))
            if not any(_reference_key(token)==_reference_key(inv.number) for inv in invoices):unknown.append(token)
        for match in EXPLICIT_ID.finditer(text):
            token=_complete_code(text,match.start(1),match.end(1))
            if not any(_reference_key(token)==_reference_key(inv.number) for inv in invoices):unknown.append(token)
        for inv in invoices:
            for identifier in sorted({inv.number,inv.reference}-{''}):
                if re.search(rf"(?<!\w){NON_INVOICE_ROLE}[\s#:/_-]*{_number_pattern(identifier)}(?!\w)",text,re.I):
                    conflicts.append(f"{identifier} occurs as an order/project/transaction identifier, not verified invoice proof")
                    if inv.number not in named:named.append(inv.number)
    if ADVANCE_INTENT.search(" ".join(fields)):
        conflicts.append("memo/reference describes a future advance or says not to apply the receipt to an invoice")
    for inv in invoices:
        if inv.number not in named:continue
        identifiers=[(inv.number,False)]+([(inv.reference,True)] if inv.reference else [])
        literal=[(identifier,ref) for identifier,ref in identifiers if any(_identifier_in_text(identifier,text,reference=ref) for text in fields)]
        if literal and all(_reference_key(identifier).isdigit() for identifier,_ in literal):
            if not any(_numeric_proof(text,identifier,reference=ref) for identifier,ref in literal for text in fields):
                conflicts.append("numeric identity needs a whole remittance value or explicit invoice/payment-reference label")
    return ReferenceEvidence(tuple(named),tuple(dict.fromkeys(unknown)),tuple(dict.fromkeys(conflicts)))


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
    no_payer_files: frozenset | None = None,
) -> MatchResult:
    """Match payments to invoices; apply only reference + exact amount, same currency.

    Pass every invoice you have (paid and void included) so a payment naming a
    settled invoice is explained as ALREADY_PAID or VOID instead of UNMATCHED.
    ``customers`` supplies known names for automatic identity checks and candidate scoring. With ``as_of``, a
    payment dated later is queued (AFTER_AS_OF) rather than applied.
    """
    payments = list(payments)
    ledger = _Ledger(invoices, _customer_names(customers))
    if no_payer_files is None:
        no_payer_files = no_payer_statements(payments)
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

    def queue(payment: Payment, kind: str, detail: str, named: Sequence[str] = (), cands: Sequence[Candidate] = (), hold_named: bool = True, unknown_references: Sequence[str] | None = None) -> ReviewItem:
        holds: list[str] = []
        if unknown_references is None:unknown_references=reference_evidence(payment,ledger.invoices.values()).unknown
        if hold_named:
            for no in named:
                if no in ledger.invoices and ledger.is_open(no):
                    held.setdefault(no, f"held: payment {payment.id} in review ({kind})")
                    holds.append(no)
        for c in cands:
            if kind == "MULTI_INVOICE":
                held.setdefault(c.invoice_number, f"held: may be paid by {payment.id} ({kind})")
                holds.append(c.invoice_number)
        # Safety evidence is evaluated across the whole ledger, never the
        # three display candidates. A known payer may have split/partial debt.
        if payment.amount > 0 and kind not in ("AFTER_AS_OF", "CREDIT_NOTE", "POSSIBLE_DUPLICATE"):
            payer_ids = {cid for cid, name in ledger.names.items() if payer_fits(name, payment.counterparty)}
            if not payer_ids:
                payer_ids = {cid for cid, name in ledger.names.items() if payer_may_fit(name, payment.counterparty)}
            plausible = [inv.number for inv in ledger.open_in(payment.currency)
                         if inv.customer_id in payer_ids or
                         (not payer_ids and ledger.balance[inv.number] == payment.amount)]
            for no in plausible:
                held[no] = f"receipt review pauses reminders: payment {payment.id} ({kind}) may pay {no}; verify the receipt, then Apply or Dismiss"
                holds.append(no)
        holds = list(dict.fromkeys(holds))
        if holds:
            detail += (f"; receipt review pauses reminders for {', '.join(holds)}; "
                       "verify the receipt, then Apply or Dismiss. A fresh draft and approval are required afterwards")
        item = ReviewItem(payment, kind, detail, tuple(named), tuple(cands), tuple(holds), tuple(unknown_references))
        (unmatched if kind == "UNMATCHED" else credit_notes if kind == "CREDIT_NOTE" else review).append(item)
        return item

    for p in payments:
        if p.id in seen:
            duplicates.append(p)
            continue
        seen.add(p.id)
        amt, cur, text = p.amount, p.currency, p.text
        evidence = reference_evidence(p,ledger.invoices.values())
        named = list(evidence.named)
        if as_of is not None and p.date > as_of:
            queue(p, "AFTER_AS_OF", f"dated {p.date.isoformat()}, after the as-of date {as_of.isoformat()}; not applied in this run", named)
            continue
        if is_return(text):
            item = queue(p, "RETURNED_PAYMENT", "returned payment review: verify the bank return and prior receipts; no automatic reversal or allocation", named, hold_named=False)
            owners = {cid for cid,name in ledger.names.items() if payer_may_fit(name,p.counterparty)}
            for inv in ledger.invoices.values():
                if inv.number in named or inv.customer_id in owners or (not owners and inv.currency == cur and inv.amount == abs(amt)):
                    held[inv.number] = "returned_payment: returned payment review; verify /cashdesk returns before collection or acknowledgement"
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
        if evidence.conflicts or (evidence.unknown and named):
            queue(p,"REFERENCE","reference evidence needs review: "+evidence.problem,named,ledger.candidates(p),unknown_references=evidence.unknown)
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
            owners = {i.customer_id for i in same}
            if (len(same) == len(named) and len(owners) == 1 and tot == amt and amt > 0
                    and all(not i.is_void and i.number not in ledger.paid and ledger.balance[i.number] > 0 for i in same)
                    and _payer_ids(ledger.names, p, no_payer_files) == owners
                    and all(p.date >= i.issue_date - EARLY_GRACE for i in same)):
                # One transfer for several invoices of one customer, to the cent: apply each in full.
                for i in sorted(same, key=lambda x: x.number):
                    bal = ledger.balance[i.number]
                    applied.append(Match(p.id, i.number, bal, cur, "reference+exact-batch", "high",
                                         f"reference names {', '.join(named)}; their open balances add up exactly to {m(amt, cur)} "
                                         "from the payer of that one customer"))
                    ledger.balance[i.number] = Decimal("0.00")
                    ledger.paid.add(i.number)
                continue
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
        elif _payer_ids(ledger.names, p, no_payer_files) != {inv.customer_id}:
            queue(p, "PAYER", f"payer identity needs review: reference names {no} ({ledger.name(inv)}) but the payer "
                  f"'{p.counterparty}' is missing, different or not unique; check who paid before applying. "
                  "Inspect the retained bank description and source statement; similarity alone cannot establish identity", named)
        elif p.date < inv.issue_date - EARLY_GRACE:
            queue(p, "EARLY", f"dated {p.date.isoformat()}, before {no} was issued ({inv.issue_date.isoformat()}); check the dates before applying", named)
        else:
            applied.append(Match(p.id, no, amt, cur, "reference+exact", "high", f"reference names {no}, {m(amt, cur)} equals its open balance, and payer identity matches one known customer within the three-day booking grace"))
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
