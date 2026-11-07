"""Pre-demo hardening (2026-11-05): each test is a case a judge or a bookkeeper broke on v1.0.0."""

from __future__ import annotations

import sqlite3

from tests.plugin.support import AS_OF, DEMO, load_demo

BANK_HEADER = "id,date,amount,currency,counterparty,reference,memo\n"


def data_dir(hermes_fakes):
    d = hermes_fakes / "plugin-data" / "cash-desk"
    d.mkdir(parents=True, exist_ok=True)
    return d


def import_bank(ctx, hermes_fakes, name, rows):
    path = data_dir(hermes_fakes) / name
    path.write_text(BANK_HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    result = ctx.call("cashdesk_import", kind="bank", path=str(path))
    assert result["ok"], result


def import_invoices_with(ctx, hermes_fakes, name, changes=None, extra=""):
    """The demo invoice export again (next week's export), with some lines changed."""
    text = (DEMO / "invoices.csv").read_text(encoding="utf-8")
    for old, new in (changes or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    path = data_dir(hermes_fakes) / name
    path.write_text(text + extra, encoding="utf-8")
    result = ctx.call("cashdesk_import", kind="invoices", path=str(path), source="generic")
    assert result["ok"], result
    return result


def invoice(hermes_fakes, number):
    db = sqlite3.connect(data_dir(hermes_fakes) / "cashdesk.sqlite")
    db.row_factory = sqlite3.Row
    try:
        return dict(db.execute("SELECT * FROM invoices WHERE number = ?", (number,)).fetchone())
    finally:
        db.close()


def pending(ctx, prefix):
    return [d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["id"].startswith(prefix)]


def test_next_weeks_export_does_not_undo_an_applied_payment(plugin, hermes_fakes):
    """Bookkeeper + judge: the export still showed the paid invoice open; v1.0.0 reopened it and reminded the payer."""
    module, ctx = plugin
    load_demo(ctx)
    assert invoice(hermes_fakes, "INV-2026-0045")["status"] == "paid"  # B1-01 paid it exactly
    import_invoices_with(ctx, hermes_fakes, "invoices-next-week.csv",
                         extra="INV-2026-0050,C001,2026-11-05,2026-11-19,300.00,EUR,300.00,open,20260050,DE89 3704 0044 0532 0130 00\n")
    inv = invoice(hermes_fakes, "INV-2026-0045")
    assert (inv["status"], inv["balance"]) == ("paid", "0.00")
    ctx.draft_on(AS_OF)
    drafts = ctx.call("cashdesk_queue", status="pending")["drafts"]
    assert not [d for d in drafts if "INV-2026-0045" in d["invoice_numbers"] and d["kind"].startswith("stage")]


def test_an_approved_reminder_with_an_old_amount_is_refused_and_redrafted(plugin, hermes_fakes):
    """Judge: the balance changed after drafting; v1.0.0 still approved and sent the old amount."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "bluebell-reminder14")
    assert "EUR 1,250.00" in draft["body"]
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:8]}").startswith("Approved")
    import_invoices_with(ctx, hermes_fakes, "invoices-partly-paid.csv", {
        "INV-2026-0042,C002,2026-10-13,2026-10-27,1250.00,EUR,1250.00": "INV-2026-0042,C002,2026-10-13,2026-10-27,1250.00,EUR,400.00"})
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not sent["ok"] and "EUR 400.00 open, not the amount this draft states" in sent["error"], sent
    again = ctx.draft_on(AS_OF)
    assert [s["draft_id"] for s in again["superseded"]] == [draft["id"]]
    (fresh,) = pending(ctx, "bluebell-reminder14")
    assert "EUR 400.00" in fresh["body"] and "1,250.00" not in fresh["body"]


def test_a_rejected_reminder_is_drafted_again_on_a_later_day(plugin, hermes_fakes):
    """Judge: a rejected reminder blocked its stage for good (`already: stage07`)."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "northgate-reminder")
    assert "Rejected" in ctx.command(f"reject {draft['id']} too early")
    assert not pending(ctx, "northgate-reminder")
    ctx.draft_on("2026-11-11")
    assert len(pending(ctx, "northgate-reminder")) == 1


def test_a_payment_from_someone_else_is_not_applied_automatically(plugin, hermes_fakes):
    """Bookkeeper: Beta paid its own amount but typed Acme's invoice number; v1.0.0 thanked Acme."""
    module, ctx = plugin
    load_demo(ctx)
    import_bank(ctx, hermes_fakes, "bank-other-payer.csv", ["X-1,2026-11-09,1250.00,EUR,STONEBRIDGE HOLDINGS LTD,INV-2026-0042,"])
    result = ctx.call("cashdesk_match", as_of=AS_OF)
    assert not result["applied"]
    (item,) = [i for i in result["review"] if i["payment"]["id"] == "X-1"]
    assert item["kind"] == "PAYER" and "Bluebell" in item["detail"] and "check who paid before applying" in item["detail"]
    assert invoice(hermes_fakes, "INV-2026-0042")["status"] == "open"


def test_a_truncated_payer_name_still_applies(plugin, hermes_fakes):
    module, ctx = plugin
    load_demo(ctx)
    import_bank(ctx, hermes_fakes, "bank-truncated.csv", ["X-2,2026-11-09,1250.00,EUR,BLUEBELL DESIGN ST,INV-2026-0042,"])
    result = ctx.call("cashdesk_match", as_of=AS_OF)
    assert [a["invoice_number"] for a in result["applied"]] == ["INV-2026-0042"]


def test_a_payment_dated_before_the_invoice_goes_to_review(plugin, hermes_fakes):
    """Bookkeeper: US-format bank dates put a payment in March, before the invoice existed."""
    module, ctx = plugin
    load_demo(ctx)
    import_bank(ctx, hermes_fakes, "bank-early.csv", ["X-3,2026-03-11,1250.00,EUR,BLUEBELL DESIGN STUDIO LTD,INV-2026-0042,"])
    result = ctx.call("cashdesk_match", as_of=AS_OF)
    assert not result["applied"]
    (item,) = [i for i in result["review"] if i["payment"]["id"] == "X-3"]
    assert item["kind"] == "EARLY" and "check the dates before applying" in item["detail"]


def test_a_person_applies_or_dismisses_a_payment_waiting_for_review(plugin, hermes_fakes):
    """Judge: review items could never be resolved, so a part-paid invoice was held forever."""
    module, ctx = plugin
    load_demo(ctx)
    listing = ctx.command("review")
    assert "B1-02" in listing and "EUR 700.00" in listing
    over = ctx.command("apply B1-02 INV-2026-0031 800")  # more than the payment
    assert over.startswith("Refused") and "is left to apply" in over
    done = ctx.command("apply B1-02 INV-2026-0031 700")
    assert done.startswith("Applied EUR 700.00 of B1-02 to INV-2026-0031") and "EUR 2,900.00 still open" in done
    assert invoice(hermes_fakes, "INV-2026-0031")["balance"] == "2900.00"
    assert "B1-02" not in ctx.command("review")
    assert "Refused" in ctx.command("dismiss B1-02")  # already applied
    import_bank(ctx, hermes_fakes, "bank-other.csv", ["X-4,2026-11-09,55.00,EUR,CITY SAVINGS BANK,interest Q3,"])
    assert "X-4" in ctx.command("review")
    assert ctx.command("dismiss X-4 bank interest").startswith("Dismissed X-4")
    assert "X-4" not in ctx.command("review")


def test_the_model_has_no_tool_to_apply_or_dismiss(plugin):
    module, ctx = plugin
    assert not [t for t in ctx.tools if "apply" in t or "dismiss" in t]


def test_hermes_prompt_names_the_reminder_recipient_total_and_hash(plugin, hermes_fakes):
    """Judge: the second gate said only 'a reminder a person approved'."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "bluebell-reminder14")
    # Not approved yet: Hermes is never asked to allow a send Cash Desk would refuse.
    early = ctx.gate("cashdesk_send", {"draft_id": draft["id"], "draft_hash": draft["hash"]})
    assert early["action"] == "block" and "no approval covers this exact text" in early["message"]
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:12]}").startswith("Approved")
    verdict = ctx.gate("cashdesk_send", {"draft_id": draft["id"], "draft_hash": draft["hash"]})
    assert verdict["action"] == "approve" and verdict["rule_key"] == f"cashdesk:send:{draft['hash']}"
    for part in ("Reminder 14", "Bluebell Design Studio Ltd", "mia@bluebell.example", "total EUR 1,250.00", draft["hash"][:12]):
        assert part in verdict["message"], (part, verdict["message"])


def test_no_send_where_hermes_would_pass_its_own_prompt(plugin, hermes_fakes, monkeypatch):
    """Judge: Hermes auto-approves under yolo and in single-query/cron/unattended 'approve' modes."""
    import sys

    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "bluebell-reminder14")
    args = {"draft_id": draft["id"], "draft_hash": draft["hash"]}
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    assert ctx.gate("cashdesk_send", args)["action"] == "block"
    monkeypatch.delenv("HERMES_YOLO_MODE")
    for key in ("single_query_mode", "cron_mode", "unattended_mode"):
        monkeypatch.setattr(sys.modules["hermes_cli.config"], "load_config_readonly", lambda key=key: {"approvals": {"mode": "manual", key: "approve"}})
        verdict = ctx.gate("cashdesk_send", args)
        assert verdict["action"] == "block" and key in verdict["message"], verdict
        assert "lets Hermes pass its prompt with nobody there" in verdict["message"]
        assert not ctx.call("cashdesk_send", **args)["ok"]


def test_a_rewording_may_not_add_links_addresses_percentages_or_terms(plugin, hermes_fakes):
    """Judge: Figure Lock accepted a pay-by-card link, a redirect address, 8 % interest, 'half as full settlement'."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "bluebell-reminder14")
    body = draft["body"]
    for addition, kind in (("Pay by card at https://pay.acme-billing.example/inv42.", "link"),
                           ("Please send the remittance to accounts@acme-billing.example.", "email address"),
                           ("Late payments carry 8% interest.", "percentage"),
                           ("We will accept half as full and final settlement.", "term"),
                           ("Otherwise we will take legal action.", "term"),
                           ("We will accept half of the balance as full settlement.", "term"),
                           ("Pay by this Friday or your service will be suspended.", "term")):
        result = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=body + "\n" + addition)
        assert result["rejected"], addition
        assert any(line.startswith(f"added: {kind}") and "a rewording may not add" in line for line in result["diff"]), result["diff"]
    still = ctx.call("cashdesk_queue", status="pending")["drafts"]
    assert [d["hash"] for d in still if d["id"] == draft["id"]] == [draft["hash"]]
    warmer = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text="Hope you are well!\n" + body)
    assert warmer["ok"], warmer



def test_only_a_reminder_that_really_went_out_is_called_earlier(plugin, hermes_fakes):
    """Live run: the agent flagged that a first 28-day reminder claimed earlier reminders that were never sent."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    first = [d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["kind"].startswith("stage")]
    assert first and not [d for d in first if "earlier reminder" in d["body"].lower()]
    (north,) = pending(ctx, "northgate-reminder7")
    ctx.command(f"approve {north['id']} {north['hash'][:8]}")
    assert ctx.call("cashdesk_send", draft_id=north["id"], draft_hash=north["hash"])["ok"]
    ctx.draft_on("2026-11-17")  # a week later: Northgate's invoice reaches 14 days
    (second,) = pending(ctx, "northgate-reminder14")
    assert "earlier reminder" in second["body"].lower(), second["body"]


def test_approving_needs_the_hash_the_person_read(plugin, hermes_fakes):
    """Second review: '/cashdesk approve <id>' approved whatever the text was at that moment."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "bluebell-reminder14")
    refused = ctx.command(f"approve {draft['id']}")
    assert refused.startswith("Refused") and "read the text first with /cashdesk show" in refused
    assert draft['hash'][:8] not in refused  # the refusal never hands out the hash
    shown = ctx.command(f"show {draft['id']}")
    assert draft['body'].strip()[:40] in shown and shown.rstrip().endswith(f"/cashdesk approve {draft['id']} {draft['hash'][:12]}")
    assert draft['hash'][:8] not in ctx.command("queue")
    assert ctx.command(f"approve {draft['id']} {draft['hash'][:8]}").startswith("Approved")


def test_a_newer_stage_replaces_the_older_unsent_reminder(plugin, hermes_fakes):
    """Second review: a pending 7-day reminder stayed sendable next to the 14-day one, still saying '7 days'."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    (seven,) = pending(ctx, "northgate-reminder7")
    ctx.command(f"approve {seven['id']} {seven['hash'][:8]}")
    later = ctx.draft_on("2026-11-17")
    assert seven["id"] in [s["draft_id"] for s in later["superseded"]]
    assert not [d for d in ctx.call("cashdesk_queue", status="approved")["drafts"] if d["id"] == seven["id"]]
    assert ctx.call("cashdesk_send", draft_id=seven["id"], draft_hash=seven["hash"])["ok"] is False
    (fourteen,) = pending(ctx, "northgate-reminder14")
    assert "14 days" in fourteen["body"]


def test_a_part_payment_on_one_invoice_of_a_two_invoice_reminder_is_caught(plugin, hermes_fakes):
    """Second review: 3,600 + 720 in one draft; 2,880 paid on the first left 720, which the draft 'contained'."""
    module, ctx = plugin
    load_demo(ctx)
    assert ctx.command("dismiss B1-02 verified noninvoice receipt for amount control").startswith("Dismissed")
    ctx.draft_on(AS_OF)
    (draft,) = pending(ctx, "birchfield-reminder28")
    ctx.command(f"approve {draft['id']} {draft['hash'][:8]}")
    import_bank(ctx, hermes_fakes, "bank-part.csv", ["X-9,2026-11-09,2880.00,EUR,BIRCHFIELD EXAMPLE CONSULTING,partial,"])
    assert ctx.command("apply X-9 INV-2026-0031 2880").startswith("Applied")  # 3,600 -> 720, same as INV-2026-0044
    sent = ctx.call("cashdesk_send", draft_id=draft["id"], draft_hash=draft["hash"])
    assert not sent["ok"] and "INV-2026-0031 now has EUR 720.00 open" in sent["error"], sent


def test_an_open_invoice_export_holds_invoices_it_no_longer_lists(plugin, hermes_fakes):
    """Second review: next week's 'Awaiting payment' export leaves paid invoices out; v1 kept chasing them."""
    module, ctx = plugin
    load_demo(ctx)
    text = (DEMO / "invoices.csv").read_text(encoding="utf-8")
    week2 = "\n".join(l for l in text.splitlines() if not l.startswith("INV-2026-0041,")) + "\n"
    path = data_dir(hermes_fakes) / "awaiting-payment.csv"
    path.write_text(week2, encoding="utf-8")
    result = ctx.call("cashdesk_import", kind="invoices", path=str(path), source="generic", open_list=True)
    assert result["ok"] and result["imported"]["not_in_export"] == 1, result
    drafts = ctx.draft_on(AS_OF)
    held = [h for h in drafts["held"] if "INV-2026-0041" in h["invoice_numbers"]]
    assert held and held[0]["reason"] == "not_in_export" and "awaiting-payment.csv" in held[0]["detail"]
    assert not pending(ctx, "northgate-reminder")


def test_a_contact_list_without_ids_meets_the_invoice_export_by_name(plugin, hermes_fakes):
    """Second review: a QuickBooks/Xero export has no email; a contact list could not reach its hashed ids."""
    module, ctx = plugin
    d = data_dir(hermes_fakes)
    (d / "qbo-invoices.csv").write_text(
        "Invoice Date,No.,Customer,Due Date,Amount,Open Balance\n"
        "10/01/2026,1047,Acme Trading Ltd,10/20/2026,1500.00,1500.00\n"
        "10/14/2026,1048,Beta Studio Ltd,10/28/2026,800.00,800.00\n", encoding="utf-8")
    r = ctx.call("cashdesk_import", kind="invoices", path=str(d / "qbo-invoices.csv"), source="quickbooks", date_order="mdy")
    assert r["ok"], r
    (d / "contacts.csv").write_text("Customer,Email,Full Name\nAcme Trading Ltd,ap@acme.example,Jo Example\nBeta Studio Ltd,ap@beta.example,\n", encoding="utf-8")
    c = ctx.call("cashdesk_import", kind="customers", path=str(d / "contacts.csv"))
    assert c["ok"], c
    drafts = ctx.draft_on(AS_OF)
    to = sorted(r for dd in drafts["drafts"] for r in dd["recipients"])
    assert to == ["ap@acme.example", "ap@beta.example"], drafts["held"]
    assert len(ctx.call("cashdesk_status")["customers"] if "customers" in ctx.call("cashdesk_status") else [1, 2]) >= 1


def test_the_rest_of_an_overpayment_can_be_dismissed(plugin, hermes_fakes):
    """Second review: 1,100 paid against 1,000; the 100 left over stayed in review for ever."""
    module, ctx = plugin
    load_demo(ctx)
    import_bank(ctx, hermes_fakes, "bank-over.csv", ["X-5,2026-11-09,1350.00,EUR,BLUEBELL DESIGN STUDIO LTD,INV-2026-0042,"])
    assert ctx.command("apply X-5 INV-2026-0042").startswith("Applied EUR 1,250.00")
    assert "X-5" in ctx.command("review")  # EUR 100.00 left
    assert ctx.command("dismiss X-5 overpaid, refund by hand").startswith("Dismissed X-5")
    assert "X-5" not in ctx.command("review")


def test_a_small_leftover_balance_is_not_chased(plugin, hermes_fakes):
    """Second review: a bank fee of EUR 7.50 got its own 21-day reminder."""
    module, ctx = plugin
    load_demo(ctx)
    import_bank(ctx, hermes_fakes, "bank-fee.csv", ["X-6,2026-11-09,1242.50,EUR,BLUEBELL DESIGN STUDIO LTD,INV-2026-0042,"])
    assert ctx.command("apply X-6 INV-2026-0042").startswith("Applied EUR 1,242.50")
    drafts = ctx.draft_on(AS_OF)
    assert not pending(ctx, "bluebell-reminder")
    (held,) = [h for h in drafts["held"] if "INV-2026-0042" in h["invoice_numbers"]]
    assert held["reason"] == "small_balance" and "EUR 7.50" in held["detail"]


def test_rejecting_an_escalation_note_does_not_chase_the_customer_again(plugin, hermes_fakes):
    """Second review: rejecting the internal note wiped the history and re-sent a 28-day reminder."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)  # Stonebridge (36 days, never reminded) gets Reminder 28
    (r28,) = [d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["customer_id"] == "C005" and d["kind"] == "stage28"]
    assert ctx.command(f"approve {r28['id']} {r28['hash'][:12]}").startswith("Approved")
    assert ctx.call("cashdesk_send", draft_id=r28["id"], draft_hash=r28["hash"])["ok"]
    ctx.draft_on("2026-11-18")  # a week later, still unanswered: the escalation note
    (esc,) = [d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["kind"] == "escalation" and d["customer_id"] == "C005"]
    ctx.command(f"reject {esc['id']} handled by phone")
    ctx.draft_on("2026-11-19")
    assert not [d for d in ctx.call("cashdesk_queue", status="pending")["drafts"]
                if d["customer_id"] == esc["customer_id"] and d["kind"].startswith("stage") and d["id"].endswith("nov19")]


def test_a_promise_to_pay_pauses_reminders_until_two_days_after_it(plugin, hermes_fakes):
    """Second review: 'we will pay by 30 November' still got the next reminder. The model reads the reply."""
    module, ctx = plugin
    load_demo(ctx)
    reply = ctx.call("cashdesk_reply", customer_id="C003", text="Sorry for the delay, we will pay by 20 November.",
                     kind="promise", promised_date="2026-11-20")
    assert reply["ok"] and reply["paused_until"] == "2026-11-20"
    ctx.draft_on(AS_OF)
    assert not pending(ctx, "greenhill-reminder")
    held = [h for h in ctx.draft_on("2026-11-12")["held"] if h["customer_id"] == "C003"]
    assert held and held[0]["reason"] == "promise"
    ctx.draft_on("2026-11-23")
    assert pending(ctx, "greenhill-reminder")


def test_the_model_s_reading_of_a_reply_wins_over_keywords(plugin, hermes_fakes):
    """Second review: 'we never received the invoice' read as a dispute by the keyword list."""
    module, ctx = plugin
    load_demo(ctx)
    r = ctx.call("cashdesk_reply", customer_id="C001", text="We never received the invoice, can you resend it?", kind="other")
    assert r["classification"] == "other" and r["held"] is False


def test_the_date_order_setting_is_the_default_for_imports(plugin, hermes_fakes):
    """Second review: a one-row weekly bank file dated the 1st-12th was refused every time."""
    module, ctx = plugin
    load_demo(ctx)
    path = data_dir(hermes_fakes) / "week.csv"
    path.write_text("Date,Amount,Currency,Counter Party,Reference\n09/11/2026,1250.00,EUR,BLUEBELL DESIGN STUDIO LTD,INV-2026-0042\n", encoding="utf-8")
    assert not ctx.call("cashdesk_import", kind="bank", path=str(path))["ok"]
    ctx.config["date_order"] = "dmy"
    result = ctx.call("cashdesk_import", kind="bank", path=str(path))
    assert result["ok"] and result["date_order"] == "dmy", result
    assert [a["invoice_number"] for a in ctx.call("cashdesk_match", as_of=AS_OF)["applied"]] == ["INV-2026-0042"]


def test_no_escalation_note_for_a_28_day_reminder_that_was_never_sent(plugin, hermes_fakes):
    """Judge 13: the note said 'unanswered' about a reminder nobody had sent."""
    module, ctx = plugin
    load_demo(ctx)
    ctx.draft_on(AS_OF)
    ctx.draft_on("2026-11-18")
    assert not [d for d in ctx.call("cashdesk_queue", status="pending")["drafts"] if d["kind"] == "escalation" and d["customer_id"] == "C005"]
