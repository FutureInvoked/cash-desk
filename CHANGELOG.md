# Changelog

All notable changes to Cash Desk. Versions follow [Semantic Versioning](https://semver.org/);
the version here, in `plugin.yaml` and in the catalog entry are always the same.

## 1.2.0 (release candidate, not yet tagged)

- The morning brief lists payments waiting for review and the reminders they pause, so a held
  customer is visible without opening the Desktop page. `cashdesk_revise` now submits edits the
  user asks for and lets Figure Lock decide, so the user sees its exact diff instead of a model's
  refusal. Screenshots re-shot on 1.2.0.

- Review fixes: a refused bank statement is now a hold on reminders (drafting, approval and
  sending) that only that file's clean import or `bank_checked=true` clears, and path typos do
  not set it; thank-yous name an open balance only when nothing is about to settle it, and two
  part payments to one invoice no longer crash drafting; a customers file without a language column
  keeps existing languages; own sort codes keep two accounts apart; truncated "…COR" names fit;
  Figure Lock adds Czech terms and lets "no compensation" and "your other invoices" through.

- A thank-you for a part payment also says what is still open on the invoice (English and Czech).

- New `default_language` setting for customers imported from files without a language column, so a
  Czech business's Xero/QuickBooks/Stripe customers get Czech reminders.

- "Corp" and "Corporation" are one legal form; a customer filed as "Smith, John" matches a bank
  line from "JOHN SMITH".

- Figure Lock also refuses "accrue", spelled-out percentages, statutory compensation, another
  account's balance, "due immediately" and telling other suppliers; README and PR describe it as a
  list of known phrases, with the person's reading as the final check.

- A refused bank statement stops reminder drafting until a statement imports cleanly (or the person
  confirms nothing is missing with `bank_checked=true`).

- Fio banka statements with their account preamble import: a row's BIC or bank code is the
  payer's bank, not the statement's.

- Review fixes: the partial-export guard no longer recounts invoices already held; `payment_iban`
  follows the setting at every ledger open (a change withdraws approvals of the old account); payer
  names keep their spaces (KINGS WAY is not KINGSWAY); Figure Lock passes "second payment reminder",
  "so you do not pay twice" and refuses curly-apostrophe and Czech diversions; badges accept
  totals and received amounts; escalation needs the 28-day reminder itself to have been sent.

- The Desktop's per-figure badge shows whether today's ledger still holds each stated figure.

- The internal escalation note follows only a 28-day reminder that was actually sent.

- The banner heads the README; the `/cashdesk` hint is one short line; the disclosures say the
  plugin reads Hermes's `config.yaml` to check approvals are on.

- An invoice export that would hold more than half of a source's open invoices asks whether it
  is a complete list (`open_list=true`) or partial (`open_list=false`) instead of silencing chasing.

- New `payment_iban` setting: reminders for invoices whose export has no IBAN (Xero, QuickBooks,
  Stripe) tell the customer where to pay, and Figure Lock holds it.

- Payer names match as banks write them (ZOES CAFE LIMITED, OBRIEN, MUELLER UND SOEHNE, LODZ).
  Figure Lock also refuses "our bank account has changed", "do not use the IBAN above", "we have
  moved banks, ring us before you pay" and "pay twice".

- An SMTP send that reached some recipients counts as sent (naming who was refused), so a retry
  never mails anyone twice; `/cashdesk send-status <draft> sent|failed` resolves a send left in
  progress when the gateway stopped mid-send.

- Review fixes: a reply pause only ever extends a dated promise; a payer read from a Chase ACH
  line no longer switches off description matching for the rest of the statement; Figure Lock
  greetings allow surnames and "and team" but refuse re-addressing, and its stoppage, immediate,
  new-account and age patterns no longer catch polite lines (and catch "IBAN has changed",
  "cease supply", "right away", "mid-December").

- US ACH lines (Chase and others) give their payer from `ORIG CO NAME:` in the description.

- Lines of one statement with different payers and references are never flagged as one receipt
  (a subscription business's same-day 99.00 payments).

- One transfer naming several invoices of one customer whose open balances add up to it exactly
  is applied to each; anything else still waits for a person.

- The send gate says why an approval was withdrawn (a hold, a dispute, changed figures); an
  internal escalation note can no longer be approved and then fail to send; "O'Brien & Sons Ltd"
  matches "O'BRIEN AND SONS LIMITED".

- `/cashdesk show <draft-id>` prints the exact text and ends with its approve command; the queue and
  a bare approve no longer hand out the hash. Figure Lock also refuses collections, work stoppage,
  part-payment offers, "immediately", changed bank details, word ages and re-addressed greetings.

- "We already paid" pauses a customer's reminders for five days and a promise without a date for
  a week. A part-paid status without a balance column, and a bank statement with no currency
  (unless you say the account's currency), are refused instead of guessed.

- Review fixes: a third rolling bank download imports; equal payments whose raw lines differ
  (running balance) or that share only a boundary day still ask; the description payer rule needs
  the name first, its own legal form and a file with no payer column; holds reach every address in
  a multi-address cell; the bank warning uses the newest booking date; Figure Lock no longer
  refuses "separate", "this week" or "no fees".

- A payer spelled with umlauts written out ("MUELLER GMBH" for Müller GmbH) matches.

- Re-importing an exact copy of an earlier invoice export is still refused as a duplicate, but the
  invoices it lists are released from "not in the latest export".

- QuickBooks statuses "Overdue 14 days", "Due in 5 days" and credit memos marked "Unapplied"
  import instead of refusing the file.

- An invoice listed in another invoice's reminder before its own first stage counts as reminded,
  so it never gets a gentler "7 days overdue" mail after an urgent one.

- A contact cell with several addresses ("a@x; b@x") sends to each of them; the demo README
  says Birchfield is held while its receipt waits for review.

- Drafting reminders says so when the newest bank statement is more than a week old or missing
  (for example because this week's file was refused).

- `/cashdesk settlements` proposes the usual reconcile command and figure when the latest export
  already includes the payments applied here.

- Overlapping weekly bank downloads without transaction IDs no longer raise a question for every
  repeated line: a line found exactly once in each of two downloads is one receipt (logged).

- QuickBooks Online's "Open Invoices" report, grouped by customer, imports.

- A send that Cash Desk would refuse (no approval of the exact text, a new hold, changed figures,
  stale age) is blocked before Hermes asks a person to allow it.

- Figure Lock also refuses an added date, day count, payment channel (PayPal...) or fee, and its
  refusal says which kind of change it refused. `/cashdesk status` no longer repeats the identity
  line; the banner no longer says "reword freely".

- A `date_order` setting no longer silently decides a file whose dates read both ways when the
  other order is clearly the recent one (a US export read day-first claimed 267 days overdue);
  the import asks for the order of that file.

- Revolut Business statements import (`Date completed (UTC)`, `Payment currency`).

- Bank files with no payer column (most UK and US downloads) match automatically when the
  reference and amount are exact and the description names exactly one customer in full.

- After a part payment, reminders say the invoice is not fully paid instead of "we have not yet
  received payment"; more dispute wording ("not what we agreed", "we cancelled the order",
  "cena nesedí") is recognised when the model gives no label.

- Two customer records behind one email address share holds and disputes, and only one
  reminder can be approved for an address within three days.

- `/cashdesk help` lists every command with one line each; an unknown command shows it. The
  README is short again: the full refusal table is in `docs/refusals.md`, reconciliation details in
  `docs/reconciliation.md`.

- A reminder sent more than six days after it was written is refused (its days-overdue figure
  would be untrue and the next stage due) and redrafted on the next run; a Monday draft approved
  on Thursday still goes. Thank-you notes are not affected.

- A pending incoming bank line that names an invoice for exactly its open balance pauses that
  invoice's reminders for a week (or until it books); it is never counted as cash.

- Xero `PlannedDate` pauses reminders until two days after the expected payment date, and an
  invoice marked `Not sent` is not reminded.

- Two open invoices that look like one debt imported twice (same amount, same customer or
  address, numbers or reference equal apart from case, separators, prefix and zeros) are held;
  `/cashdesk not-duplicate` releases a separate pair.

- Imports through the plugin now hold an open invoice that the next export from the same source
  leaves out (it was probably paid) instead of chasing it; `open_list: false` marks a deliberately
  partial file, and `/cashdesk still-open <invoice> <why>` chases a held invoice again.

- `cashdesk_match` refuses a future `as_of`, so money dated after today is never applied early.
  An unmatched incoming receipt that names a customer in its payer, reference or memo (a director
  paying, no payer column, short by bank fees) holds that customer's reminders until it is resolved.

- A receipt for exactly the amount an invoice export already shows as paid is not applied
  automatically: it may be the payment the export already counts. It waits for Apply or Dismiss,
  and the invoice's reminders and thank-yous wait with it.

- Figure Lock also refuses a rewording that adds a new payee ("payable to", "in the name of"),
  a new deadline ("within 24 hours", "by Friday", "immediately") or credit-reporting threats.

- Xero `AmountCredited` is read: a partly credited invoice (Total - Paid - Credited = Amount Due)
  imports with its real amount due instead of refusing the whole file.

- `cashdesk_draft` writes drafts for today only; an `as_of` for another day is refused, so the
  agent cannot date a reminder into a later, harsher stage (status and brief still accept a date).

- An empty cell in a present balance/open-balance column (no paid/void status, no amount paid)
  is a row error, not the full amount owed: the invoice is not chased and an existing one is held
  until the cell is filled in. So are an empty amount-paid cell and a paid date with no balance or
  status. More balance column names are read (Balance Due, Amount Outstanding, Open Amount,
  Remaining...); any other balance-looking column refuses the file. Only exports with no balance,
  amount-paid or paid-date evidence at all read the full amount as owed.

- Read bank direction and lifecycle kept in descriptive columns: an exact DEBIT/CREDIT
  (DR/CR, Soll/Haben) in Type/Transaction Type signs an unsigned amount; Sparkasse
  'Umsatz vorgemerkt' in Info, Buchungsstatus or Umsatzstatus is pending, not cash; other debit or
  lifecycle wording beside an unsigned amount refuses the file instead of applying money paid out.

- Bind customer-facing draft authority to original contact snapshots; revoke unsent
  reminders and thanks on contact changes, recheck recipients at reservation, preserve
  sent history, and distinguish explicit blank from omitted authoritative contact columns.

- Retain recognized payer/customer legal forms for automatic matching and writes; quarantine contradictory historical automatic allocations without financial undo, with visible snapshot verification.

- Require whole reference identifiers, complete known/unknown remittance evidence and
  invoice/payment-reference roles before automatic allocation. Support repeated separators;
  reject embedded numeric substrings and order/deposit ambiguity. Preserve unknown targets,
  recheck automatic writes and quarantine uncertain historical automatic allocations through
  human snapshot verification without rewriting balances, matches or sent history.

- Establish bank native currency from agreeing statement, amount-header, row and inline
  evidence before receipt identity. Persist raw currency provenance and explicit-default
  warnings; retain partial conflicts and historical raw-row currency changes as authority
  holds, with human snapshot verification and no financial or sent-history undo.

- Reconcile bank payer/remittance aliases and repeated normalized headers before payment
  identity construction. Retain explicit partial-import conflicts as raw whole-ledger review
  holds through allocation, collection and acknowledgement; add visible questions and human
  snapshot verification without changing balances, allocations, sent history or old approvals.

- Reconcile invoice status/type and Stripe collection aliases before skipping or collapsing
  rows; reject conflicting/unknown/duplicate lifecycle evidence. Preserve complete failed Xero
  groups and raw partial-import questions with conservative customer/unknown-scope holds.
  Retain known-document unissued observations; recover with verified source rows or snapshots
  without restoring approval or rewriting financial/sent history.

- Preserve authoritative customer IDs across invoice/contact sources instead of merging equal
  names. Retain raw explicit/derived provenance, quarantine uncertain old bindings and revoke
  affected collection/acknowledgement authority without rewriting financial or sent history.
  Add visible identity review and human snapshot-bound verification; limit owner reassignment
  to wholly unpaid sales with no allocation/send history.

- Retain reference-free/separately identified bank returns as durable review evidence,
  conservatively pause affected reminders and acknowledgements through approval/send,
  and preserve allocation/balance/sent history. Show return questions in commands/Desktop;
  add snapshot-bound human verification for unrelated observations without automatic undo.

- Preserve exported sale overpayments as customer-surplus review evidence, pause customer
  collection/acknowledgements and revoke approvals without automatic netting or match undo.
  Retain raw/history across closures/reimports and add human snapshot-bound verification
  plus conservative stored-basis/event migration.

- Reconcile every populated bank lifecycle/status alias and reject duplicate status
  headers, unknown evidence and conflicting states before any row is accepted.
  Status refusals cannot be bypassed through partial-import allow_errors.

- Separate Stripe Paid booleans from payment dates, reconcile lifecycle evidence, and
  retain partial-import invoice failures as review holds through matching/approval/send.
  Add raw-error review and audited snapshot-bound verification without restoring approval.

- Scope bank transaction identity by explicit statement account, retain conflicting and
  non-booked observations, and quarantine affected receipts and drafts without silent cash
  loss or allocation reversal. Add human snapshot-bound evidence recovery and legacy replay.

- Reconcile invoice gross/paid/remaining and currency evidence across populated aliases,
  headers and line items. Refuse duplicate financial headers and contradictory monetary
  evidence before ingestion; preserve supported partials, credits, void and Stripe layouts.

- Protect plausible actual payer debt during unresolved receipt review, including partial
  receipts and equal-amount candidates beyond the three displayed suggestions. Show the
  affected invoices and resolution action; enforce holds through approval and send.

- Require a known, unambiguous normalized payer name or bounded bank-name truncation for
  automatic allocation. Missing names, fuzzy/shared-word matches and ambiguous identities
  require review. Preserve non-Latin letters/marks and broad protective overlap holds;
  keep bank descriptions as evidence. Document the existing three-day booking-date grace.

- Retain paid/zero and void invoice updates without due dates. Verify source and document
  identity, preserve known historical fields, revoke unsent approvals, and keep unmatched
  terminal evidence so stale open exports cannot reopen it. Never invent due dates.

- Preserve drafted financial lines and their invoice/amount/date/reference relationships
  through revisions, approval and send reservation. Allow surrounding prose edits and
  whole-row reordering, but refuse additional financial demands even for ledger amounts.
  Persist the original text baseline; regenerate older drafts without a baseline.

- Map credits and unapplied receipts to the same customer as invoices; retain numbered
  paid/zero/void credit updates so weekly re-imports close old holds. Guard document/source
  identity, preserve gross and remaining credit separately, and revoke unsent approvals.
  Legacy orphan credits pause plausible owners until verified by a fresh export. Anonymous
  historical credit observations remain held rather than being linked by row position.

- Refuse contradictory bank amount evidence before ingestion. Check every populated
  Amount/Debit/Credit and direction column, preserve signed refunds, and reject negative
  Debit/Credit magnitudes, explicit positive/debit conflicts, mismatched values/currencies
  and duplicate financial headers instead of inventing incoming receipts.

- Quarantine possible overlapping receipt observations across applied, dismissed and pending
  history. Preserve no-ID rows from distinct exports; require an audited, snapshot-bound human
  same/distinct decision before further allocation. Keep uncertain row values out of confident
  cash totals, pause affected approvals/reminders, and expose review through Desktop/commands.
  Preserve all financial/sent history; historical pairs with allocations on both sides remain
  paused for a separate audited correction instead of silently undoing money.

- Restore Desktop module loading after an unescaped apostrophe in the demo message.
  CI checks real ESM syntax; the Python suite also loads the actual module and checks
  page/chip registration, read-before-approve controls and empty/loading/error states
  using isolated SDK/React fixtures without taking any approval action.

- Preserve imported settlements separately from local bank allocations, preventing phantom
  reopened debt on stale exports. Unknown legacy bases and ambiguous changed settlements
  pause reminders and allocations until snapshot-checked human reconciliation; local matches
  and sent history remain intact.

- Planning, approval and send reservation now share current eligibility: new promises,
  unresolved receipts, missing open-export invoices and credits attached to the customer
  invalidate earlier approvals. Old text remains visible with a warning and needs a fresh
  draft and approval after the reason is resolved.
- Per-invoice approval basis includes IBAN, payment reference and customer ownership as
  well as balance/currency/due date. Legacy unsent drafts without that basis are invalidated;
  migrations do not infer their old bank details from current invoices or change sent history.
- Revocations survive a refused approval/send transaction; planning and its persisted basis
  share one ledger snapshot. Desktop and slash-command approvals use the same profile clock
  and minimum-balance setting. Activity explains the pause and the next action.

## 1.1.0 (tagged 2026-11-05)

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
