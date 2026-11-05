"""Measured claim: every figure in every demo draft, altered one digit at a time, is caught.

For each of the demo's drafts the test reads out every figure Figure Lock knows (amounts,
dates, invoice numbers, payment references, IBANs), then makes two single-digit edits per
occurrence (the last digit and the first digit) and sends the edited text through the real
``cashdesk_revise`` tool. Every edit must be rejected with the draft left as it was. The
untouched text, reworded, must still pass, so the lock is not simply refusing everything.
The README quotes the totals; this test fails if they drift.
"""

from __future__ import annotations

import re

from tests.plugin.support import AS_OF, ROOT, load_demo


def _bump(ch: str) -> str:
    return str((int(ch) + 1) % 10)


def mutations(body: str, raw: str):
    """Yield (label, mutated body) for each occurrence of ``raw`` and each digit position."""
    for occurrence in re.finditer(re.escape(raw), body):
        digits = [occurrence.start() + m.start() for m in re.finditer(r"\d", raw)]
        for label, pos in (("last digit", digits[-1]), ("first digit", digits[0])) if digits else ():
            yield label, body[:pos] + _bump(body[pos]) + body[pos + 1:]


def run_mutations(module, ctx):
    lock = module._engine()
    load_demo(ctx)
    ctx.call("cashdesk_draft", as_of=AS_OF)
    drafts = ctx.call("cashdesk_queue")["drafts"]
    tried, caught, misses, by_kind = 0, 0, [], {}
    for draft in drafts:
        body = draft["body"]
        control = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=body + "\n")
        assert control["ok"], (draft["id"], control)
        body = ctx.call("cashdesk_queue")["drafts"]
        body = next(d for d in body if d["id"] == draft["id"])["body"]
        seen = set()
        for figure in lock.extract_figures(body, draft["lang"]):
            if not figure.raw or figure.raw in seen:
                continue
            seen.add(figure.raw)
            for label, mutated in mutations(body, figure.raw):
                if mutated == body:
                    continue
                tried += 1
                result = ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=mutated)
                after = next(d for d in ctx.call("cashdesk_queue")["drafts"] if d["id"] == draft["id"])["body"]
                stats = by_kind.setdefault(figure.kind, [0, 0])
                stats[0] += 1
                if result["ok"] is False and result.get("rejected") and after == body:
                    caught += 1
                    stats[1] += 1
                else:
                    misses.append((draft["id"], figure.kind, figure.raw, label, result.get("ok")))
                    if result["ok"]:  # put the original back so later mutations start from it
                        ctx.call("cashdesk_revise", draft_id=draft["id"], new_text=body)
    return len(drafts), tried, caught, misses, by_kind


def test_every_single_digit_change_to_a_demo_figure_is_caught(plugin):
    module, ctx = plugin
    drafts, tried, caught, misses, by_kind = run_mutations(module, ctx)
    print(f"\nmutation run: {drafts} drafts, {tried} edits tried, {caught} caught; by kind {by_kind}")
    assert drafts == 7 and tried >= 50
    assert not misses, misses
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"{caught} of {tried}" in readme, f"README should say '{caught} of {tried}'"


def test_the_same_run_reports_every_edit_as_a_miss_when_the_lock_is_off(plugin, monkeypatch):
    """Negative control: with a lock that accepts any text, the harness must see 72 misses."""
    from dataclasses import replace

    module, ctx = plugin
    engine = module._engine()

    def no_lock(draft, new_text, allowed, new_subject=None):
        subject = draft.subject if new_subject is None else new_subject
        revised = engine.with_hash(replace(draft, subject=subject, body=new_text, status="pending"))
        return engine.RevisionResult(True, revised, engine.LockResult(True))

    monkeypatch.setattr(engine, "revise", no_lock)
    drafts, tried, caught, misses, _ = run_mutations(module, ctx)
    assert caught == 0 and len(misses) == tried == 72
    assert "With the lock switched off\nthe same run reports 72 misses" in (ROOT / "README.md").read_text(encoding="utf-8")
