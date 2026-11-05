"""Dates in the formats customers and exports use, in English and Czech.

Numeric day/month order follows the language: English reads ``11/05/2026``
month first, Czech reads ``5. 11. 2026`` and ``5.11.2026`` day first. A dotted
date is day-first in both languages (nobody writes month-first with dots).
Slashed and dashed dates (``11/05/2026``, ``05-11-2026``) can be given an
explicit ``order`` (``dmy`` or ``mdy``); :func:`infer_date_order` decides it for
a whole file and refuses a file whose dates read both ways.
Long forms (``5 November 2026``, ``November 5, 2026``, ``5. listopadu 2026``)
are accepted whatever the language, since month names are unambiguous.
"""

from __future__ import annotations

import datetime as dt
import re

MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
MONTHS_CS = {
    # genitive (after a day number) and nominative
    "ledna": 1, "února": 2, "března": 3, "dubna": 4, "května": 5, "června": 6, "července": 7,
    "srpna": 8, "září": 9, "října": 10, "listopadu": 11, "prosince": 12,
    "leden": 1, "únor": 2, "březen": 3, "duben": 4, "květen": 5, "červen": 6, "červenec": 7,
    "srpen": 8, "říjen": 10, "listopad": 11, "prosinec": 12,
}
MONTHS = {**MONTHS_EN, **MONTHS_CS}
MONTH_NAME_RE = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))

_TIME = r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"  # a trailing time of day is ignored
_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_DOTTED = re.compile(rf"^(\d{{1,2}})\.\s*(\d{{1,2}})\.\s*(\d{{4}}|\d{{2}}){_TIME}$")
_SLASHED = re.compile(rf"^(\d{{1,2}})([/-])(\d{{1,2}})\2(\d{{4}}|\d{{2}}){_TIME}$")
DATE_ORDERS = ("dmy", "mdy")


class DateOrderError(ValueError):
    """The day/month order of a file's dates cannot be decided."""
_DAY_FIRST = re.compile(rf"^(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+({MONTH_NAME_RE})\.?,?\s+(\d{{4}})$", re.IGNORECASE)
_MONTH_FIRST = re.compile(rf"^({MONTH_NAME_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})$", re.IGNORECASE)


def month_number(name: str) -> int | None:
    return MONTHS.get(name.strip().lower().rstrip("."))


def _date(year: int, month: int, day: int) -> dt.date | None:
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def numeric_parts(text: str) -> tuple[int, int] | None:
    """The two leading numbers of a slashed or dashed date (``03/11/2026`` -> (3, 11)), else None."""
    m = _SLASHED.match(str(text or "").replace("\u00a0", " ").strip())
    return (int(m.group(1)), int(m.group(3))) if m else None


def infer_date_order(texts) -> str | None:
    """``dmy`` or ``mdy`` from every slashed or dashed date in ``texts``; None when no date needs it.

    A first number above 12 proves day-first, a second number above 12 proves
    month-first. Raises :class:`DateOrderError` when the dates prove both, or
    when at least one date reads differently each way (``03/04/2026``) and none
    proves an order.
    """
    day_first = month_first = ambiguous = None
    for text in texts:
        parts = numeric_parts(text)
        if parts is None:
            continue
        a, b = parts
        if a > 12 and 1 <= b <= 12:
            day_first = day_first or str(text).strip()
        elif b > 12 and 1 <= a <= 12:
            month_first = month_first or str(text).strip()
        elif 1 <= a <= 12 and 1 <= b <= 12 and a != b:
            ambiguous = ambiguous or str(text).strip()
    if day_first and month_first:
        raise DateOrderError(f"the dates mix day-first ({day_first}) and month-first ({month_first}); "
                             "fix the file or set date_order to dmy or mdy")
    if day_first:
        return "dmy"
    if month_first:
        return "mdy"
    if ambiguous:
        raise DateOrderError(f"every slashed date in this file reads both ways ({ambiguous} could be day-first or month-first); "
                             "set date_order to dmy or mdy")
    return None


def parse_date(text: str, lang: str = "en", order: str | None = None) -> dt.date | None:
    """Read one date; None when ``text`` is not a date in a supported form.

    ``order`` (``dmy`` or ``mdy``) decides slashed and dashed dates; without it
    the language does (English month-first, Czech day-first).
    """
    if order is not None and order not in DATE_ORDERS:
        raise ValueError(f"order must be one of {', '.join(DATE_ORDERS)}, not {order!r}")
    if text is None:
        return None
    s = str(text).replace("\u00a0", " ").strip()
    if not s:
        return None
    m = _ISO.match(s)
    if m:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _DOTTED.match(s)
    if m:
        return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = _SLASHED.match(s)
    if m:
        a, b, year = int(m.group(1)), int(m.group(3)), int(m.group(4))
        if order == "dmy" or (order is None and lang == "cs"):
            return _date(year, b, a)
        return _date(year, a, b)
    m = _DAY_FIRST.match(s)
    if m:
        month = month_number(m.group(2))
        return _date(int(m.group(3)), month, int(m.group(1))) if month else None
    m = _MONTH_FIRST.match(s)
    if m:
        month = month_number(m.group(1))
        return _date(int(m.group(3)), month, int(m.group(2))) if month else None
    return None


def format_date(d: dt.date, lang: str = "en") -> str:
    """House style: en ISO ``2026-11-05``; cs ``5. 11. 2026``."""
    if lang == "cs":
        return f"{d.day}. {d.month}. {d.year}"
    return d.isoformat()


__all__ = ["MONTHS_EN", "MONTHS_CS", "MONTHS", "MONTH_NAME_RE", "DATE_ORDERS", "DateOrderError", "month_number",
           "numeric_parts", "infer_date_order", "parse_date", "format_date"]
