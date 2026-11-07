"""The Hermes half against the demo ledger: tools, gate, slash command, blueprint."""

from __future__ import annotations

import email
from pathlib import Path

import pytest
import yaml

from tests.plugin.support import AS_OF, DEMO, ROOT, load_demo


def manifest() -> dict:
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def test_registration_matches_the_manifest(plugin):
    module, ctx = plugin
    declared = manifest()
    assert sorted(ctx.tools) == sorted(declared["provides_tools"]) == sorted(module.TOOL_NAMES)
    assert list(ctx.hooks) == declared["provides_hooks"] == ["pre_tool_call", "subagent_start", "subagent_stop", "transform_llm_output"]
    assert "cashdesk" in ctx.commands
    (key, blueprint), = ctx.blueprints
    assert key == "morning-brief"
    assert {s["name"] for s in blueprint["slots"]} == {"time", "recurrence", "lang", "deliver"}
    assert "{minute}" in blueprint["schedule_template"] and "{dow}" in blueprint["schedule_template"]
    for name, tool in ctx.tools.items():
        schema = tool["schema"]
        assert schema["name"] == name
        assert schema["parameters"]["additionalProperties"] is False
        assert tool["toolset"] == "cashdesk"


def test_status_and_brief_on_an_empty_ledger(plugin):
    module, ctx = plugin
    status = ctx.call("cashdesk_status")
    assert status["ledger"] == "empty" and status["engine"] is True
    assert status["engine_version"] == module.engine_version() != "unknown"
    brief = ctx.call("cashdesk_brief", lang="cs", as_of=AS_OF)
    assert brief["lang"] == "cs" and "Zatím žádná evidence" in brief["brief"]
    assert ctx.call("cashdesk_queue")["count"] == 0


def test_import_refuses_bad_input_and_duplicates(plugin, hermes_fakes):
    module, ctx = plugin
    data = hermes_fakes / "plugin-data" / "cash-desk"
    data.mkdir(parents=True, exist_ok=True)
    assert ctx.call("cashdesk_import", kind="receipts", path=str(DEMO / "customers.csv"))["ok"] is False
    assert "no such file" in ctx.call("cashdesk_import", kind="customers", path=str(data / "missing.csv"))["error"]
    first = ctx.call("cashdesk_import", kind="customers", path=str(DEMO / "customers.csv"))
    assert first["ok"] and first["imported"]["customers"] == 8
    again = ctx.call("cashdesk_import", kind="customers", path=str(DEMO / "customers.csv"))
    assert again["ok"] is False and again.get("duplicate") is True and "already imported" in again["error"]
    bad = data / "bad.csv"
    bad.write_text("foo,bar\n1,2\n", encoding="utf-8")
    refused = ctx.call("cashdesk_import", kind="customers", path=str(bad))
    assert refused["ok"] is False and refused["errors"]


def test_import_reads_only_from_allowed_folders(plugin, tmp_path, monkeypatch):
    """Only the plugin's data folder, its bundled demo/ and the import_dirs setting are readable."""
    module, ctx = plugin
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "customers.csv"
    target.write_bytes((DEMO / "customers.csv").read_bytes())
    opened = []
    real_read = module._engine().read_customers
    monkeypatch.setattr(module._engine(), "read_customers", lambda p, *a, **k: opened.append(p) or real_read(p, *a, **k))

    refused = ctx.call("cashdesk_import", kind="customers", path=str(target))
    assert refused["ok"] is False and "outside the folders Cash Desk may read" in refused["error"], refused
    assert "import_dirs" in refused["error"] and opened == []
    # A missing file outside the folders gets the same answer, so nothing is learned about the disk.
    assert "outside the folders" in ctx.call("cashdesk_import", kind="customers", path=str(elsewhere / "nope.csv"))["error"]
    # ".." and symlinks are resolved before the check.
    sneaky = DEMO / ".." / ".." / target.relative_to(target.anchor)
    assert "outside the folders" in ctx.call("cashdesk_import", kind="customers", path=str(sneaky))["error"]
    link_dir = tmp_path / "plugin-data" / "cash-desk"
    link_dir.mkdir(parents=True, exist_ok=True)
    (link_dir / "link.csv").symlink_to(target)
    assert "outside the folders" in ctx.call("cashdesk_import", kind="customers", path=str(link_dir / "link.csv"))["error"]
    assert opened == []

    # The README's demo step uses paths relative to the plugin folder.
    assert ctx.call("cashdesk_import", kind="invoices", path="demo/invoices.csv")["ok"]
    assert "outside the folders" in ctx.call("cashdesk_import", kind="customers", path="../x.csv")["error"]

    ctx.config["import_dirs"] = f"  {elsewhere} , /nonexistent/folder "
    allowed = ctx.call("cashdesk_import", kind="customers", path=str(target))
    assert allowed["ok"], allowed
    assert opened and Path(opened[0]).resolve() == target.resolve()


def test_match_applies_only_exact_reference_matches(plugin):
    module, ctx = plugin
    assert ctx.call("cashdesk_import", kind="customers", path=str(DEMO / "customers.csv"))["ok"]
    assert ctx.call("cashdesk_import", kind="invoices", path=str(DEMO / "invoices.csv"))["ok"]
    assert ctx.call("cashdesk_import", kind="bank", path=str(DEMO / "bank_day1.csv"))["ok"]
    result = ctx.call("cashdesk_match", as_of=AS_OF)
    applied = {m["invoice_number"] for m in result["applied"]}
    assert applied == {"INV-2026-0045"}
    reviewed = {i["payment"]["id"] for i in result["review"]}
    assert "B1-02" in reviewed
    assert all(c["invoice_number"] for i in result["review"] for c in i["candidates"])
    # Nothing is applied twice.
    assert ctx.call("cashdesk_match", as_of=AS_OF)["applied"] == []


def test_draft_queue_and_status(plugin):
    module, ctx = plugin
    load_demo(ctx)
    plan = ctx.call("cashdesk_draft", as_of=AS_OF)
    assert plan["ok"] and plan["created"] >= 5
    kinds = {d["kind"] for d in plan["drafts"]}
    assert {"stage07", "stage14", "stage21", "stage28", "thanks"} <= kinds
    assert any(h["reason"] for h in plan["held"])  # the disputed customer is held
    queue = ctx.call("cashdesk_queue")
    assert queue["count"] == plan["created"]
    draft = next(d for d in queue["drafts"] if d["kind"] == "stage14")
    assert draft["lang"] == "en" and draft["hash"] and draft["figures"]
    assert all(f["ok"] for f in draft["figures"])
    # Drafting again the same day creates nothing new.
    assert ctx.call("cashdesk_draft", as_of=AS_OF)["created"] == 0
    status = ctx.call("cashdesk_status", as_of=AS_OF)
    assert status["ledger"] == "ok" and status["awaiting_approval"] == plan["created"]
    assert set(status["open_by_bucket"]) >= {"EUR"}
    brief = ctx.call("cashdesk_brief", lang="en", as_of=AS_OF)
    assert "Cash Desk" in brief["brief"] or "brief" in brief["brief"].lower()


def test_draft_needs_a_sender(plugin):
    module, ctx = plugin
    load_demo(ctx)
    ctx.config.pop("sender_name")
    refused = ctx.call("cashdesk_draft", as_of=AS_OF)
    assert refused["ok"] is False and "set sender_name in the plugin settings" in refused["error"]
    assert ctx.call("cashdesk_draft", as_of=AS_OF, sender="Example Studio s.r.o.")["ok"]


def demo_stage14(ctx) -> dict:
    return next(d for d in ctx.call("cashdesk_queue")["drafts"] if d["kind"] == "stage14")


def test_figure_lock_accepts_rewording_and_rejects_changed_figures(plugin):
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    reworded = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=draft["body"] + "\n\nThank you for your help.")
    assert reworded["ok"], reworded
    assert reworded["draft"]["hash"] != draft["hash"]
    assert reworded["draft"]["status"] == "pending"
    body = reworded["draft"]["body"]
    assert "1,250.00" in body, body
    tampered = ctx.call("cashdesk_revise", draft_id=draft["id"],
                        new_text=body.replace("1,250.00", "1,520.00", 1) + "\nIBAN GB82 WEST 1234 5698 7654 32")
    assert tampered["ok"] is False and tampered["rejected"] is True
    assert "Figure Lock rejected the revision" in tampered["message"]
    assert tampered["diff"], tampered
    assert tampered["changed"] or tampered["missing"]
    assert tampered["extra"]  # the foreign IBAN
    # The stored draft is untouched by the rejected revision.
    assert demo_stage14(ctx)["body"] == body
    unknown = ctx.call("cashdesk_revise", draft_id="nope", new_text="x")
    assert unknown["ok"] is False


def test_reply_classification_holds_disputes(plugin):
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    reply = ctx.call("cashdesk_reply", customer_id=draft["customer_id"], text="We dispute this invoice, the amount is wrong.", lang="en")
    assert reply["classification"] == "dispute" and reply["held"] is True
    held = ctx.call("cashdesk_queue", status="held")["drafts"]
    assert any(d["id"] == draft["id"] for d in held)
    # A held customer cannot be approved; a person must release first.
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:8]}").startswith("Refused")
    assert "released" in ctx.command(f"release {draft['customer_id']}")
    assert ctx.call("cashdesk_queue")["count"] >= 1
    assert ctx.call("cashdesk_reply", customer_id="nobody", text="paid")["ok"] is False
    promise = ctx.call("cashdesk_reply", customer_id=draft["customer_id"], text="We will pay next week")
    assert promise["classification"] == "promise" and promise["held"] is False


def test_hold_tool_and_release_command(plugin):
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    assert ctx.call("cashdesk_hold", customer_id=draft["customer_id"], reason="phone call pending")["ok"]
    assert ctx.call("cashdesk_status", as_of=AS_OF)["on_hold"] >= 1
    assert "on hold" in ctx.command(f"hold {draft['customer_id']} still waiting")
    assert "released" in ctx.command(f"release {draft['customer_id']}")
    assert ctx.call("cashdesk_hold", customer_id="nobody")["ok"] is False


def test_slash_command_status_queue_and_help(plugin):
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    assert "awaiting approval" in ctx.command("status")
    queue = ctx.command("queue")
    assert "waiting for approval" in queue and "/cashdesk show" in queue
    assert "  Reminder 14 · Bluebell Design Studio Ltd · " in queue and "    /cashdesk show bluebell-reminder14-0042-nov10" in queue
    assert ctx.command("frobnicate").startswith("Unknown command 'frobnicate'") and module.HELP_TEXT in ctx.command("frobnicate")
    help_text = ctx.command("help")
    assert help_text == module.HELP_TEXT
    for verb in ("still-open", "not-duplicate", "import-errors", "overlaps", "returns", "settlements", "surpluses", "identities", "bank-evidence", "approve", "review"):
        assert verb in help_text, verb
    assert ctx.command("approve").startswith("Usage")
    draft = demo_stage14(ctx)
    assert ctx.command(f"approve {draft['id']} ffffffffffff").startswith("Refused")
    approved = ctx.command(f"approve {draft['id']} {draft['hash'][:8]}")
    assert approved.startswith("Approved")
    assert ctx.call("cashdesk_queue", status="approved")["count"] == 1
    rejected = ctx.command(f"reject {draft['id']} wrong tone")
    assert rejected.startswith("Rejected")
    assert ctx.call("cashdesk_queue", status="rejected")["count"] == 1


def test_gate_blocks_writes_in_cron_and_escalates_sends(plugin):
    module, ctx = plugin
    assert ctx.gate("cashdesk_status") is None
    assert ctx.gate("cashdesk_queue") is None
    assert ctx.gate("cashdesk_import", {"kind": "bank", "path": "x.csv"}) is None
    verdict = ctx.gate("cashdesk_send", {"draft_id": "D-1", "draft_hash": "abc123"})
    assert verdict["action"] == "block" and "Cash Desk will not send D-1" in verdict["message"]  # no such draft: never asked
    assert ctx.gate("cashdesk_send", {"draft_id": "D-1"})["action"] == "block"
    ctx.cron = object()
    for tool in sorted(module.WRITE_TOOLS):
        verdict = ctx.gate(tool, {"draft_hash": "abc"})
        assert verdict["action"] == "block" and "from a scheduled job; scheduled runs only read" in verdict["message"], tool
    assert ctx.gate("cashdesk_brief") is None
    ctx.cron = None
    del ctx.__class__.current_cron_execution
    try:
        unknown = ctx.gate("cashdesk_import", {})
        assert unknown["action"] == "block" and "cannot tell whether this is a scheduled run" in unknown["message"]
    finally:
        ctx.__class__.current_cron_execution = lambda self: self.cron


def test_gate_blocks_sends_when_host_approvals_are_off(plugin, approvals_mode):
    module, ctx = plugin
    approvals_mode[0] = "off"
    verdict = ctx.gate("cashdesk_send", {"draft_hash": "abc"})
    assert verdict["action"] == "block" and "Hermes approvals are switched off in this profile" in verdict["message"]
    refused = ctx.call("cashdesk_send", draft_id="D-1", draft_hash="abc")
    assert refused["ok"] is False and "Hermes approvals are switched off in this profile" in refused["error"]


def test_send_requires_an_approval_of_the_exact_text_and_writes_once(plugin, hermes_fakes):
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    refused = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert refused["ok"] is False and "no approval covers this exact text" in refused["error"]
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:8]}").startswith("Approved")
    wrong_hash = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash="0" * 64)
    assert wrong_hash["ok"] is False
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert sent["ok"], sent
    path = Path(sent["path"])
    assert path.is_file() and path.suffix == ".eml"
    assert path.name.endswith(" Bluebell Design Studio Ltd - Reminder 14 - INV-2026-0042.eml"), path.name
    assert str(path).startswith(str(hermes_fakes / "plugin-data" / "cash-desk" / "outbox"))
    message = email.message_from_string(path.read_text(encoding="utf-8"))
    assert message["X-Cash-Desk-Draft"].split() == [draft["id"], draft["hash"]]
    assert message["To"] == draft["recipients"][0] and message["From"] == "billing@example.invalid"
    again = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert again["ok"] is False and "was already sent; a draft goes out once" in again["error"]
    assert ctx.call("cashdesk_queue", status="sent")["count"] == 1
    # A revision after approval drops the approval: the old hash no longer sends.
    other = next(d for d in ctx.call("cashdesk_queue")["drafts"] if d["kind"] == "stage07")
    assert ctx.command(f"approve {other['id']} {other['hash'][:8]}").startswith("Approved")
    revised = ctx.call("cashdesk_revise", draft_id=other["id"], new_text=other["body"] + "\n\nKind regards.")
    assert revised["ok"]
    for stale_or_unapproved in (other["hash"], revised["draft"]["hash"]):
        refused = ctx.call("cashdesk_send", draft_id=other["id"], draft_hash=stale_or_unapproved)
        assert refused["ok"] is False and "no approval covers this exact text" in refused["error"]


def test_approve_refuses_a_stale_hash_prefix(plugin):
    """/cashdesk approve <id> <prefix> only approves the version the person read."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    revised = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=draft["body"] + "\n\nThank you.")
    assert revised["ok"]
    stale = ctx.command(f"approve {draft['id']} {draft['hash'][:8]}")
    assert stale.startswith("Refused") and "Re-read the draft before approving" in stale
    assert ctx.call("cashdesk_queue", status="approved")["count"] == 0
    fresh = ctx.command(f"approve {draft['id']} {revised['draft']['hash'][:8]}")
    assert fresh.startswith("Approved"), fresh


def test_send_refuses_a_draft_edited_in_the_ledger_after_approval(plugin, hermes_fakes):
    """Someone edits the stored text straight in the SQLite file and leaves the hash alone:
    the approved hash still names the draft, but it no longer covers the text, so nothing goes out."""
    import sqlite3

    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:8]}").startswith("Approved")
    db = hermes_fakes / "plugin-data" / "cash-desk" / "cashdesk.sqlite"
    with sqlite3.connect(db) as conn:
        changed = conn.execute("UPDATE drafts SET body = body || ? WHERE id = ?",
                               ("\nIBAN GB82 WEST 1234 5698 7654 32", draft["id"])).rowcount
    assert changed == 1
    refused = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert refused["ok"] is False and "does not match its approved hash" in refused["error"], refused
    outbox = hermes_fakes / "plugin-data" / "cash-desk" / "outbox"
    assert not outbox.exists() or not any(outbox.iterdir())


def test_send_needs_a_from_address_and_never_runs_scheduled(plugin):
    module, ctx = plugin
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    draft = demo_stage14(ctx)
    ctx.command(f"approve {draft['id']} {draft['hash'][:8]}")
    ctx.config["smtp_from"] = ""
    assert "set the From address (smtp_from)" in ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])["error"]
    ctx.config["smtp_from"] = "billing@example.invalid"
    ctx.cron = object()
    assert "never sends from a scheduled run" in ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])["error"]


def test_gate_blocks_writes_inside_a_subagent(plugin):
    """Hermes hides the cron run from delegated subagents, so writes never run in one."""
    module, ctx = plugin
    (pre,) = ctx.hooks["pre_tool_call"]
    (start,) = ctx.hooks["subagent_start"]
    (stop,) = ctx.hooks["subagent_stop"]
    assert pre("cashdesk_import", {}, task_id="t", session_id="child-1") is None
    start(parent_session_id="main-1", child_session_id="child-1", child_goal="x")
    for tool in sorted(module.WRITE_TOOLS):
        verdict = pre(tool, {"draft_hash": "abc"}, task_id="t", session_id="child-1")
        assert verdict["action"] == "block" and "inside a delegated subagent" in verdict["message"], tool
    assert pre("cashdesk_status", {}, task_id="t", session_id="child-1") is None
    assert pre("cashdesk_import", {}, task_id="t", session_id="main-1") is None
    stop(parent_session_id="main-1", child_session_id="child-1", child_status="completed")
    assert pre("cashdesk_import", {}, task_id="t", session_id="child-1") is None
    start(child_session_id=None)  # a missing id is ignored, not recorded
    assert not module._SUBAGENT_SESSIONS


def test_import_schema_names_every_bundled_demo_file(plugin):
    """An agent asked to "load the demo" finds every file from the tool description alone."""
    module, ctx = plugin
    description = ctx.tools["cashdesk_import"]["schema"]["parameters"]["properties"]["path"]["description"]
    for name in sorted(p.name for p in DEMO.glob("*.csv")):
        assert f"demo/{name}" in description, name


def test_one_version_everywhere():
    """plugin.yaml and the CHANGELOG's newest heading name one version; so does the catalog entry
    draft where it exists (the working repository only; the published tree leaves it out)."""
    import re as _re

    version = manifest()["version"]
    newest = _re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), _re.M).group(1)
    assert version == newest == "1.2.0", (version, newest)
    draft = ROOT / "docs" / "catalog-entry.draft.yaml"
    if draft.exists():
        catalog = yaml.safe_load(draft.read_text(encoding="utf-8"))
        assert catalog["version"] == version, catalog["version"]
        assert manifest()["requires_hermes"] == catalog["requires_hermes"]


def test_one_currency_setting_for_the_demo_and_for_files_without_a_currency(plugin, hermes_fakes):
    """Owner: everything in one currency (EUR by default), and people can change it, demo included."""
    module, ctx = plugin
    ctx.config["currency"] = "usd"
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    status = ctx.call("cashdesk_status", as_of=AS_OF)
    assert set(status["open_by_bucket"]) == {"USD"}
    assert "USD 1,250.00" in demo_stage14(ctx)["subject"]
    data = hermes_fakes / "plugin-data" / "cash-desk"
    (data / "bank.csv").write_text("id,date,amount,counterparty,reference\nX-1,2026-11-09,15.00,SOMEONE,thanks\n", encoding="utf-8")
    refused = ctx.call("cashdesk_import", kind="bank", path=str(data / "bank.csv"))
    assert refused["ok"] is False and "will not assume the account is in USD" in refused["error"]
    assert ctx.call("cashdesk_import", kind="bank", path=str(data / "bank.csv"), default_currency="USD")["ok"]
    assert {p["currency"] for p in ctx.call("cashdesk_match", as_of=AS_OF)["unmatched"] for p in [p["payment"]]} == {"USD"}


def test_the_default_currency_is_eur(plugin):
    module, ctx = plugin
    load_demo(ctx)
    assert set(ctx.call("cashdesk_status", as_of=AS_OF)["open_by_bucket"]) == {"EUR"}


def test_the_demo_always_happens_today(plugin, monkeypatch):
    """On any real day the demo's dates move with it, so "7 days overdue" stays true: an agent
    (or a judge) running it on 20 November sees INV-2026-0041 due 13 November, not 3 November."""
    module, ctx = plugin
    monkeypatch.setenv("CASHDESK_TODAY", "2026-11-20")
    for kind, name in (("customers", "customers.csv"), ("invoices", "invoices.csv"), ("bank", "bank_day1.csv"), ("bank", "bank_day2.csv")):
        assert ctx.call("cashdesk_import", kind=kind, path=f"demo/{name}")["ok"]
    assert ctx.call("cashdesk_match")["ok"]  # no as_of: today
    ctx.call("cashdesk_draft")
    northgate = next(d for d in ctx.call("cashdesk_queue")["drafts"] if d["kind"] == "stage07")
    assert "INV-2026-0041 due 2026-11-13: EUR 980.00 (7 days overdue)" in northgate["body"], northgate["body"]
    assert northgate["id"].endswith("-nov20")
    assert len(ctx.call("cashdesk_queue")["drafts"]) == 6
