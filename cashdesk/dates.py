"""Dates in the formats customers and exports use, in English and Czech.

Numeric day/month order follows the language: English reads ``11/05/2026``
month first, Czech reads ``5. 11. 2026`` and ``5.11.2026`` day first. A dotted
date is day-first in both languages (nobody writes month-first with dots).
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
_DOTTED = re.compile(rf"^(\d{{1,2}})\.\s*(\d{{1,2}})\.\s*(\d{{4}}){_TIME}$")
_SLASHED = re.compile(rf"^(\d{{1,2}})/(\d{{1,2}})/(\d{{4}}|\d{{2}}){_TIME}$")
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


def parse_date(text: str, lang: str = "en") -> dt.date | None:
    """Read one date; None when ``text`` is not a date in a supported form."""
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
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if lang == "cs":
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


__all__ = ["MONTHS_EN", "MONTHS_CS", "MONTHS", "MONTH_NAME_RE", "month_number", "parse_date", "format_date"]
