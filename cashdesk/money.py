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
    rf"^(?P<open>\()?\s*(?P<sign1>[-+−])?\s*(?P<cur1>{_CUR})?\s*(?P<sign2>[-+−])?\s*"
    rf"(?P<num>\d(?:[\d .,'’]*\d)?)(?P<dash>\s*,\s*[-–]|\s*\.\s*[-–])?\s*(?P<sign3>[-−])?\s*"
    rf"(?P<cur2>{_WORD}\b|{_CUR})?\.?\s*(?P<close>\))?\s*$",
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


def _decimal(whole: str, frac: str, strict: bool = False) -> Decimal | None:
    """``whole.frac`` as a Decimal; with ``strict``, None when more than two decimals are not zeros."""
    if strict and len(frac.rstrip("0")) > 2:
        return None
    return Decimal(f"{whole}.{frac}") if frac else Decimal(whole)


def _digits(num: str, lang: str | None, strict: bool = False) -> Decimal | None:
    """Turn ``1.250,00`` / ``1,250.00`` / ``1 250`` into a Decimal, by language when ambiguous.

    A lone separator before exactly three digits is a thousands separator only
    when the group before it has one to three digits (``1.250``, ``250,000``)
    and is not a bare ``0``; after a longer group it is a decimal point
    (``1250.500`` is 1250.50). With ``strict``, more than two decimals that are
    not zeros (``1250.505``) are refused rather than rounded.
    """
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
        return _decimal(whole.replace(thousands, ""), frac, strict)
    sep = "." if has_dot else "," if has_comma else None
    if sep is None:
        return Decimal(num)
    parts = num.split(sep)
    if len(parts) > 2:
        if 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:]):
            return Decimal("".join(parts))
        return None
    whole, frac = parts
    if len(frac) == 3 and len(whole) <= 3 and whole != "0" and not whole.startswith("0"):
        # one separator before exactly three digits after a short group: thousands or decimal by language
        if lang == "cs":
            is_thousands = sep == "."
        elif lang == "en":
            is_thousands = sep == ","
        else:
            is_thousands = True
        if is_thousands:
            return Decimal(whole + frac)
    return _decimal(whole, frac, strict)


def parse_amount(text: str, lang: str | None = None, *, strict: bool = False) -> Amount | None:
    """Read one amount out of ``text``; None when it is not an amount.

    Handles ``1,250.00``, ``1250.00``, ``€1,250``, ``1 250,00 Kč``, ``1.250,00 €``,
    ``CZK 1 250``, ``12 500,-`` and no-break spaces. A lone separator followed by
    three digits after a group of one to three digits (``1.250`` / ``1,250``) is
    decided by ``lang``: English reads a comma as thousands and a dot as
    decimal, Czech the other way round; without a language it is thousands.
    After a longer group it is always a decimal point (``1250.500`` is 1250.50).
    More decimals round half up to cents, except with ``strict`` (the importers),
    where more than two non-zero decimals (``1250.505``) are refused. A trailing minus
    (``1.200,00-``) and accounting parentheses (``(1,200.00)``) are negative.
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
    value = _digits(m.group("num"), lang, strict)
    if value is None:
        return None
    first_currency, last_currency = (normalise_currency(m.group(key)) for key in ("cur1", "cur2"))
    if first_currency and last_currency and first_currency != last_currency:
        return None
    currency = first_currency or last_currency
    if (m.group("cur1") and currency is None) or (m.group("cur2") and currency is None):
        return None
    if bool(m.group("open")) != bool(m.group("close")):
        return None
    sign = (m.group("sign1") or "") + (m.group("sign2") or "") + (m.group("sign3") or "")
    minuses = sign.count("-") + sign.count("−")
    if minuses > 1 or (minuses and m.group("open")):
        return None
    if minuses == 1 or m.group("open"):
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
