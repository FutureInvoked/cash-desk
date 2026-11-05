# Changelog

All notable changes to Cash Desk. Versions follow [Semantic Versioning](https://semver.org/);
the version here, in `plugin.yaml` and in the catalog entry are always the same.

## 1.0.0 (unreleased)

First public release, built for Hermes Agent 0.22.6 and later.

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
  `/cashdesk approve <draft-id> [hash-prefix]`. Any change to the text drops the approval.
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
