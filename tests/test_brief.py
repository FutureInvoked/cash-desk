"""brief.py: the terse morning brief in English and Czech, read-only."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from cashdesk.approval import draft_hash
from cashdesk.brief import brief_window, morning_brief
from cashdesk.importers import read_bank_csv, read_customers, read_invoices
from cashdesk.model import Draft, Match
from cashdesk.store import Store

D = Decimal
NBSP = " "
AS_OF = dt.date(2026, 11, 10)  # a Tuesday
NOW = dt.datetime(2026, 11, 10, 6, 43, tzinfo=dt.timezone.utc)

INVOICES_CSV = """invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference,iban
INV-1,C1,2026-10-01,2026-10-20,1250.00,EUR,1250.00,open,20261,DE89370400440532013000
INV-2,C1,2026-09-01,2026-09-20,500.00,EUR,500.00,open,20262,DE89370400440532013000
INV-3,C2,2026-10-20,2026-11-20,48400.00,CZK,48400.00,open,20263,CZ6508000000192000145399
INV-4,C2,2026-07-01,2026-07-20,1000.00,CZK,1000.00,open,20264,CZ6508000000192000145399
INV-5,C3,2026-10-01,2026-10-15,600.00,EUR,600.00,disputed,20265,DE89370400440532013000
INV-7,C3,2026-10-25,2026-11-08,300.00,EUR,300.00,open,20267,DE89370400440532013000
"""
CUSTOMERS_CSV = """customer_id,name,language,email,contact_name,on_hold,hold_reason
C1,Northgate Sample Studio Ltd,en,ann@northgate.example,Ann Example,,
C2,Modrá Dílna Demo s.r.o.,cs,petr@modra.example,Petr Ukázka,,
C3,Oakridge Placeholder Partners LLP,en,lee@oakridge.example,Lee Placeholder,,
"""
BANK_CSV = """id,date,amount,currency,counterparty,reference,memo
B1,2026-11-06,1250.00,EUR,NORTHGATE,INV-1,
B5,2026-11-09,1000.00,CZK,MODRA DILNA,INV-4,
"""


def draft(did, cid, numbers, kind="stage14"):
    body, subject, to = f"Body for {', '.join(numbers)}", f"Reminder {did}", (f"{cid.lower()}@example.test",)
    return Draft(did, kind, cid, tuple(numbers), "en", subject, body, to, NOW, "pending", draft_hash(body, to, subject))


def make_store() -> Store:
    s = Store(":memory:")
    s.ingest(read_customers(CUSTOMERS_CSV), at=NOW - dt.timedelta(days=1))
    s.ingest(read_invoices("generic", INVOICES_CSV), at=NOW - dt.timedelta(days=1))
    s.ingest(read_bank_csv(BANK_CSV), at=NOW)
    s.apply_match(Match("B1", "INV-1", D("1250.00"), "EUR", "reference+exact", "high"), at=NOW)
    s.apply_match(Match("B5", "INV-4", D("1000.00"), "CZK", "reference+exact", "high"), at=NOW)
    s.create_drafts([draft("d1", "C1", ("INV-1",)), draft("d2", "C1", ("INV-1", "INV-2")), draft("d3", "C2", ("INV-3",), "stage21")], at=NOW)
    s.set_hold("C2", "dispute in reply", at=NOW)
    return s


def test_brief_window_from_the_previous_business_day():
    assert brief_window(dt.date(2026, 10, 19)) == (dt.date(2026, 10, 16), dt.date(2026, 10, 18)), "Monday covers Fri-Sun"
    assert brief_window(dt.date(2026, 10, 20)) == (dt.date(2026, 10, 19), dt.date(2026, 10, 19)), "Tuesday covers Monday"
    since = dt.datetime(2026, 10, 14, 7, tzinfo=dt.timezone.utc)
    assert brief_window(dt.date(2026, 10, 19), since) == (dt.date(2026, 10, 14), dt.date(2026, 10, 18)), "since the previous brief"


def test_english_brief_sections():
    store = make_store()
    text = morning_brief(store, AS_OF, "en")
    assert text.startswith("# Morning cash & AR brief - 2026-11-10\n")
    assert "Covers 2026-11-09 to 2026-11-09" in text and "Nothing has been sent" in text
    hold = text.split("## HOLD")[1].split("##")[0]
    assert "Modrá Dílna Demo s.r.o." in hold and "dispute in reply" in hold and "Oakridge Placeholder Partners LLP" in hold and "INV-5" in hold
    pay = text.split("## Payments applied (not yet in a brief)")[1].split("##")[0]
    assert "- 2026-11-06 INV-1 Northgate Sample Studio Ltd: EUR 1,250.00 (B1)" in pay
    assert "- 2026-11-09 INV-4 Modrá Dílna Demo s.r.o.: CZK 1,000.00 (B5)" in pay
    assert "Total per currency: CZK 1,000.00 + EUR 1,250.00" in pay and "2,250" not in text
    overdue = text.split("## Newly overdue")[1].split("##")[0]
    assert "- INV-7 Oakridge Placeholder Partners LLP: EUR 300.00, 2 days overdue - HOLD" in overdue
    assert "INV-2" not in overdue, "overdue for weeks is not newly overdue"
    queue = text.split("## Awaiting approval (3)")[1].split("##")[0]
    rows = [r for r in queue.splitlines() if r.startswith("- ")]
    assert rows[0].startswith("- Reminder 14 · Northgate Sample Studio Ltd: INV-1 - ") and "STALE: invoice now paid - do not send" in rows[0]
    assert rows[1].startswith("- Reminder 14 · Northgate Sample Studio Ltd: INV-1, INV-2 - ") and "STALE IN PART: INV-1 now paid - redraft before approving" in rows[1]
    assert rows[2].startswith("- Reminder 21 · ") and "DO NOT APPROVE" in rows[2]
    assert not any(did in queue for did in ("d1 ", "d2 ", "d3 ")), "draft ids are not shown to people in the brief"
    open_section = text.split("## Open per currency")[1]
    assert "CZK 48,400.00" in open_section and "EUR 1,400.00" in open_section


def test_czech_brief_marks_holds_in_czech_and_never_sums_currencies():
    text = morning_brief(make_store(), AS_OF, "cs")
    assert text.startswith("# Ranní přehled hotovosti a pohledávek - 10. 11. 2026\n")
    assert "Období 9. 11. 2026 až 9. 11. 2026" in text and "nic nebylo odesláno" in text
    assert f"1{NBSP}250,00{NBSP}EUR" in text and f"1{NBSP}000,00{NBSP}Kč + 1{NBSP}250,00{NBSP}EUR" in text
    assert "2 dny po splatnosti - POZASTAVENO" in text and " - HOLD" not in text
    assert "NESCHVALOVAT" in text and "NEAKTUÁLNÍ: faktura už je zaplacena" in text and "ČÁSTEČNĚ NEAKTUÁLNÍ: INV-1" in text
    assert "## Spárované platby (dosud v žádném přehledu)" in text and "## Nově po splatnosti" in text and "## Čeká na schválení (3)" in text


def test_brief_only_reads_and_accepts_a_snapshot():
    store = make_store()
    before = len(store.events())
    snapshot = store.brief_snapshot(AS_OF)
    assert morning_brief(snapshot, AS_OF, "en") == morning_brief(store, AS_OF, "en")
    assert len(store.events()) == before and store.previous_brief_at() is None


def test_second_brief_covers_only_what_happened_since_the_first():
    store = make_store()
    store.record_brief(NOW)
    later = morning_brief(store, AS_OF + dt.timedelta(days=1), "en")
    assert "## Payments applied since the previous brief\n- none" in later
    assert "Covers 2026-11-10 to 2026-11-10 (since the previous brief" in later


def test_empty_store_brief_has_every_section_with_none():
    text = morning_brief(Store(":memory:"), AS_OF, "en")
    for section in ("## Payments applied (not yet in a brief)", "## Newly overdue", "## Awaiting approval (0)", "## Open per currency"):
        assert section in text
    assert text.count("- none") >= 3 and "## HOLD" not in text


def test_the_payments_heading_says_it_lists_payments_applied_not_the_window():
    """A first brief covers 9 Nov for newly overdue invoices but lists a payment dated 6 Nov:
    the heading must say these are payments applied since the last brief, so the two agree."""
    store = make_store()
    first = morning_brief(store, AS_OF, "en")
    assert "## Payments applied (not yet in a brief)" in first and "## New payments" not in first
    assert "## Spárované platby (dosud v žádném přehledu)" in morning_brief(store, AS_OF, "cs")
    store.record_brief(NOW)
    later = morning_brief(store, AS_OF + dt.timedelta(days=1), "en")
    assert "## Payments applied since the previous brief\n- none" in later
