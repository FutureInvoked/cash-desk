"""Tests for the plugin-side delivery module. Nothing here touches the network."""

import smtplib
from pathlib import Path

import pytest

import sending  # repository root is on sys.path via pytest.ini


def outgoing(**over):
    base = dict(draft_id="d-0001", draft_hash="a" * 64, subject="Reminder: invoice 2026-0042",
                body="Dear customer,\n\nInvoice 2026-0042 for EUR 1,250.00 was due on 2026-10-22.\n",
                recipients=("billing@example-customer.test",), sender="desk@example-studio.test",
                language="en")
    base.update(over)
    return sending.Outgoing(**base)


def test_build_message_headers_and_body():
    msg = sending.build_message(outgoing(reply_to="replies@example-studio.test"))
    assert msg["From"] == "desk@example-studio.test"
    assert msg["To"] == "billing@example-customer.test"
    assert msg["Reply-To"] == "replies@example-studio.test"
    assert msg["Subject"] == "Reminder: invoice 2026-0042"
    assert msg["X-Cash-Desk-Draft"] == "d-0001 " + "a" * 64
    assert msg["Message-ID"].endswith("@example-studio.test>")
    assert "EUR 1,250.00" in msg.get_content()


@pytest.mark.parametrize("field,value", [
    ("subject", "line one\r\nBcc: evil@example.test"),
    ("subject", ""),
    ("body", "   "),
    ("sender", "not-an-address"),
    ("recipients", ("ok@example.test", "bad address@example.test")),
    ("recipients", ()),
    ("reply_to", "x@example.test\nBcc: y@example.test"),
])
def test_bad_fields_are_refused(field, value):
    with pytest.raises(sending.SendError):
        sending.build_message(outgoing(**{field: value}))


def test_write_eml_once_only(tmp_path):
    out = outgoing()
    result = sending.write_eml(out, tmp_path / "outbox")
    path = Path(result.path)
    assert result.ok and result.mode == "eml" and path.exists()
    assert path.name == "d-0001-" + "a" * 12 + ".eml"
    text = path.read_bytes().decode("utf-8")
    assert "Subject: Reminder: invoice 2026-0042" in text and "EUR 1,250.00" in text
    assert not list((tmp_path / "outbox").glob(".tmp-*"))
    with pytest.raises(sending.SendError, match="already written"):
        sending.write_eml(out, tmp_path / "outbox")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.refuse = {}
        self.auth_error = None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, "<redacted>"))
        self._password_seen = password
        if self.auth_error:
            raise self.auth_error

    def send_message(self, msg):
        self.calls.append(("send", msg["To"]))
        return self.refuse

    def quit(self):
        self.calls.append("quit")


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(sending.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(sending.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_send_smtp_starttls_path(fake_smtp):
    result = sending.send_smtp(outgoing(), host="smtp.example.test", port=587, user="u", password="pw")
    client = fake_smtp.instances[-1]
    assert result.ok and result.mode == "smtp" and result.message_id
    assert client.calls[:3] == ["ehlo", "starttls", "ehlo"]
    assert ("login", "u", "<redacted>") in client.calls and client.calls[-1] == "quit"
    assert "pw" not in result.detail


def test_send_smtp_implicit_tls_on_465(fake_smtp):
    sending.send_smtp(outgoing(), host="smtp.example.test", port=465, user="", password="")
    client = fake_smtp.instances[-1]
    assert "starttls" not in client.calls and not any(c[0] == "login" for c in client.calls if isinstance(c, tuple))


def test_send_smtp_refused_recipient_and_auth_failure_hide_secrets(fake_smtp, monkeypatch):
    def refusing(host, port, timeout=None, context=None):
        c = FakeSMTP(host, port, timeout, context)
        c.refuse = {"billing@example-customer.test": (550, b"no such user")}
        return c
    monkeypatch.setattr(sending.smtplib, "SMTP", refusing)
    with pytest.raises(sending.SendError, match="refused"):
        sending.send_smtp(outgoing(), host="smtp.example.test", port=587, user="u", password="s3cret")

    def failing_auth(host, port, timeout=None, context=None):
        c = FakeSMTP(host, port, timeout, context)
        c.auth_error = smtplib.SMTPAuthenticationError(535, b"bad credentials s3cret-echo")
        return c
    monkeypatch.setattr(sending.smtplib, "SMTP", failing_auth)
    with pytest.raises(sending.SendError) as info:
        sending.send_smtp(outgoing(), host="smtp.example.test", port=587, user="u", password="s3cret")
    assert "s3cret" not in str(info.value) and "535" in str(info.value)


def test_send_smtp_requires_host():
    with pytest.raises(sending.SendError, match="not configured"):
        sending.send_smtp(outgoing(), host="", port=587, user="", password="")


def test_eml_file_name_says_when_who_what_and_which_invoice(tmp_path):
    """Owner: a file in the outbox must be easy to find by date, customer, kind and invoice."""
    out = sending.Outgoing(draft_id="northgate-reminder7-0041-nov10", draft_hash="a" * 64, subject="s", body="b\n",
                           recipients=("ann@northgate.example",), sender="billing@example.invalid",
                           file_label="2026-11-05 Northgate Sample Studio Ltd - Reminder 7 - INV-2026-0041")
    assert sending.eml_filename(out) == "2026-11-05 Northgate Sample Studio Ltd - Reminder 7 - INV-2026-0041.eml"
    odd = sending.Outgoing(draft_id="x", draft_hash="b" * 64, subject="s", body="b\n", recipients=("a@b.example",),
                           sender="c@d.example", file_label="2026-11-05 A/B: Co*op - Reminder 7 - INV/7")
    assert sending.eml_filename(odd) == "2026-11-05 A_B_ Co_op - Reminder 7 - INV_7.eml"


def test_send_smtp_can_trust_a_named_certificate_but_never_skips_the_check(fake_smtp, monkeypatch, tmp_path):
    """smtp_ca_file: a company (or test) mail server with its own certificate. The certificate is
    still checked (required, hostname checked), only against that file; without it the default
    context (the system's trusted certificates) is used. A missing file is refused."""
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")
    loaded = []
    monkeypatch.setattr(sending.ssl.SSLContext, "load_verify_locations", lambda self, cafile=None, **kw: loaded.append(cafile))
    ctx = sending.smtp_tls_context(str(ca))
    assert loaded == [str(ca)]
    assert ctx.verify_mode == sending.ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= sending.ssl.TLSVersion.TLSv1_2
    assert isinstance(sending.smtp_tls_context(""), sending.ssl.SSLContext)
    with pytest.raises(sending.SendError, match="smtp_ca_file"):
        sending.smtp_tls_context(str(tmp_path / "missing.pem"))


def test_a_named_certificate_is_honoured_even_when_hermes_injected_truststore(monkeypatch, tmp_path):
    """Hermes calls truststore.inject_into_ssl(), which swaps ssl.SSLContext for one that checks
    only the macOS/Windows system store and ignores load_verify_locations. With smtp_ca_file set,
    Cash Desk builds the standard-library context truststore kept aside, so the file is honoured
    and verification stays on."""
    import sys
    import types

    original = sending.ssl.SSLContext

    class PlatformOnly(original):  # what truststore puts in place of ssl.SSLContext
        pass

    api = types.ModuleType("truststore._api")
    api._original_SSLContext = original
    monkeypatch.setitem(sys.modules, "truststore._api", api)
    monkeypatch.setattr(sending.ssl, "SSLContext", PlatformOnly)
    monkeypatch.setattr(original, "load_verify_locations", lambda self, cafile=None, **kw: None)
    ca = tmp_path / "ca.pem"
    ca.write_text("x", encoding="utf-8")
    ctx = sending.smtp_tls_context(str(ca))
    assert type(ctx) is original and ctx.verify_mode == sending.ssl.CERT_REQUIRED and ctx.check_hostname
