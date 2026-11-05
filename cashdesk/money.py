"""Decimal money: parsing in every format the desk sees, house-style output.

English writes ``EUR 1,250.00``; Czech writes ``1 250,00 Kč`` with a no-break
space as the thousands separator and before the currency. Totals are kept per
currency and joined with `` + ``; amounts in different currencies are never
added together.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

NBSP = "\u00a0"
NNBSP = "\u202f"
THIN_SPACES = (NBSP, NNBSP, "\u2009", "\u2007")
_CENTS = Decimal("0.01")

CURRENCY_SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "Kč": "CZK", "zł": "PLN"}
CURRENCY_WORDS = {
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "eura": "EUR", "eurech": "EUR",
    "czk": "CZK", "kč": "CZK", "kc": "CZK", "koruna": "CZK", "koruny": "CZK", "korun": "CZK", "korunách": "CZK",
    "crown": "CZK", "crowns": "CZK",
    "usd": "USD", "dollar": "USD", "dollars": "USD",
    "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "pln": "PLN", "chf": "CHF",
}
CURRENCY_CS = {"CZK": "Kč"}

_CUR = r"(?:[A-Za-z]{3}|€|\$|£|Kč|Kc|zł)"
_WORD = r"(?:euros?|eura|korun[ay]?|korunách|crowns?|dollars?|pounds?)"
_AMOUNT_RE = re.compile(
    rf"^(?P<sign1>[-+−])?\s*(?P<cur1>{_CUR})?\s*(?P<sign2>[-+−])?\s*"
    rf"(?P<num>\d(?:[\d .,'’]*\d)?)(?P<dash>\s*,\s*[-–]|\s*\.\s*[-–])?\s*(?P<cur2>{_WORD}\b|{_CUR})?\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Amount:
    value: Decimal
    currency: str | None


def quantize(value: Decimal | str | int) -> Decimal:
    """Two decimal places, rounding half up (the accountant's rounding)."""
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def normalise_currency(token: str | None) -> str | None:
    if not token:
        return None
    token = token.strip()
    if token in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[token]
    low = token.lower()
    if low in CURRENCY_WORDS:
        return CURRENCY_WORDS[low]
    if re.fullmatch(r"[A-Za-z]{3}", token):
        return token.upper()
    return None


def _digits(num: str, lang: str | None) -> Decimal | None:
    """Turn ``1.250,00`` / ``1,250.00`` / ``1 250`` into a Decimal, by language when ambiguous."""
    num = num.replace("'", "").replace("’", "").replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", num):
        return None
    has_dot, has_comma = "." in num, "," in num
    if has_dot and has_comma:
        decimal_sep = "." if num.rfind(".") > num.rfind(",") else ","
        thousands = "," if decimal_sep == "." else "."
        if num.count(decimal_sep) != 1:
            return None
        whole, frac = num.split(decimal_sep)
        whole = whole.replace(thousands, "")
        return Decimal(f"{whole}.{frac}")
    sep = "." if has_dot else "," if has_comma else None
    if sep is None:
        return Decimal(num)
    parts = num.split(sep)
    if len(parts) > 2:
        if all(len(p) == 3 for p in parts[1:]):
            return Decimal("".join(parts))
        return None
    whole, frac = parts
    if len(frac) == 3 and len(whole) <= 3:
        # one separator before exactly three digits: thousands or decimal by language
        if lang == "cs":
            is_thousands = sep == "."
        elif lang == "en":
            is_thousands = sep == ","
        else:
            is_thousands = sep == "," or whole != "0"
        if is_thousands:
            return Decimal(whole + frac)
        return Decimal(f"{whole}.{frac}")
    if len(frac) == 3:
        return Decimal(whole + frac)
    if len(frac) > 3:
        return None
    return Decimal(f"{whole}.{frac}")


def parse_amount(text: str, lang: str | None = None) -> Amount | None:
    """Read one amount out of ``text``; None when it is not an amount.

    Handles ``1,250.00``, ``1250.00``, ``€1,250``, ``1 250,00 Kč``, ``1.250,00 €``,
    ``CZK 1 250``, ``12 500,-`` and no-break spaces. A lone separator followed by
    three digits (``1.250`` / ``1,250``) is decided by ``lang``: English reads a
    comma as thousands and a dot as decimal, Czech the other way round.
    """
    if text is None:
        return None
    cleaned = str(text)
    for space in THIN_SPACES:
        cleaned = cleaned.replace(space, " ")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    m = _AMOUNT_RE.match(cleaned)
    if not m:
        return None
    value = _digits(m.group("num"), lang)
    if value is None:
        return None
    currency = normalise_currency(m.group("cur1")) or normalise_currency(m.group("cur2"))
    if (m.group("cur1") and currency is None) or (m.group("cur2") and currency is None):
        return None
    if m.group("sign1") or m.group("sign2"):
        sign = (m.group("sign1") or "") + (m.group("sign2") or "")
        if sign.count("-") + sign.count("−") == 1:
            value = -value
    try:
        return Amount(value.quantize(_CENTS, rounding=ROUND_HALF_UP), currency)
    except InvalidOperation:
        return None


def format_money(amount: Decimal, currency: str, lang: str = "en") -> str:
    """House style: en ``EUR 1,200.00``; cs ``1 200,00 Kč`` (no-break spaces)."""
    value = quantize(amount)
    currency = (currency or "").upper()
    if lang == "cs":
        sign = "-" if value < 0 else ""
        whole, cents = f"{abs(value):,.2f}".split(".")
        return f"{sign}{whole.replace(',', NBSP)},{cents}{NBSP}{CURRENCY_CS.get(currency, currency)}"
    return f"{currency} {value:,.2f}"


def sum_by_currency(items: Iterable[tuple[str, Decimal]]) -> dict[str, Decimal]:
    """{currency: total}. Each currency is its own total; nothing is converted."""
    totals: dict[str, Decimal] = {}
    for currency, value in items:
        key = (currency or "").upper()
        totals[key] = totals.get(key, Decimal("0.00")) + quantize(value)
    return totals


def format_totals(totals: Mapping[str, Decimal], lang: str = "en") -> str:
    """``CZK 24,200.00 + EUR 1,200.00``; ``-`` when there is nothing."""
    return " + ".join(format_money(v, c, lang) for c, v in sorted(totals.items())) or "-"


def cs_days(n: int) -> str:
    """Czech day count: 1 den, 2-4 dny, otherwise dní."""
    if n == 1:
        return "1 den"
    if 2 <= n <= 4:
        return f"{n} dny"
    return f"{n} dní"


__all__ = [
    "NBSP",
    "Amount",
    "quantize",
    "normalise_currency",
    "parse_amount",
    "format_money",
    "sum_by_currency",
    "format_totals",
    "cs_days",
]
