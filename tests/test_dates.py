"""dates.py: ISO, numeric by language, and long forms in English and Czech."""

from __future__ import annotations

import datetime as dt

import pytest

from cashdesk.dates import format_date, parse_date

NOV5 = dt.date(2026, 11, 5)
MAY11 = dt.date(2026, 5, 11)


@pytest.mark.parametrize(
    "text, lang, expected",
    [
        ("2026-11-05", "en", NOV5),
        ("2026-11-05", "cs", NOV5),
        ("2026/11/05", "en", NOV5),
        ("20261105", "en", None),
        ("11/05/2026", "en", NOV5),
        ("11/05/2026", "cs", MAY11),
        ("05/11/2026", "en", MAY11),
        ("5/11/2026", "cs", NOV5),
        ("5. 11. 2026", "cs", NOV5),
        ("5. 11. 2026", "en", NOV5),
        ("5.11.2026", "cs", NOV5),
        ("05.11.2026", "en", NOV5),
        ("5 November 2026", "en", NOV5),
        ("5th November 2026", "en", NOV5),
        ("November 5, 2026", "en", NOV5),
        ("Nov 5 2026", "en", NOV5),
        ("5 Nov 2026", "en", NOV5),
        ("5. listopadu 2026", "cs", NOV5),
        ("5. listopad 2026", "cs", NOV5),
        ("5 listopadu 2026", "cs", NOV5),
        ("11. května 2026", "cs", MAY11),
        ("5. listopadu 2026", "en", NOV5),
        ("5 November 2026", "cs", NOV5),
        (" 2026-11-05 ", "en", NOV5),
        ("2026-11-05T10:22:00", "en", NOV5),
        ("2026-11-05 10:22", "en", NOV5),
        ("05.11.2026 14:32:11", "cs", NOV5),
        ("5. 11. 2026 09:00", "cs", NOV5),
        ("11/05/2026 09:00", "en", NOV5),
    ],
)
def test_parse_date_formats(text, lang, expected):
    assert parse_date(text, lang) == expected


@pytest.mark.parametrize("text", ["", "n/a", "5. 11.", "2026-13-05", "31/02/2026", "1250.00", "INV-2026-0042", "32. 11. 2026", "2026-11-5x"])
def test_parse_date_rejects_non_dates(text):
    assert parse_date(text, "en") is None
    assert parse_date(text, "cs") is None


def test_format_date_house_style():
    assert format_date(NOV5, "en") == "2026-11-05"
    assert format_date(NOV5, "cs") == "5. 11. 2026"
    assert format_date(dt.date(2026, 1, 9), "cs") == "9. 1. 2026"


def test_format_date_round_trips():
    for lang in ("en", "cs"):
        assert parse_date(format_date(NOV5, lang), lang) == NOV5
