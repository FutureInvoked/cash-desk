# Changelog

All notable changes to Cash Desk. Versions follow [Semantic Versioning](https://semver.org/);
the version here, in `plugin.yaml` and in the catalog entry are always the same.

## 1.1.0 (unreleased)

First public release. Everything below 1.0.0 was hardened after an independent judge-style
review, a bookkeeper's stress test with realistic exports, and live runs through the agent;
each break they found is now a test.

### Fixed
- A newer invoice export never undoes a payment Cash Desk applied (the lower balance is kept
  and the Activity list says so).
- Approving and sending re-check every invoice of a reminder against the balance and due date
  it was drafted with; a reminder that is no longer true is refused, replaced on the next
  draft run, and a newer stage replaces an older unsent reminder for the same invoice.
- A payment is applied automatically only when the payer's name fits the customer and it is
  not dated before the invoice (`PAYER`, `EARLY` review items).
- Reminders never claim an earlier reminder that was not sent, state the real age, and an old
  backlog invoice gets a customer reminder (the escalation note follows a week later). Weekly
  runs no longer skip a stage; escalation is per invoice; "Hello," when there is no contact name.
- Real exports: Xero amount due and line-item rows, Stripe amount remaining, QuickBooks report
  title/total rows and credit memos, bank files with debit/credit, direction and status
  columns, trailing minus, three-decimal amounts, unicode and near-identical customer names.
  Day/month order is decided from the whole file (or the `date_order` setting).
- Open credit notes hold that customer's reminders instead of a gross total being chased.
- Hermes's approval prompt names the reminder, customer, recipient, total and text hash; no
  send where Hermes would pass that prompt on its own (yolo, or an `approve` mode for
  single-query, cron or unattended runs).

### Added
- `/cashdesk review | apply | dismiss` and **Apply** / **Dismiss** on the Desktop page: a
  person places (all or part of) a payment waiting for review, or says it pays nothing.
- Figure Lock also refuses a rewording that adds a link, an email address, a percentage, or
  legal, settlement, interest or suspension terms. Measured on the demo: 74 of 74 edits caught.
- `cashdesk_reply` takes the model's reading (`kind`) and a `promised_date`, which pauses that
  customer's reminders until two days after it.
- `open_list` imports, contact lists matched by name, `ignore_balances_below` (default 10.00),
  `date_order`.
- `/cashdesk approve` needs the first characters of the hash the person read; on the Desktop
  page Approve works only once the text is open.

## 1.0.0 (tagged 2026-11-05, never published)

First tagged build, for Hermes Agent 0.22.6 and later.

### Added

- **Ten tools.** Read-only: `cashdesk_status`, `cashdesk_brief`, `cashdesk_queue`.
  Writing: `cashdesk_import`, `cashdesk_match`, `cashdesk_draft`, `cashdesk_revise`,
  `cashdesk_reply`, `cashdesk_hold`, `cashdesk_send`.
- **Imports** of a customers CSV, invoice exports (generic, Xero, QuickBooks, Stripe) and bank
  statements with English or Czech headers. Decimal money throughout. The same file is never
  imported twice.
- **Payment matching** that applies a payment only on an exact reference and amount; anything
  fuzzy goes to a review queue and is never applied.
- **Graduated reminders** at 7, 14, 21 and 28 days overdue in the customer's language (English
  or Czech), thank-you notes for payments received, and an internal escalation note past 30
  days. Customers on hold or in dispute get nothing.
- **Figure Lock.** The model may reword a draft freely, but every amount, invoice number, due
  date, IBAN and payment reference must match the ledger. A revision that changes one is
  rejected with a short diff, and the stored draft is left as it was.
  Measured: 72 of 72 single-digit edits to the demo drafts' figures are caught
  (`tests/plugin/test_mutations.py`).
- **Figure Lock on the agent's replies** (opt-in `guard_outgoing_messages`): through Hermes's
  `transform_llm_output` hook, a reply about a ledger customer that states an amount, IBAN or
  invoice number the ledger does not hold for that customer is withheld and replaced with a
  notice before Hermes stores or delivers it. Needs Hermes 0.22.6 for `hold_stream`.
- **Hash-bound approval.** A person approves the exact text on the Desktop page or with
  `/cashdesk approve <draft-id> <hash-prefix>`. Any change to the text drops the approval.
- **Two human gates before anything goes out.** The approval, then Hermes's own approval prompt
  on every `cashdesk_send` (rule key `cashdesk:send:<hash>`). Sending writes an `.eml` file by
  default; SMTP is opt-in and reads its password only through Hermes's secret scope.
- **Reply handling.** `cashdesk_reply` classifies a customer's reply (dispute, paid, promise,
  other); a dispute puts the customer on hold.
- **`/cashdesk` command** for people: `status`, `queue`, `approve`, `reject`, `hold`, `release`.
  The model has no tool for approving, rejecting or releasing.
- **Morning-brief blueprint** (`cash-desk:morning-brief`) on the Scheduled jobs page: payments
  received, newly overdue invoices, drafts awaiting approval and customers on hold, in English or
  Czech, on weekdays at a time you choose. It only reads.
- **Desktop page**: open balance by age bucket per currency, the approval queue with each
  draft's exact text and hash, customers on hold (including those with a disputed invoice),
  recent activity, a status-bar chip, and a **Load demo data** button on an empty ledger.
- **Demo data** in `demo/`: made-up companies on `.example` domains and public example IBANs,
  written for 10 November 2026 and moved to today whenever it is loaded, so every "N days
  overdue" is true on the day it runs.
- **Settings**: `sender_name`, `send_mode`, `smtp_from`, `reply_to`, `smtp_host`, `smtp_port`,
  `smtp_user`, `smtp_password` (secret), `eml_outbox`, `import_dirs`, `currency` (default
  EUR; the demo and files without a currency column use it) and `guard_outgoing_messages`.

### Refusals

- No tool that writes runs in a scheduled job, inside a delegated subagent, or when Hermes
  cannot say whether the run is scheduled. No send happens when Hermes approvals are switched
  off.
- A send is refused when no approval covers the draft's current text, when the stored text was
  changed outside Cash Desk after approval, when the draft was already sent, or when no From
  address is set.
- `cashdesk_import` reads only from the plugin's data folder, its `demo/` folder and the folders
  listed in `import_dirs`. Any other path is refused before it is opened.
- The README's "What it refuses" table lists every refusal with its message and its test; a test
  fails if the table and the code drift apart.
