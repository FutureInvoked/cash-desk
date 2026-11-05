"""The Desktop backend over the same ledger the tools fill."""

from __future__ import annotations

import importlib.util
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.plugin.support import AS_OF, ROOT, load_demo


@pytest.fixture
def client(plugin):
    name = "cashdesk_api_under_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router)
        yield TestClient(app), plugin[1]
    finally:
        sys.modules.pop(name, None)


def test_empty_ledger_status(client):
    api, ctx = client
    status = api.get("/status").json()
    assert status["ledger"] == "empty" and status["engine"] is True and status["chip"] == "Cash Desk"
    assert api.get("/drafts").json()["drafts"] == []
    assert api.get("/holds").json()["customers"] == []
    assert api.get("/events").json()["events"] == []


def test_queue_approve_hold_release(client):
    api, ctx = client
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    status = api.get("/status").json()
    assert status["ledger"] == "ok" and status["awaiting_approval"] >= 5
    assert "to approve" in status["chip"] and "overdue" in status["chip"]
    assert set(status["open_by_bucket_display"]) >= {"EUR"}
    for cur, buckets in status["open_by_bucket_display"].items():
        assert all(isinstance(v, str) and cur in v for v in buckets.values())
    drafts = api.get("/drafts").json()["drafts"]
    draft = next(d for d in drafts if d["kind"] == "stage14")
    assert draft["body"] and draft["figures"] and draft["customer"]
    assert draft["label"] == "Reminder 14" and {d["label"] for d in drafts} >= {"Reminder 7", "Thank-you", "Escalation"}
    assert api.post(f"/drafts/{draft['id']}/approve", json={}).status_code == 400
    assert api.post(f"/drafts/{draft['id']}/approve", json={"hash": "0" * 64}).status_code == 409
    assert api.post("/drafts/nope/approve", json={"hash": "0" * 64}).status_code == 404
    ok = api.post(f"/drafts/{draft['id']}/approve", json={"hash": draft["hash"]})
    assert ok.status_code == 200 and ok.json()["draft"]["status"] == "approved"
    assert api.get("/drafts?status=approved").json()["drafts"][0]["id"] == draft["id"]
    other = next(d for d in drafts if d["kind"] == "stage07")
    held = api.post(f"/drafts/{other['id']}/hold", json={"reason": "call first"})
    assert held.status_code == 200 and held.json()["draft"]["status"] == "held"
    holds = api.get("/holds").json()["customers"]
    assert [c["id"] for c in holds if c["releasable"]] == [other["customer_id"]]
    assert api.post(f"/customers/{other['customer_id']}/release").status_code == 200
    # Only the demo's disputed customer (Oakridge, C006) remains; a dispute is not released here.
    assert [(c["id"], c["releasable"]) for c in api.get("/holds").json()["customers"]] == [("C006", False)]
    assert api.post("/customers/nobody/hold", json={}).status_code == 404
    rejected = api.post(f"/drafts/{other['id']}/reject", json={"reason": "tone"})
    assert rejected.status_code == 200 and rejected.json()["draft"]["status"] == "rejected"
    kinds = [e["kind"] for e in api.get("/events?limit=50").json()["events"]]
    assert {"approve", "hold", "unhold", "reject"} <= set(kinds)


def test_load_demo_fills_an_empty_ledger_once(client):
    """The Desktop's "Load demo data" button: bundled demo CSVs in, payments matched as of the demo day."""
    api, ctx = client
    first = api.post("/demo")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["ok"] is True and body["as_of"] == AS_OF
    assert body["imported"] == {"customers": 8, "invoices": 11, "payments": 3}
    assert sorted(m["payment_id"] for m in body["applied"]) == ["B1-01", "B2-01"]
    assert body["review"] == 1
    assert api.get("/status").json()["ledger"] == "ok"
    assert api.get("/drafts").json()["drafts"] == []  # drafting stays with the agent (it needs sender_name)
    again = api.post("/demo")
    assert again.status_code == 409 and "not empty" in again.json()["detail"]


def test_load_demo_refuses_a_ledger_that_already_has_data(client):
    api, ctx = client
    ctx.call("cashdesk_import", kind="customers", path="demo/customers.csv")
    refused = api.post("/demo")
    assert refused.status_code == 409 and "the ledger is not empty" in refused.json()["detail"]
    # Nothing else was imported by the refused call: the invoices file is still new.
    assert ctx.call("cashdesk_import", kind="invoices", path="demo/invoices.csv")["ok"]


def test_holds_list_agrees_with_the_on_hold_count(client):
    """A customer with an open disputed invoice counts as on hold in the status, so the
    On hold list shows it too (as a dispute, which Release does not clear)."""
    api, ctx = client
    assert api.post("/demo").status_code == 200
    status = api.get("/status").json()
    customers = api.get("/holds").json()["customers"]
    assert len(customers) == status["on_hold"] == 1
    (oak,) = customers
    assert oak["id"] == "C006" and oak["dispute"] is True and oak["releasable"] is False
    assert "INV-2026-0033" in oak["hold_reason"]
    ctx.call("cashdesk_hold", customer_id="C001", reason="call first")
    customers = {c["id"]: c for c in api.get("/holds").json()["customers"]}
    assert set(customers) == {"C001", "C006"} == set(customers) and api.get("/status").json()["on_hold"] == 2
    assert customers["C001"]["releasable"] is True and customers["C001"]["hold_reason"] == "call first"


def test_review_lists_payments_waiting_for_a_person_and_applies_nothing(client):
    api, ctx = client
    assert api.post("/demo").status_code == 200
    matches_before = ctx.call("cashdesk_status")
    body = api.get("/review").json()
    assert body["engine"] is True and body["as_of"] >= "2026-11-06"
    (item,) = body["payments"]
    assert item["id"] == "B1-02" and item["amount_display"].endswith("700.00") and "BIRCHFIELD" in item["counterparty"]
    assert item["why"] and item["candidates"][0]["invoice_number"] == "INV-2026-0044"
    assert item["candidates"][0]["customer"] == "Birchfield Example Consulting GmbH"
    assert ctx.call("cashdesk_status") == matches_before  # nothing was applied


def test_review_on_an_empty_ledger(client):
    api, ctx = client
    assert api.get("/review").json()["payments"] == []


def test_load_demo_uses_the_currency_setting(client, monkeypatch):
    import sys
    api, ctx = client
    monkeypatch.setattr(sys.modules["hermes_cli.config"], "load_config_readonly",
                        lambda: {"plugins": {"entries": {"cash-desk": {"settings": {"currency": "USD"}}}}})
    assert api.post("/demo").status_code == 200
    assert set(api.get("/status").json()["open_by_bucket_display"]) == {"USD"}


def test_activity_reads_as_plain_sentences(client):
    """Owner: the import lines with long numbers/letters are confusing. Activity names files,
    customers and figures, never fingerprints or draft ids."""
    import re
    api, ctx = client
    assert api.post("/demo").status_code == 200
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = next(d for d in api.get("/drafts").json()["drafts"] if d["kind"] == "stage07")
    assert api.post(f"/drafts/{draft['id']}/approve", json={"hash": draft["hash"]}).status_code == 200
    texts = [e["text"] for e in api.get("/events?limit=50").json()["events"]]
    joined = "\n".join(texts)
    assert "Imported customers.csv · 8 customers" in joined and "Imported invoices.csv · 11 invoices" in joined
    assert "Imported bank_day1.csv · 2 bank lines" in joined
    assert "Matched payment B1-01 to INV-2026-0045 · EUR 1,980.00" in joined
    assert "Drafted Reminder 7 for Northgate Sample Studio Ltd" in joined
    assert "Approved Reminder 7 for Northgate Sample Studio Ltd on the Desktop" in joined
    assert not re.search(r"[0-9a-f]{16,}", joined), joined
    assert not any("-nov10" in t for t in texts), "draft ids are not shown to people"
