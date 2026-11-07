"""Figure sets are insufficient: preserve invoice/amount/date/reference roles."""
import datetime as dt
from dataclasses import replace
from decimal import Decimal as D

import pytest

from cashdesk.approval import with_hash
from cashdesk.figure_lock import revise, ledger_figures
from cashdesk.model import Customer, Invoice
from cashdesk.money import format_money
from cashdesk.dates import format_date
from cashdesk.render import DraftLine, RenderContext, render_draft
from cashdesk.store import Store, ConflictError

DAY = dt.date(2026, 11, 10)


def setup(lang='en', kind='stage14', path=':memory:'):
    store = Store(path, today=lambda: DAY)
    customer = Customer('C1', 'Demo Studio', lang, 'alex@example.test', 'Alex')
    invoices = [Invoice(f'INV-100{i}', 'C1', dt.date(2026, 10, 1), dt.date(2026, 10, 20+i),
                        D(amount), 'EUR', D(balance), 'open', f'2026100{i}', 'DE89370400440532013000')
                for i, amount, balance in [(1, '1000', '100'), (2, '200', '200')]]
    store.upsert_customers([customer])
    store.upsert_invoices(invoices)
    lines = tuple(DraftLine(i.number, i.due_date, i.balance, i.currency,
                            (DAY-i.due_date).days, i.reference, i.iban) for i in invoices)
    draft = render_draft(kind, lang, RenderContext(customer, lines, 'Demo Creditor', DAY))
    store.create_draft(draft)
    return store, draft, invoices


def swap(text, a, b):
    assert a in text and b in text
    return text.replace(a, '__SWAP__').replace(b, a).replace('__SWAP__', b)


def mutation(draft, invoices, what):
    a, b = invoices
    if what == 'amount':
        return swap(draft.body, format_money(a.balance, 'EUR', draft.language), format_money(b.balance, 'EUR', draft.language))
    if what == 'date':
        return swap(draft.body, format_date(a.due_date, draft.language), format_date(b.due_date, draft.language))
    if what == 'reference':
        return swap(draft.body, a.reference, b.reference)
    if what == 'invoice':
        return swap(draft.body, a.number, b.number)
    if what == 'gross':
        return draft.body + '\nPlease pay ' + format_money(a.amount, 'EUR', draft.language) + '.\n'
    row = next(line for line in draft.body.splitlines() if a.number in line)
    if what == 'duplicate':
        return draft.body + '\n' + row
    return draft.body.replace(row, row + ' instead of the amount owed')


@pytest.mark.parametrize('lang', ['en', 'cs'])
@pytest.mark.parametrize('what', ['amount', 'date', 'reference', 'invoice', 'gross', 'duplicate', 'role'])
def test_financial_lines_changed_refuses_reassigned_and_added_demands(lang, what):
    store, draft, invoices = setup(lang)
    changed = mutation(draft, invoices, what)
    result = revise(draft, changed, ledger_figures(invoices))
    assert not result.ok and result.draft == draft
    assert any('financial lines changed' in line for line in result.lock.diff)
    with pytest.raises(ConflictError, match='financial lines changed'):
        store.replace_draft(with_hash(replace(draft, body=changed)))
    assert store.get_draft(draft.id).body == draft.body
    assert store.approval(draft.id) is None and store.list_sends() == []


@pytest.mark.parametrize('gate', ['approve', 'send'])
@pytest.mark.parametrize('what', ['amount', 'date', 'reference', 'gross', 'duplicate'])
def test_approval_and_send_recheck_persisted_financial_baseline(gate, what, tmp_path):
    path = tmp_path / 'ledger.sqlite'
    store, draft, invoices = setup(path=path)
    store.approve_draft(draft.id, draft.hash)
    bad = with_hash(replace(draft, body=mutation(draft, invoices, what)))
    # Simulate a malformed revision that bypassed the public revision function.
    store._conn.execute('UPDATE drafts SET body=?, hash=? WHERE id=?', (bad.body, bad.hash, bad.id))
    store._conn.execute('UPDATE approvals SET hash=? WHERE draft_id=?', (bad.hash, bad.id))
    store.close()
    with Store(path, today=lambda: DAY) as store:
        action = store.approve_draft if gate == 'approve' else lambda i, h: store.reserve_send(i, h, 'eml')
        with pytest.raises(ConflictError, match='financial lines changed'):
            action(bad.id, bad.hash)
        assert store.approval(bad.id) is None
        assert store.get_draft(bad.id).status in ('held', 'rejected')
        assert store.list_sends() == []


@pytest.mark.parametrize('lang', ['en', 'cs'])
@pytest.mark.parametrize('kind', ['stage07', 'stage14', 'stage21', 'stage28', 'thanks', 'escalation'])
def test_prose_and_whole_row_reordering_preserve_bindings(lang, kind):
    store, draft, invoices = setup(lang, kind)
    body = 'Hello!\n' + draft.body + '\nThank you.\n'
    rows = [line for line in body.splitlines() if any(i.number in line for i in invoices)]
    assert len(rows) == 2
    body = swap(body, *rows)
    result = revise(draft, body, ledger_figures(invoices))
    assert result.ok, result.lock.diff
    store.replace_draft(result.draft)
    assert store.get_draft(draft.id).body == body
    # Repeat from the persisted original baseline, not a mutable revision baseline.
    next_result = revise(result.draft, 'Good morning!\n' + body, ledger_figures(invoices))
    assert next_result.ok
    store.replace_draft(next_result.draft)


def test_good_revision_can_be_approved_and_reserved():
    store, draft, invoices = setup()
    result = revise(draft, 'Thank you for your help.\n' + draft.body, ledger_figures(invoices))
    assert result.ok
    store.replace_draft(result.draft)
    store.approve_draft(draft.id, result.draft.hash)
    assert store.reserve_send(draft.id, result.draft.hash, 'eml')
    assert len(store.list_sends()) == 1  # reservation only; no delivery


def test_financial_baseline_missing_fails_closed_and_can_be_regenerated():
    store, draft, invoices = setup()
    store.approve_draft(draft.id, draft.hash)
    store._conn.execute('DELETE FROM draft_text_basis WHERE draft_id=?', (draft.id,))
    with pytest.raises(ConflictError, match='financial baseline missing'):
        store.reserve_send(draft.id, draft.hash, 'eml')
    assert store.approval(draft.id) is None and store.list_sends() == []
    assert store.create_draft(draft)
    store.approve_draft(draft.id, draft.hash)
    assert store.reserve_send(draft.id, draft.hash, 'eml')


def test_subject_cannot_gain_a_previously_allowed_gross_demand():
    store, draft, invoices = setup()
    result = revise(draft, draft.body, ledger_figures(invoices), new_subject='Pay EUR 1,000.00: ' + draft.subject)
    assert not result.ok
    with pytest.raises(ConflictError, match='financial lines changed'):
        store.replace_draft(with_hash(replace(draft, subject='Pay EUR 1,000.00: ' + draft.subject)))
