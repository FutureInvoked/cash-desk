"""Shared test helpers (not a package: pytest puts tests/ on sys.path)."""

from __future__ import annotations

import re

# The desk's old brand name must not appear anywhere in this repository, the
# tests included, so the tests that assert its absence assemble it at runtime.
OLD_BRAND = "".join(("Your", "Finance"))

# The English word list the desk's batch-9 check used on Czech drafts, kept in
# full so a stray English word in a Czech template is caught.
ENGLISH = re.compile(
    r"\b(the|and|invoice|invoices|please|payment|reminder|dear|hi|regards|thank|thanks|overdue|due|subject|notice|accounts|"
    r"draft|cash|bank|revenue|costs|cost|month|total|review|approved|line|customer|comment|comments|team|held|dispute|refund|"
    r"credit|void|none|days|date|sent|reviewer|signed|desk|project|projects|profit|work|value|invoiced|unbilled|deferred|income|"
    r"needs|check|accountant|missing|higher|lower|same|average|figures|export|prepared|posted|kind|language|to)\b",
    re.IGNORECASE,
)


def english_words(text: str) -> list[str]:
    """English words found in a text that should be entirely Czech.

    Placeholders, e-mail addresses, invoice-number-like tokens and all-caps
    codes (IBAN, EUR, CZK) are removed first, as the original check did.
    """
    cleaned = re.sub(r"\{[a-z_]+\}|[\w.-]+@[\w.-]+|`[^`]*`|\b[A-Z]{1,4}-?\d[\w-]*\b|\b[A-Z]{2,}\b", "", text)
    return ENGLISH.findall(cleaned)
