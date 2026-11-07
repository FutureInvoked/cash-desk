"""Plugin-side delivery: an .eml file for the user's mail client, or SMTP when opted in.

No Hermes imports. Nothing here decides *whether* a draft may go out; the gate and
the send tool in the plugin entry point do that. This module only builds the
message and delivers it. It never logs or echoes the SMTP password.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

SEND_MODES = ("eml", "smtp")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class SendError(Exception):
    """Delivery refused or failed. The message never contains credentials."""


@dataclass(frozen=True)
class Outgoing:
    """Exactly what leaves the plugin: the approved text and its addressing."""

    draft_id: str
    draft_hash: str
    subject: str
    body: str
    recipients: tuple[str, ...]
    sender: str
    reply_to: str = ""
    language: str = "en"
    # What the .eml file is called: "2026-11-05 Northgate Sample Studio Ltd - Reminder 7 - INV-2026-0041".
    file_label: str = ""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    mode: str
    detail: str
    path: str = ""
    message_id: str = ""


def _single_line(value: str, what: str) -> str:
    text = (value or "").strip()
    if not text:
        raise SendError(f"empty {what}")
    if "\r" in text or "\n" in text:
        raise SendError(f"{what} must be a single line")
    return text


def _address(value: str, what: str) -> str:
    name, addr = parseaddr(value or "")
    if not addr or "@" not in addr or any(ch in addr for ch in "\r\n ,;<>"):
        raise SendError(f"invalid {what}: {value!r}")
    if "\r" in (value or "") or "\n" in (value or ""):
        raise SendError(f"{what} must be a single line")
    return value.strip()


def clean_recipients(recipients: Iterable[str]) -> tuple[str, ...]:
    out = tuple(_address(r, "recipient") for r in recipients)
    if not out:
        raise SendError("no recipients")
    return out


def build_message(out: Outgoing, *, now: datetime | None = None) -> EmailMessage:
    """RFC 5322 message with UTF-8 text body. Header injection is refused, not escaped."""
    msg = EmailMessage()
    sender = _address(out.sender, "sender")
    msg["From"] = sender
    msg["To"] = ", ".join(clean_recipients(out.recipients))
    if out.reply_to:
        msg["Reply-To"] = _address(out.reply_to, "reply-to")
    msg["Subject"] = _single_line(out.subject, "subject")
    stamp = now or datetime.now(timezone.utc)
    msg["Date"] = formatdate(stamp.timestamp(), usegmt=True)
    domain = parseaddr(sender)[1].rsplit("@", 1)[-1]
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["X-Cash-Desk-Draft"] = f"{_single_line(out.draft_id, 'draft id')} {_single_line(out.draft_hash, 'draft hash')}"
    msg["Content-Language"] = out.language or "en"
    if not (out.body or "").strip():
        raise SendError("empty body")
    msg.set_content(out.body, charset="utf-8")
    return msg


_UNSAFE_NAME = re.compile(r"[^\w .,()&'-]+", re.UNICODE)


def eml_filename(out: Outgoing) -> str:
    """A name people can find the file by (date, customer, kind, invoice); the draft id and hash
    are inside the message, in its X-Cash-Desk-Draft header."""
    if out.file_label.strip():
        return f"{_UNSAFE_NAME.sub('_', out.file_label.strip())[:150].rstrip(' .')}.eml"
    return f"{_SAFE_ID.sub('_', out.draft_id)[:64]}-{out.draft_hash[:12]}.eml"


def write_eml(out: Outgoing, outbox: Path) -> SendResult:
    """Write the message as <outbox>/<file label>.eml (see eml_filename). Never overwrites: a second
    write of the same approved text is refused, so a retry cannot silently duplicate."""
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    target = outbox / eml_filename(out)
    if target.exists():
        raise SendError(f"already written: {target.name}")
    data = build_message(out).as_bytes()
    fd, tmp = tempfile.mkstemp(dir=str(outbox), prefix=".tmp-", suffix=".eml")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, target)  # fails if the target appeared meanwhile
        except FileExistsError as exc:
            raise SendError(f"already written: {target.name}") from exc
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return SendResult(ok=True, mode="eml", detail=f"written to {target}", path=str(target))


def smtp_tls_context(ca_file: str = "") -> ssl.SSLContext:
    """The TLS context for the SMTP connection. Verification is always on.

    Without ``ca_file``: the default context (the system's trusted certificates). With it (a
    company or test mail server that uses its own certificate): a context that requires a
    valid certificate for the host name and trusts exactly that file. Hermes installs
    truststore, which swaps ``ssl.SSLContext`` for one that only consults the system store and
    ignores ``load_verify_locations``; so the context is built from the standard-library class
    truststore keeps aside, otherwise the named file would silently not count.
    """
    if not ca_file:
        return ssl.create_default_context()
    path = Path(ca_file).expanduser()
    if not path.is_file():
        raise SendError(f"smtp_ca_file does not exist: {ca_file}")
    api = sys.modules.get("truststore._api")
    cls = getattr(api, "_original_SSLContext", None) or ssl.SSLContext
    # PROTOCOL_TLS_CLIENT means CERT_REQUIRED and check_hostname=True, and OpenSSL 3 refuses
    # anything below TLS 1.2. No property is set here on purpose: the stdlib setters look up the
    # module-level ssl.SSLContext, which truststore has replaced, and recurse.
    context = cls(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(path))
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:  # pragma: no cover - defensive
        raise SendError("could not build a verifying TLS context for smtp_ca_file")
    return context


def send_smtp(out: Outgoing, *, host: str, port: int, user: str, password: str,
              timeout: float = 30.0, ca_file: str = "") -> SendResult:
    """Deliver through the user's own SMTP server: implicit TLS on 465, STARTTLS otherwise.
    Credentials are used once and never appear in results, logs or exceptions."""
    if not host:
        raise SendError("SMTP host is not configured")
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise SendError(f"invalid SMTP port: {port!r}") from exc
    msg = build_message(out)
    context = smtp_tls_context(ca_file)
    client = None
    try:
        if port == 465:
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if user:
            client.login(user, password or "")
        refused = client.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise SendError(f"SMTP authentication failed ({exc.smtp_code})") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(f"SMTP failed: {exc.__class__.__name__}") from None
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:  # pragma: no cover - best effort
                pass
    if refused and len(refused) >= len(out.recipients):
        raise SendError(f"{len(refused)} recipient(s) refused by the server")
    if refused:
        # The server took the message for the other recipients: it HAS gone out. Recording this as
        # failed would let a retry mail them twice, so it counts as sent and names who was refused.
        return SendResult(ok=True, mode="smtp", detail=f"sent via {host}:{port} to {len(out.recipients) - len(refused)} of "
                          f"{len(out.recipients)} recipients; refused (not retried): {', '.join(sorted(refused))}",
                          message_id=str(msg["Message-ID"]))
    return SendResult(ok=True, mode="smtp", detail=f"sent via {host}:{port}",
                      message_id=str(msg["Message-ID"]))
