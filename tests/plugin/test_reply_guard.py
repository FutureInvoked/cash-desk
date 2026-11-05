"""Figure Lock on the agent's replies (opt-in setting guard_outgoing_messages).

The reply guard is a transform_llm_output hook: a reply that talks about a ledger customer
or quotes an invoice number may only state the amounts, IBANs and invoice numbers the ledger
holds for that customer. Anything else is withheld and replaced with a short notice before
Hermes stores or delivers it. Replies about anything else are never touched.
"""

from __future__ import annotations

import pytest

from tests.plugin.support import FakeCtx, load_demo

GOOD = ("Hello Bluebell Design Studio Ltd, invoice INV-2026-0042 for EUR 1,250.00 is overdue. "
        "IBAN DE89 3704 0044 0532 0130 00.")


@pytest.fixture
def guarded(plugin):
    module, ctx = plugin
    load_demo(ctx)
    on = FakeCtx(config={**ctx.config, "guard_outgoing_messages": True})
    module.register(on)
    (hook,) = on.hooks["transform_llm_output"]
    return module, on, lambda text: hook(response_text=text, session_id="s-1", model="m", platform="telegram", turn_id="t-1")


def test_the_hook_is_registered_but_does_nothing_while_the_setting_is_off(plugin):
    module, ctx = plugin
    load_demo(ctx)
    (hook,) = ctx.hooks["transform_llm_output"]
    assert ctx.hook_options["transform_llm_output"].get("hold_stream", False) is False
    assert hook(response_text="INV-2026-0042 is EUR 1,520.00", session_id="s", model="m", platform="telegram", turn_id="t") is None


def test_on_it_holds_the_stream_so_a_wrong_figure_is_never_shown_first(guarded):
    module, ctx, guard = guarded
    assert ctx.hook_options["transform_llm_output"]["hold_stream"] is True


def test_a_reply_with_the_ledger_figures_passes(guarded):
    module, ctx, guard = guarded
    assert guard(GOOD) is None
    # Birchfield's open total across two invoices is a ledger figure too.
    assert guard("Birchfield Example Consulting GmbH owes EUR 4,320.00 on INV-2026-0031 and INV-2026-0044.") is None


def test_a_wrong_amount_for_that_customer_is_withheld(guarded):
    module, ctx, guard = guarded
    out = guard(GOOD.replace("EUR 1,250.00", "€1,520.00"))
    assert out and "Cash Desk withheld this reply" in out and "EUR 1,520.00" in out
    # EUR 980.00 is a real ledger amount, but Northgate's, not Bluebell's.
    assert "Cash Desk withheld this reply" in guard("Bluebell Design Studio Ltd, INV-2026-0042: please pay EUR 980.00.")


def test_a_foreign_iban_or_an_unknown_invoice_number_is_withheld(guarded):
    module, ctx, guard = guarded
    out = guard(GOOD + " Or pay to GB82 WEST 1234 5698 7654 32.")
    assert out and "GB82" in out.replace(" ", "")
    assert "Cash Desk withheld this reply" in guard("Your invoice INV-2026-0099 is due.")


def test_replies_without_ledger_figures_or_ledger_customers_are_never_touched(guarded):
    module, ctx, guard = guarded
    assert guard("Thanks, I will check again in 14 days.") is None
    assert guard("The new GPU costs USD 300.00 and ships on 2026-11-20.") is None
    assert guard("") is None


def test_the_guard_fails_closed_when_the_ledger_cannot_be_read(guarded, monkeypatch):
    module, ctx, guard = guarded
    monkeypatch.setattr(module, "_store", lambda: None)
    assert "could not read the ledger" in guard(GOOD)
    assert guard("Nothing to check here.") is None
