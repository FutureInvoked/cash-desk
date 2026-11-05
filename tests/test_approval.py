"""approval.py: the hash an approval is tied to, and the send idempotency key."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

from cashdesk.approval import canonical_text, draft_hash, idempotency_key, verify_hash, with_hash
from cashdesk.model import Draft

BODY = "Hi Jana,\n\n  - INV-2026-0042 due 2026-10-27: EUR 1,250.00 (14 days overdue)\n\nKind regards,\nNorthgate\n"
TO = ("jana@modra.example",)
SUBJECT = "Second reminder: 1 invoice(s) overdue (EUR 1,250.00)"


def test_hash_is_sha256_hex_and_stable():
    h = draft_hash(BODY, TO, SUBJECT)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h == draft_hash(BODY, TO, SUBJECT)


def test_hash_ignores_line_endings_trailing_whitespace_and_unicode_form():
    base = draft_hash(BODY, TO, SUBJECT)
    assert draft_hash(BODY.replace("\n", "\r\n"), TO, SUBJECT) == base
    assert draft_hash(BODY.replace("\n", "  \n"), TO, SUBJECT) == base
    assert draft_hash(BODY.rstrip("\n"), TO, SUBJECT) == base
    czech = "1 250,00 Kč"
    assert draft_hash(unicodedata.normalize("NFD", czech), TO, SUBJECT) == draft_hash(unicodedata.normalize("NFC", czech), TO, SUBJECT)
    assert canonical_text("a \r\nb\r\n\r\n") == "a\nb"


def test_hash_ignores_recipient_order_and_case_but_not_identity():
    two = ("B@x.example", "a@x.example")
    assert draft_hash(BODY, two, SUBJECT) == draft_hash(BODY, ("a@x.example", "b@x.example"), SUBJECT)
    assert draft_hash(BODY, two, SUBJECT) != draft_hash(BODY, ("a@x.example",), SUBJECT)
    assert draft_hash(BODY, (), SUBJECT) != draft_hash(BODY, TO, SUBJECT)


def test_any_change_to_text_or_subject_changes_the_hash():
    base = draft_hash(BODY, TO, SUBJECT)
    assert draft_hash(BODY.replace("1,250.00", "1,520.00"), TO, SUBJECT) != base
    assert draft_hash(BODY.replace("Hi", "Dear"), TO, SUBJECT) != base
    assert draft_hash(BODY, TO, SUBJECT + " ") == base, "trailing whitespace in the subject is not a change"
    assert draft_hash(BODY, TO, "Final notice") != base


def test_idempotency_key_depends_on_draft_id_and_hash():
    h = draft_hash(BODY, TO, SUBJECT)
    key = idempotency_key("stage14-C002-20261110-abc123", h)
    assert re.fullmatch(r"[0-9a-f]{32}", key)
    assert key == idempotency_key("stage14-C002-20261110-abc123", h)
    assert key != idempotency_key("stage14-C002-20261110-abc124", h)
    assert key != idempotency_key("stage14-C002-20261110-abc123", draft_hash(BODY + "x", TO, SUBJECT))


def test_with_hash_and_verify_hash():
    draft = Draft("d1", "stage14", "C002", ("INV-2026-0042",), "en", SUBJECT, BODY, TO, dt.datetime(2026, 11, 10, 7))
    assert draft.hash == "" and verify_hash(draft, BODY, TO, SUBJECT) is False
    hashed = with_hash(draft)
    assert hashed.hash == draft_hash(BODY, TO, SUBJECT)
    assert verify_hash(hashed, BODY, TO, SUBJECT) is True
    assert verify_hash(hashed, BODY.replace("1,250.00", "1,520.00"), TO, SUBJECT) is False
    assert verify_hash(hashed, BODY, ("other@x.example",), SUBJECT) is False
    assert verify_hash(hashed, BODY, TO, "Other subject") is False
