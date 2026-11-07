"""Records the engine passes around: frozen dataclasses with Decimal money.

Every amount is a :class:`decimal.Decimal` quantised to cents; a ``float`` is
refused. Enumerated fields (language, invoice status, draft kind and status,
figure kind) are validated on construction so a bad value fails at the edge,
not deep inside a run.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

LANGUAGES = ("en", "cs")
INVOICE_STATUSES = ("open", "paid", "void", "disputed")
DRAFT_KINDS = ("stage07", "stage14", "stage21", "stage28", "thanks", "escalation")
DRAFT_STATUSES = ("pending", "approved", "sent", "held", "rejected")
FIGURE_KINDS = ("money", "date", "invoice_no", "reference", "iban")
PAYMENT_SOURCES = ("bank", "stripe", "manual")

_CENTS = Decimal("0.01")


def to_money(value: object, what: str = "amount") -> Decimal:
    """Coerce ``value`` to a Decimal with two places; floats are a TypeError."""
    if isinstance(value, float):
        raise TypeError(f"{what} must be a Decimal or string, not float ({value!r})")
    if isinstance(value, bool):
        raise TypeError(f"{what} must be a Decimal, not bool")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{what} is not a number: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"{what} is not a finite number: {value!r}")
    return dec.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _choice(value: str, allowed: tuple[str, ...], what: str) -> str:
    text = str(value).strip().lower()
    if text not in allowed:
        raise ValueError(f"{what} must be one of {', '.join(allowed)}, not {value!r}")
    return text


def _language(value: str) -> str:
    text = (value or "en").strip().lower()
    if text not in LANGUAGES:
        raise ValueError(f"language must be one of {', '.join(LANGUAGES)}, not {value!r}")
    return text


def _set(obj: object, name: str, value: object) -> None:
    object.__setattr__(obj, name, value)


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    language: str = "en"
    email: str = ""
    contact_name: str = ""
    on_hold: bool = False
    hold_reason: str = ""

    def __post_init__(self) -> None:
        _set(self, "id", str(self.id).strip())
        _set(self, "name", str(self.name).strip())
        _set(self, "language", _language(self.language))
        _set(self, "email", (self.email or "").strip())
        _set(self, "contact_name", (self.contact_name or "").strip())
        _set(self, "on_hold", bool(self.on_hold))
        _set(self, "hold_reason", (self.hold_reason or "").strip())

    @property
    def first_name(self) -> str:
        return self.contact_name.split()[0] if self.contact_name else ""


@dataclass(frozen=True)
class Invoice:
    number: str
    customer_id: str
    issue_date: dt.date
    due_date: dt.date
    amount: Decimal
    currency: str
    balance: Decimal
    status: str = "open"
    reference: str = ""
    iban: str = ""
    paid_date: dt.date | None = None
    note: str = ""

    def __post_init__(self) -> None:
        _set(self, "number", str(self.number).strip())
        _set(self, "customer_id", str(self.customer_id).strip())
        _set(self, "amount", to_money(self.amount, "amount"))
        _set(self, "balance", to_money(self.balance, "balance"))
        _set(self, "currency", str(self.currency).strip().upper())
        _set(self, "status", _choice(self.status, INVOICE_STATUSES, "status"))
        _set(self, "reference", (self.reference or "").strip())
        _set(self, "iban", (self.iban or "").replace(" ", "").upper())
        _set(self, "note", (self.note or "").strip())

    def days_overdue(self, as_of: dt.date) -> int:
        """Days past the due date (negative when not yet due)."""
        return (as_of - self.due_date).days

    @property
    def is_void(self) -> bool:
        return self.status == "void"

    @property
    def is_paid(self) -> bool:
        return self.status == "paid" or (self.balance <= 0 and not self.is_void)

    @property
    def is_disputed(self) -> bool:
        return self.status == "disputed"

    @property
    def is_open(self) -> bool:
        """Carries a balance a customer could still pay (open or disputed)."""
        return self.balance > 0 and self.status in ("open", "disputed")


@dataclass(frozen=True)
class Payment:
    id: str
    date: dt.date
    amount: Decimal
    currency: str
    reference: str = ""
    counterparty: str = ""
    memo: str = ""
    source: str = "bank"
    identity_origin: str = "provided"

    def __post_init__(self) -> None:
        _set(self, "id", str(self.id).strip())
        _set(self, "amount", to_money(self.amount, "amount"))
        _set(self, "currency", str(self.currency).strip().upper())
        _set(self, "reference", (self.reference or "").strip())
        _set(self, "counterparty", (self.counterparty or "").strip())
        _set(self, "memo", (self.memo or "").strip())
        _set(self, "source", (self.source or "bank").strip().lower())
        _set(self, "identity_origin", _choice(self.identity_origin, ("provided", "derived", "unknown"), "payment identity origin"))

    @property
    def is_outgoing(self) -> bool:
        return self.amount < 0

    @property
    def text(self) -> str:
        """Reference and memo together: what a matcher reads."""
        return " ".join(part for part in (self.reference, self.memo) if part)


@dataclass(frozen=True)
class Match:
    payment_id: str
    invoice_number: str
    amount: Decimal
    currency: str
    method: str
    confidence: str
    reason: str = ""

    def __post_init__(self) -> None:
        _set(self, "amount", to_money(self.amount, "amount"))
        _set(self, "currency", str(self.currency).strip().upper())


@dataclass(frozen=True)
class Figure:
    """One figure the ledger owns: a kind plus its canonical spelling.

    ``raw`` and ``line`` say where the figure was found and never take part in
    equality, so a figure rendered into a draft equals the same figure read
    back out of free text.
    """

    kind: str
    canonical: str
    raw: str = field(default="", compare=False)
    line: int = field(default=0, compare=False)
    valid: bool = field(default=True, compare=False)

    def __post_init__(self) -> None:
        _set(self, "kind", _choice(self.kind, FIGURE_KINDS, "figure kind"))
        _set(self, "canonical", str(self.canonical))

    # Canonical forms. Render and Figure Lock both build figures through these,
    # so a figure written into a draft equals the same figure read back out.

    @classmethod
    def money(cls, value: Decimal, currency: str | None, raw: str = "", line: int = 0) -> "Figure":
        """``EUR 1250.00``; an amount without a currency is ``? 1250.00``."""
        return cls("money", f"{(currency or '?').upper()} {to_money(value)}", raw, line)

    @classmethod
    def date(cls, value: dt.date, raw: str = "", line: int = 0) -> "Figure":
        return cls("date", value.isoformat(), raw, line)

    @classmethod
    def invoice(cls, number: str, raw: str = "", line: int = 0) -> "Figure":
        """Letters and digits only, upper-case: ``INV-2026/0042`` -> ``INV20260042``."""
        return cls("invoice_no", _alnum(number), raw or number, line)

    @classmethod
    def reference(cls, reference: str, raw: str = "", line: int = 0) -> "Figure":
        """A payment reference or variable symbol, letters and digits only."""
        return cls("reference", _alnum(reference), raw or reference, line)

    @classmethod
    def iban(cls, iban: str, raw: str = "", line: int = 0, valid: bool = True) -> "Figure":
        """Spaceless upper-case IBAN."""
        return cls("iban", _alnum(iban), raw or iban, line, valid)

    @property
    def amount(self) -> Decimal | None:
        """The Decimal of a money figure, else None."""
        if self.kind != "money":
            return None
        return Decimal(self.canonical.split(" ", 1)[1])

    @property
    def currency(self) -> str | None:
        """The currency of a money figure (None when it was written without one)."""
        if self.kind != "money":
            return None
        code = self.canonical.split(" ", 1)[0]
        return None if code == "?" else code


def _alnum(text: str) -> str:
    return "".join(ch for ch in str(text).upper() if ch.isalnum())


@dataclass(frozen=True)
class Draft:
    id: str
    kind: str
    customer_id: str
    invoice_numbers: tuple[str, ...]
    language: str
    subject: str
    body: str
    recipients: tuple[str, ...]
    created_at: dt.datetime
    status: str = "pending"
    hash: str = ""
    figures: tuple[Figure, ...] = ()

    def __post_init__(self) -> None:
        _set(self, "kind", _choice(self.kind, DRAFT_KINDS, "draft kind"))
        _set(self, "language", _language(self.language))
        _set(self, "status", _choice(self.status, DRAFT_STATUSES, "draft status"))
        _set(self, "invoice_numbers", tuple(str(n) for n in self.invoice_numbers))
        _set(self, "recipients", tuple(str(r).strip() for r in self.recipients if str(r).strip()))
        _set(self, "figures", tuple(self.figures))

    @property
    def text(self) -> str:
        """Subject and body as one text: the unit Figure Lock checks."""
        return f"{self.subject}\n{self.body}"


__all__ = [
    "LANGUAGES",
    "INVOICE_STATUSES",
    "DRAFT_KINDS",
    "DRAFT_STATUSES",
    "FIGURE_KINDS",
    "PAYMENT_SOURCES",
    "to_money",
    "Customer",
    "Invoice",
    "Payment",
    "Match",
    "Figure",
    "Draft",
]
