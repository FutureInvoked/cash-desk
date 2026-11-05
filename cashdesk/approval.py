"""The hash an approval is tied to, and the idempotency key of a send.

An approval covers one exact text to one exact set of recipients under one
subject. The hash is sha256 over a canonical form (NFC, ``\\n`` line endings,
no trailing whitespace, recipients lower-cased and sorted), so a copy that
differs only in line endings is the same draft and any change to a figure or
a word is a different one. The send layer checks the hash again at send time.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from collections.abc import Iterable
from dataclasses import replace

from .model import Draft

_HASH_PREFIX = "cashdesk-draft-v1"
_KEY_PREFIX = "cashdesk-send-v1"
_SEP = "\x1e"


def canonical_text(text: str) -> str:
    """NFC, ``\\n`` line endings, trailing whitespace stripped per line and at the end."""
    normalised = unicodedata.normalize("NFC", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip() for line in normalised.split("\n")).rstrip("\n")


def canonical_recipients(recipients: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({unicodedata.normalize("NFC", r).strip().lower() for r in recipients if r and r.strip()}))


def draft_hash(text: str, recipients: Iterable[str], subject: str) -> str:
    """sha256 hex of the canonical subject, recipients and text."""
    payload = _SEP.join((_HASH_PREFIX, canonical_text(subject), ",".join(canonical_recipients(recipients)), canonical_text(text)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key(draft_id: str, draft_hash_value: str) -> str:
    """32 hex characters naming one send of one exact draft; a repeat is the same key."""
    return hashlib.sha256(f"{_KEY_PREFIX}|{draft_id}|{draft_hash_value}".encode("utf-8")).hexdigest()[:32]


def verify_hash(draft: Draft, text: str, recipients: Iterable[str], subject: str) -> bool:
    """True when ``draft.hash`` is the hash of exactly these text, recipients and subject."""
    if not draft.hash:
        return False
    return hmac.compare_digest(draft.hash, draft_hash(text, recipients, subject))


def with_hash(draft: Draft) -> Draft:
    """The same draft with its hash filled from its own body, recipients and subject."""
    return replace(draft, hash=draft_hash(draft.body, draft.recipients, draft.subject))


__all__ = ["canonical_text", "canonical_recipients", "draft_hash", "idempotency_key", "verify_hash", "with_hash"]
