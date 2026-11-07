"""A planning hold must also stop text that was drafted or approved earlier."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.plugin.support import ROOT

DAY = "2026-11-10"
HEADER = "invoice_no,customer_id,issue_date,due_date,amount,currency,balance,status,reference,iban,type\n"
ROW = "INV-2026-1001,C1,2026-10-01,2026-10-27,900,EUR,900,open,20261001,DE89370400440532013000,invoice\n"
OTHER = "INV-2026-2001,C2,2026-10-01,2026-10-27,800,EUR,800,open,20262001,DE89370400440532013000,invoice\n"


def import_file(ctx, home, name, text, kind="invoices", **kwargs):
    folder = home / "plugin-data" / "cash-desk"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    result = ctx.call("cashdesk_import", kind=kind, path=str(path), **kwargs)
    assert result["ok"], result
    return result


def seed(ctx, home):
    import_file(ctx, home, "customers.csv",
                "customer_id,name,email,contact_name\nC1,Example Client Ltd,ap@client.example,Ada Example\n"
                "C2,Other Client Ltd,ap@other.example,Alex Other\n", kind="customers")
    import_file(ctx, home, "invoices.csv", HEADER + ROW + OTHER)
    plan = ctx.call("cashdesk_draft", as_of=DAY)
    assert plan["ok"], plan
    return next(d for d in plan["drafts"] if d["customer_id"] == "C1")


def add_hold(ctx, home, kind):
    if kind == "credit":
        import_file(ctx, home, "credit.csv", HEADER +
                    "CN-2026-1001,C1,2026-10-01,2026-10-27,-300,EUR,-300,open,,,credit note\n", open_list=False)
    elif kind == "promise":
        assert ctx.call("cashdesk_reply", customer_id="C1", kind="promise",
                        text="We will pay on 20 November.", promised_date="2026-11-20")["ok"]
    elif kind == "review":
        import_file(ctx, home, "partial.csv", "id,date,amount,currency,counterparty,reference\n"
                    "PARTIAL-1,2026-11-09,400,EUR,EXAMPLE CLIENT LTD,INV-2026-1001\n", kind="bank")
    else:
        assert kind == "missing"
        import_file(ctx, home, "latest-open.csv", HEADER + OTHER, open_list=True)


@pytest.mark.parametrize("kind", ["credit", "promise", "review", "missing"])
@pytest.mark.parametrize("already_approved", [False, True])
def test_new_hold_blocks_existing_draft_and_clears_approval(plugin, hermes_fakes, kind, already_approved):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    if already_approved:
        assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    add_hold(ctx, hermes_fakes, kind)
    # No new draft run is needed to revoke the obsolete approval.
    with module._store() as store:
        assert store.approval(draft["id"]) is None
        assert store.get_draft(draft["id"]).status == "held"
        with pytest.raises(module._engine().ConflictError, match="reminder is paused"):
            store.approve_draft(draft["id"], draft["hash"])
        assert store.approval(draft["id"]) is None, "refusal must not roll the revocation back"
    refused = ctx.command(f"approve {draft['id']} {draft['hash'][:12]}")
    assert refused.startswith("Refused:") and "reminder is paused" in refused
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not sent["ok"] and "reminder is paused" in sent["error"]
    assert "cashdesk_draft" in sent["error"]
    with module._store() as store:
        assert store.list_sends() == []
    assert not list(hermes_fakes.rglob("*.eml"))
    held = ctx.call("cashdesk_draft", as_of=DAY)["held"]
    assert any("INV-2026-1001" in item["invoice_numbers"] for item in held)
    # A hold on C1 must not revoke the unrelated C2 reminder.
    other = next(d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C2")
    assert ctx.command(f"approve {other['id']} {other['hash'][:12]}").startswith("Approved")


@pytest.mark.parametrize("field", ["iban", "reference"])
@pytest.mark.parametrize("already_approved", [False, True])
def test_changed_payment_instructions_need_redraft_and_fresh_approval(plugin, hermes_fakes, field, already_approved):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    if already_approved:
        assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    changed = ROW.replace("DE89370400440532013000", "GB82WEST12345698765432") if field == "iban" else ROW.replace("20261001", "20261999")
    import_file(ctx, hermes_fakes, "new-instructions.csv", HEADER + changed + OTHER)
    with module._store() as store:
        assert store.approval(draft["id"]) is None
    refused = ctx.command(f"approve {draft['id']} {draft['hash'][:12]}")
    assert refused.startswith("Refused:") and "payment instructions changed" in refused
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not sent["ok"] and "payment instructions changed" in sent["error"]
    assert not list(hermes_fakes.rglob("*.eml"))
    plan = ctx.call("cashdesk_draft", as_of=DAY)
    fresh = next(d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C1")
    assert fresh["hash"] != draft["hash"]
    assert any(item["draft_id"] == draft["id"] for item in plan["superseded"])
    assert not ctx.call("cashdesk_send", draft_id=fresh["id"], draft_hash=fresh["hash"])["ok"]
    assert ctx.command(f"approve {fresh['id']} {fresh['hash'][:12]}").startswith("Approved")
    assert ctx.call("cashdesk_send", draft_id=fresh["id"], draft_hash=fresh["hash"])["ok"]
    assert len(list(hermes_fakes.rglob("*.eml"))) == 1


@pytest.mark.parametrize("kind", ["credit", "promise", "review", "missing"])
def test_resolving_a_hold_needs_redrafting_and_never_restores_approval(plugin, hermes_fakes, monkeypatch, kind):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    add_hold(ctx, hermes_fakes, kind)
    if kind == "credit":
        # Model a settled ledger credit directly: importing that transition is P1d.
        with module._store() as store:
            store.upsert_invoices([replace(store.get_invoice("CN-2026-1001"), balance=0, status="paid")])
    elif kind == "promise":
        monkeypatch.setenv("CASHDESK_TODAY", "2026-11-23")
    elif kind == "review":
        assert ctx.command("dismiss PARTIAL-1 duplicate bank line").startswith("Dismissed")
    else:
        import_file(ctx, hermes_fakes, "listed-again.csv", HEADER + OTHER + ROW)
    with module._store() as store:
        assert store.approval(draft["id"]) is None
    # Even if the old text remains numerically true, it must be redrafted and reread.
    assert not ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    plan = ctx.call("cashdesk_draft")
    assert plan["ok"], plan
    fresh = next(d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C1")
    assert not ctx.call("cashdesk_send", draft_id=fresh["id"], draft_hash=fresh["hash"])["ok"]
    assert ctx.command(f"approve {fresh['id']} {fresh['hash'][:12]}").startswith("Approved")
    assert ctx.call("cashdesk_send", draft_id=fresh["id"], draft_hash=fresh["hash"])["ok"]


def test_existing_ledger_drafts_require_fresh_payment_instruction_basis(plugin, hermes_fakes):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    db = hermes_fakes / "plugin-data" / "cash-desk" / "cashdesk.sqlite"
    with sqlite3.connect(db) as conn:
        # Preserve the fixture's full table, then reproduce the released five-column schema.
        conn.execute("ALTER TABLE draft_basis RENAME TO basis_before_upgrade_fixture")
        conn.execute("CREATE TABLE draft_basis (draft_id TEXT NOT NULL, invoice_number TEXT NOT NULL, balance TEXT NOT NULL, currency TEXT NOT NULL, due_date TEXT NOT NULL, PRIMARY KEY (draft_id, invoice_number))")
        conn.execute("INSERT INTO draft_basis SELECT draft_id,invoice_number,balance,currency,due_date FROM basis_before_upgrade_fixture")
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not sent["ok"] and "payment instructions were not recorded" in sent["error"]
    with module._store() as store:
        assert store.approval(draft["id"]) is None
    assert not list(hermes_fakes.rglob("*.eml"))
    assert ctx.call("cashdesk_draft")["ok"]
    fresh = next(d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C1")
    assert not ctx.call("cashdesk_send", draft_id=fresh["id"], draft_hash=fresh["hash"])["ok"]
    assert ctx.command(f"approve {fresh['id']} {fresh['hash'][:12]}").startswith("Approved")
    assert ctx.call("cashdesk_send", draft_id=fresh["id"], draft_hash=fresh["hash"])["ok"]


def test_same_current_settings_gate_desktop_and_command_approvals(plugin, hermes_fakes, monkeypatch):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    ctx.config["ignore_balances_below"] = "1000"
    monkeypatch.setattr(sys.modules["hermes_cli.config"], "load_config_readonly", lambda: {
        "approvals": {"mode": "manual"}, "plugins": {"entries": {"cash-desk": {"settings": ctx.config}}}})
    spec = importlib.util.spec_from_file_location("eligibility_api_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.post(f"/drafts/{draft['id']}/approve", json={"hash": draft["hash"]})
        assert response.status_code == 409 and "reminder is paused" in response.json()["detail"]
        assert "ignore_balances_below" in response.json()["detail"]
        assert "reminder is paused" in ctx.command(f"approve {draft['id']} {draft['hash'][:12]}")
        ctx.config["ignore_balances_below"] = 0
        assert ctx.call("cashdesk_draft")["ok"]
        fresh = next(d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C1")
        response = client.post(f"/drafts/{fresh['id']}/approve", json={"hash": fresh["hash"]})
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("operation", ["approve", "reserve", "record"])
def test_refused_store_operations_commit_the_revocation(plugin, hermes_fakes, operation):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    with module._store() as store:
        # A competing writer bypasses normal import reconciliation. The gate must
        # detect it, and raising the refusal must not roll back the revocation.
        store._conn.execute("UPDATE invoices SET reference = '20261999' WHERE number = 'INV-2026-1001'")
        with pytest.raises(module._engine().ConflictError, match="payment instructions changed"):
            if operation == "approve":
                store.approve_draft(draft["id"], draft["hash"])
            elif operation == "reserve":
                store.reserve_send(draft["id"], draft["hash"], "eml")
            else:
                store.record_send(draft["id"], draft["hash"], "eml")
        assert store.approval(draft["id"]) is None
        assert store.list_sends() == []


def test_reassigned_invoice_cannot_use_the_previous_customers_approval(plugin, hermes_fakes):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    path=hermes_fakes / "plugin-data" / "cash-desk" / "reassigned.csv"
    path.write_text(HEADER + ROW.replace(",C1,", ",C2,") + OTHER)
    imported=ctx.call("cashdesk_import",kind="invoices",path=str(path))
    assert imported["quarantined"] and not imported["ok"]
    result = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not result["ok"] and "customer identity review" in result["error"]
    with module._store() as store:
        assert store.approval(draft["id"]) is None
    assert not list(hermes_fakes.rglob("*.eml"))


def test_planning_and_its_invoice_basis_share_one_ledger_snapshot(plugin, hermes_fakes, monkeypatch):
    module, ctx = plugin
    seed(ctx, hermes_fakes)
    db = hermes_fakes / "plugin-data" / "cash-desk" / "cashdesk.sqlite"
    original = module._engine().plan_followups
    attempts = []

    def competing_update(*args, **kwargs):
        with sqlite3.connect(db, timeout=0, isolation_level=None) as writer:
            try:
                writer.execute("UPDATE invoices SET balance = '400.00' WHERE number = 'INV-2026-1001'")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                attempts.append("blocked until the draft is stored")
            else:
                attempts.append("changed while planning")
        return original(*args, **kwargs)

    monkeypatch.setattr(module._engine(), "plan_followups", competing_update)
    result = ctx.call("cashdesk_draft", as_of=DAY)
    assert result["ok"], result
    assert attempts == ["blocked until the draft is stored"]
    with module._store() as store:
        assert store.get_invoice("INV-2026-1001").balance == 900


def test_invalidated_reminder_stays_visible_with_a_next_action(plugin, hermes_fakes):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    add_hold(ctx, hermes_fakes, "promise")
    brief = ctx.call("cashdesk_brief")["brief"]
    assert "Example Client Ltd" in brief and "DO NOT APPROVE" in brief
    spec = importlib.util.spec_from_file_location("invalidation_activity_test", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        lines = [e["text"] for e in client.get("/events").json()["events"]]
    line = next(line for line in lines if line.startswith("Paused Reminder 14 for Example Client Ltd"))
    assert "promised to pay by" in line and "Draft again" in line
    assert draft["hash"] not in line and draft["id"] not in line


def test_rewording_paused_text_cannot_clear_the_hold_or_break_redrafting(plugin, hermes_fakes):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    add_hold(ctx, hermes_fakes, "missing")
    revised = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=draft["body"].replace("Hi Ada,", "Hello Ada,"))
    assert revised["ok"], revised
    assert revised["draft"]["status"] == "held"
    assert "paused" in revised["message"] and "cashdesk_draft" in revised["message"]
    assert "reminder is paused" in ctx.command(f"approve {draft['id']} {revised['draft']['hash'][:12]}")
    import_file(ctx, hermes_fakes, "listed-again.csv", HEADER + OTHER + ROW)
    assert ctx.call("cashdesk_draft")["ok"]
    fresh = next(d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C1")
    assert fresh["id"] == draft["id"]
    with module._store() as store:
        assert store.approval(fresh["id"]) is None
    assert ctx.command(f"approve {fresh['id']} {fresh['hash'][:12]}").startswith("Approved")


def test_exact_receipt_waiting_for_matching_blocks_an_old_approval(plugin, hermes_fakes):
    module, ctx = plugin
    draft = seed(ctx, hermes_fakes)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    import_file(ctx, hermes_fakes, "exact-not-yet-matched.csv",
                "id,date,amount,currency,counterparty,reference\n"
                "EXACT-1,2026-11-09,900,EUR,EXAMPLE CLIENT LTD,INV-2026-1001\n", kind="bank")
    with module._store() as store:
        assert store.approval(draft["id"]) is None
        assert store.get_invoice("INV-2026-1001").balance == 900, "eligibility must not silently apply money"
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not sent["ok"] and "run cashdesk_match before chasing" in sent["error"]
    assert not list(hermes_fakes.rglob("*.eml"))
    assert not any(d["customer_id"] == "C1" for d in ctx.call("cashdesk_draft")["drafts"])
    applied = ctx.call("cashdesk_match")["applied"]
    assert [item["payment_id"] for item in applied] == ["EXACT-1"]
    with module._store() as store:
        assert store.get_invoice("INV-2026-1001").is_paid
        assert store.approval(draft["id"]) is None
    assert not ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])["ok"]
    plan = ctx.call("cashdesk_draft")
    assert not any(d["customer_id"] == "C1" and d["kind"].startswith("stage") for d in plan["drafts"])
