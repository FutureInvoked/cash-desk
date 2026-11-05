# Cash Desk engine (`cashdesk/`)

The engine behind the Cash Desk plugin: import invoice and bank CSV exports,
match payments to invoices, plan graduated reminders in English and Czech,
render drafts, and enforce **Figure Lock**: the model may rewrite a draft's
words, but every amount, invoice number, due date, IBAN and payment reference
must match the ledger exactly.

Ground rules, all enforced by the test suite:

- Python standard library only, Python 3.11 or newer. No Hermes imports, no
  network, no `print` (logging only), no reading of files other than the paths
  a caller passes in (templates are package data).
- Money is `decimal.Decimal` quantised to cents everywhere; `float` is
  refused. Totals are kept per currency and never added across currencies.
- Dataclasses for records, plain functions for logic, one thin `Store` over
  `sqlite3`. Every store write is idempotent or refuses with a typed error.
- Relative imports inside the package (`from .money import ...`), because
  Hermes loads the plugin folder as a package and `cashdesk` as a subpackage.

Run the suite from the repository root:

```bash
python -m pytest -q
```

Every public name below is re-exported from `cashdesk`
(`from cashdesk import match_payments`).

## The flow the plugin runs

```
read_customers / read_invoices / read_bank_csv      CSV text or path -> ImportResult
Store.ingest(result)                                  refuses the same file twice
match_payments(Store.list_invoices(), Store.unapplied_payments(), Store.list_customers(), as_of)
Store.apply_match(match)                              once per payment and invoice
plan_followups(invoices, customers, applied, state, as_of, sender=...)
Store.create_drafts(plan.all_drafts); Store.save_chase_state(plan.state)
revise(draft, new_text, ledger_figures(invoices))     Figure Lock: new hash only when it passes
Store.approve_draft(id, hash)
Store.reserve_send(id, hash, mode) -> key; send the mail; Store.confirm_send(key) or Store.fail_send(key)
morning_brief(store, as_of, lang); Store.record_brief()
```

Timestamps are UTC; a naive `datetime` passed anywhere is taken as UTC.

## `model.py` - records

- `Customer(id, name, language="en", email="", contact_name="", on_hold=False, hold_reason="")` - frozen; language is `en` or `cs`; `first_name` property.
- `Invoice(number, customer_id, issue_date, due_date, amount, currency, balance, status="open", reference="", iban="", paid_date=None, note="")` - status is `open|paid|void|disputed`; amounts quantised; `days_overdue(as_of)`, `is_open`, `is_paid`, `is_void`, `is_disputed`.
- `Payment(id, date, amount, currency, reference="", counterparty="", memo="", source="bank")` - `is_outgoing`, `text` (reference and memo).
- `Match(payment_id, invoice_number, amount, currency, method, confidence, reason="")` - one application of money to an invoice.
- `Figure(kind, canonical, raw="", line=0, valid=True)` - a ledger figure; equality ignores `raw` and `line`. Constructors give the canonical form shared by renderer and lock: `Figure.money(value, currency)` (`EUR 1250.00`), `Figure.date(d)` (ISO), `Figure.invoice(no)` and `Figure.reference(ref)` (letters and digits, upper-case), `Figure.iban(s)` (spaceless, upper-case). `amount` and `currency` properties read a money figure back.
- `Draft(id, kind, customer_id, invoice_numbers, language, subject, body, recipients, created_at, status="pending", hash="", figures=())` - kind is `stage07|stage14|stage21|stage28|thanks|escalation`, status `pending|approved|sent|held|rejected`; `figures` is the required figure set; `text` is subject plus body.
- `to_money(value, what="amount") -> Decimal` - coerce to cents; a float is a `TypeError`.
- Constants: `LANGUAGES`, `INVOICE_STATUSES`, `DRAFT_KINDS`, `DRAFT_STATUSES`, `FIGURE_KINDS`.

## `money.py` - Decimal money

- `parse_amount(text, lang=None, *, strict=False) -> Amount | None` - reads `1,250.00`, `1250.00`, `€1,250`, `1 250,00 Kč`, `1.250,00 €`, `CZK 1 250`, `12 500,-`, no-break spaces, a trailing minus (`1.200,00-`) and accounting parentheses (`(1,200.00)`) as negative. A lone separator before exactly three digits is a thousands separator only after a group of one to three digits, and then `lang` decides (`1.250`, `1,250`: English comma thousands, dot decimal; Czech the other way round; no language: thousands); after a longer group it is a decimal point (`1250.500` is 1250.50). More decimals round half up, except with `strict=True` (the importers), where more than two non-zero decimals (`1250.505`) are refused. `Amount(value: Decimal, currency: str | None)`.
- `format_money(amount, currency, lang="en") -> str` - house style: `EUR 1,200.00`; Czech `1 200,00 Kč` / `500,00 EUR` with no-break spaces.
- `quantize(value) -> Decimal` - two places, half up.
- `sum_by_currency(pairs) -> dict[str, Decimal]` and `format_totals(totals, lang) -> str` - per-currency totals, joined with ` + `, `-` when empty.
- `cs_days(n) -> str` - `1 den`, `2 dny`, `5 dní`.

## `dates.py` - dates

- `parse_date(text, lang="en", order=None) -> date | None` - ISO, `5. 11. 2026`, `5.11.2026` and `05.11.26` (day first in both languages), slashed or dashed (`11/05/2026`, `05-11-2026`) by `order` (`dmy`/`mdy`) or else by language (`11/05/2026` is 5 November in English, 11 May in Czech), long forms `5 November 2026`, `November 5, 2026`, `5. listopadu 2026`.
- `infer_date_order(texts) -> "dmy" | "mdy" | None` - the order a whole file's slashed dates prove (a first number above 12 is day-first, a second above 12 month-first); None when no date reads differently either way; raises `DateOrderError` (a `ValueError`) when the dates prove both orders, or when they all read both ways (`03/04/2026`). `numeric_parts(text)`, `DATE_ORDERS`.
- `format_date(d, lang="en") -> str` - English ISO `2026-11-05`; Czech `5. 11. 2026`.

## `importers.py` - CSV in

Every reader finds its header below report titles and bank preambles (the first of the first 40 lines that carries the needed columns); row numbers are physical line numbers. Rows that cannot be chased are **skipped with a code**, not errors, so a normal export imports without `allow_errors`; real errors still refuse the file.

- `read_invoices(source, text_or_path, *, default_currency="EUR", date_order=None) -> ImportResult` - `source` is one of `SOURCES` (`generic`, `xero`, `quickbooks`, `stripe`). Exports that carry contact details also yield `customers`.
  - Xero: headers may start with `*`; balance is `InvoiceAmountDue`, else `Total - InvoiceAmountPaid`; one row per line item collapses into one invoice when contact, dates, totals, amount due/paid, currency, status, type and reference agree, otherwise that invoice is a row error; `ACCRECCREDIT` / `Sales credit note` rows are credit notes; `AUTHORISED` is open, `PAID` paid, `VOIDED` void (both imported, never chased, so a re-import closes them); `DRAFT`, `SUBMITTED` and `DELETED` are skipped; `ACCPAY` bills are skipped.
  - Stripe: balance is `Amount Remaining`, else `Amount Due`; lowercase currencies; `open`, `paid`, `void`, `uncollectible` (void); `draft` and `deleted` skipped; an invoice without a due date (charged automatically) is skipped as `no_due_date`.
  - QuickBooks Online (Invoice List, A/R Aging Detail): title lines, group headings, `Total for ...`, `TOTAL` and the footer are not rows; `Credit Memo` rows and payments with an open balance become credit notes (`credit_note`, `unapplied_payment`); an applied payment or another non-invoice type with nothing open is skipped as `not_invoice`, one with a positive open balance too, with a "not chased" message.
  - Generic: a `type` column with `credit note`, `credit memo`, `dobropis` or `Gutschrift`, or any negative amount, makes a credit note; `draft` is skipped.
  - Every source: a paid or void row without a due date is skipped (`no_due_date`); an open invoice without one is a row error; a due date before the issue date is a row error; amounts are parsed with `strict=True`.
  - `date_order` is `dmy` or `mdy`; `None` (or `auto`) decides from every slashed date in the file and refuses the file (one error) when they all read both ways or prove both orders.
  - Customer ids from names (Xero, QuickBooks, Stripe without a customer id) are `customer_key(name)`; an explicit customer id column is used as it is. An unsupported language is read as `en` with a warning.
- `read_bank_csv(text_or_path, *, default_currency="EUR", lang=None, date_order=None) -> ImportResult` - English, Czech, German and Dutch headers, `,` `;` or tab, BOM, decimal commas, trailing minus, a time of day after the date, `yyyymmdd` dates, the currency in an amount header (`Betrag (EUR)`). The amount is a signed column, or separate `Debit`/`Credit` (`Money out`/`Money in`, `Paid out`/`Paid in`, `Soll`/`Haben`) columns; a direction column (`CdtDbtInd` CRDT/DBIT, DR/CR, Debit/Credit, Soll/Haben S/H, Af/Bij) signs an unsigned amount, so only incoming credits are positive (debits are negative lines, as in a signed statement). With a status column only booked, completed, posted, settled or cleared lines count; pending, failed, declined, reverted and similar lines are skipped (`not_booked`). A direction or status value the reader does not understand refuses the whole file. Rows without an id get stable content-derived ids; `date_order` as for invoices.
- `read_customers(text_or_path) -> ImportResult` - `customer_id, name, language, email, contact_name, on_hold, hold_reason`; a language other than `en`/`cs` is read as `en` with a warning.
- `ImportResult(kind, source, records, customers, errors, skipped, fingerprint, rows, columns, credit_notes=(), warnings=(), date_order=None)` - `errors` are `RowError(row, message, raw, code="")` per bad row (never raised); `skipped` are rows deliberately left out, each with a `code` (`draft`, `deleted`, `no_due_date`, `not_invoice`, `bill`, `settled_credit`, `not_booked`) and `skipped_counts` gives `{code: n}`; `credit_notes` are `CreditNote(number, customer_id, customer_name, date, amount, currency, kind, row, detail)` with `amount` the open credit as a positive number and `kind` `credit_note` or `unapplied_payment` (never chased; a person applies them); `warnings` are `RowError`s with code `language` or `overpaid`; `date_order` is the order used for slashed dates (None when none needed deciding); `ok` is "no errors".
- `fingerprint(text) -> str` - sha256 of the content with BOM, line endings and trailing blanks normalised; `Store.ingest` refuses a fingerprint it has seen.
- Helpers: `customer_key(name)` (slug plus 8 hex of a sha256 of the casefolded name: `Müller GmbH` and `Muller GmbH` stay apart, `北京测试有限公司` gets `customer-…`), `slugify(name)`, `clean_reference(text)` (`VS 20261104` -> `20261104`).

## `matching.py` - payments to invoices

- `match_payments(invoices, payments, customers=None, as_of=None) -> MatchResult` - a payment is **applied** only when its reference names exactly one ledger invoice (number with any separators, or the variable symbol) in the same currency for exactly its open balance, from a payer whose name fits that invoice's customer, dated no more than three days before the invoice; otherwise it is reviewed as `PAYER` or `EARLY`.
- `payer_fits(customer_name, counterparty) -> bool` - no payer name, a close name, or a shared distinctive word (bank lines truncate: `BLUEBELL DESIGN ST`). Pass all invoices (paid and void too) so a payment against a settled invoice is explained. `customers` supply names for similarity scoring; with `as_of`, later-dated payments are queued.
- `MatchResult(applied, review, credit_notes, unmatched, skipped, duplicates, held_invoices)` - `review` items (`ReviewItem(payment, kind, detail, named, candidates, holds)`) carry one of `REVIEW_KINDS`: `MISSING`, `AMBIGUOUS`, `PARTIAL`, `OVERPAID`, `MULTI_INVOICE`, `CURRENCY`, `ALREADY_PAID`, `VOID`, `AFTER_AS_OF`; `credit_notes` are refunds (never applied, never a dispute); `unmatched` items have no candidate; `skipped` are outgoing non-refund lines (fees); `duplicates` repeat an id; `held_invoices` maps invoice number to the reason it should not be chased while a payment is in review. `totals()` gives `applied`, `review_in` and `out` per currency.
- `Candidate(invoice_number, customer_id, score, amount_score, name_score, date_score, balance, currency)` - up to three per item, scored by amount (exact, or within a twentieth of the balance), name similarity (`NAME_MIN` 0.6) and date window.
- Helpers: `normalise_name(s)` (accents, punctuation and legal suffixes removed), `name_similarity(a, b)`, `is_refund(text)`, `invoices_named(text, invoices)`.

## `chasing.py` - who gets what today

- `plan_followups(invoices, customers, payments_applied, state, as_of, *, sender, credit_notes=(), holds=None, replies=(), created_at=None, reminders_sent=()) -> FollowupPlan` - stages at 7, 14, 21 and 28 days overdue (`STAGES`); the customer's oldest overdue invoice sets the stage and one draft lists all their overdue invoices; the highest stage reached that was not drafted yet is drafted, so weekly runs skip none. Past `ESCALATE_AFTER` (30) days an invoice is escalated to the reviewer (internal note) once its stage-28 reminder exists; an invoice that is already past 30 days without one (a backlog on the first run, or a catch-up) gets the stage-28 customer reminder first and the escalation `ESCALATION_WAIT` (7) days later. Escalation is per invoice: an escalated invoice drops out of reminders, the customer's newer invoices keep their own schedule. `reminders_sent` (invoice number -> reminders really sent, or just the numbers) is the only source of history wording ("Second reminder", "This follows our earlier reminder"); with the default nothing claims an earlier reminder. Thank-you drafts follow `payments_applied` once per invoice. A disputed invoice, `Customer.on_hold`, a dispute reply or an open credit note (negative balance, not void) holds the whole customer; `holds` (from `MatchResult.held_invoices`) hold single invoices; `credit_notes` hold invoices and suppress their thank-you; void invoices are never chased; a customer without an e-mail is held and listed; without a contact name the draft opens "Hello,". `sender` signs every draft.
- `FollowupPlan(as_of, drafts, thanks, escalations, held, not_due, voided, already_drafted, replies, state)` - `all_drafts` joins the three draft tuples; `held` is `HeldItem(customer_id, invoice_numbers, reason, detail)` with reason in `dispute`, `on_hold`, `credit_note`, `payment_in_review`, `no_email`, `no_customer` (`no_contact_name` stays in `HOLD_REASONS` for old records but is no longer produced); `state` is the updated copy to save.
- `ChaseState(drafted, thanked)` - `drafted` is the highest reminder stage (or `escalation`) per invoice, `stage28@YYYY-MM-DD` when that reminder was drafted past 30 days (the date starts the escalation wait), `thanked` the `invoice:payment` keys already thanked; the two never mix, so a thank-you for a part payment cannot stop later reminders. `rank(no)`, `stage_of(no)`, `late_since(no)`, `record(no, stage, on=None)`, `record_thanks(no, payment_id)`, `was_thanked(no, payment_id)`, `copy()`, `to_json()`, `ChaseState.from_json(text)`.
- `classify_reply(text, lang="en") -> str` - `dispute`, `paid`, `promise` or `other` (questions and auto-replies), English and Czech words; `Reply(customer_id, date, text, lang="en")`.
- `stage_for(days) -> str | None`.

## `render.py` and `templates/` - drafts

- `render_draft(kind, lang, context) -> Draft` - fills `templates/<kind>.txt` or `<kind>_cs.txt` and returns a hashed `Draft` whose `figures` are exactly the figures inserted (amounts, per-currency totals, due dates, invoice numbers, references, IBANs). Fails with `TemplateError` instead of leaving a placeholder blank: no lines, no sender, an English template without a first name, an unknown placeholder.
- `RenderContext(customer, lines, sender, as_of, stages_drafted={}, created_at=None, draft_id=None, reminders_sent=0)` and `DraftLine(number, due_date, amount, currency, days_overdue=0, reference="", iban="")`.
- Templates: `stage07`, `stage14`, `stage21`, `stage28`, `thanks`, `escalation` (`KINDS`), each in English and Czech. Placeholders: `{count}`, `{total}`, `{first_name}`, `{greeting_hi}` / `{greeting_dear}` ("Hi Jana," / "Dear Jana," or "Hello," without a contact name), `{overdue_summary}` (subject: "invoice INV-1 is 7 days overdue" or "2 invoices overdue, the oldest by 9 days"), `{age}` (real days overdue of the oldest), `{history}` and `{reminder_lead}` (empty / "Reminder" unless `reminders_sent` > 0), `{invoice_lines}`, `{payment_details}`, `{sender}`, `{customer}`, `{stages}`. Czech drafts open with "Dobrý den," and never use a count placeholder.
- Helpers: `load_template(kind, lang)`, `fill_template(text, mapping)`, `invoice_line(line, lang)`, `payment_details(lines, lang)`, `format_iban(iban)`, `stage_label(stage, lang)`.

## `figure_lock.py` - the lock

- `extract_figures(text, lang="en") -> Figures` - every IBAN (country length and mod-97 checked; a failing one is extracted with `valid=False`), date in any supported form, amount in digits or words (`twelve hundred and fifty euros`, `tisíc dvě stě padesát korun`), and identifier (invoice numbers with letters; digit strings of four or more as references). Plain counts, years, short numbers, phone-number groups, e-mails and URLs are not figures. `Figures` iterates in text order and offers `of(kind)`.
- `unknown_figures(text, lang, allowed, kinds=("money", "iban", "invoice_no")) -> list[Figure]` - the figures of those kinds in free text that `allowed` does not explain, in text order. The plugin's reply guard uses it; dates and plain counts are not asked about.
- `check(text, lang, required, allowed) -> LockResult(ok, missing, extra, changed, diff, found)` - every `required` figure must be present; nothing outside `allowed` (plus `required`) may appear; `changed` pairs a required figure with the nearest wrong value (`EUR 1,250.00 -> EUR 1,520.00`, a swapped date, an invoice number one character off, an amount without its currency, an IBAN failing its checksum); `diff` is one short line per rejection with the line number (line 1 is the first line of `text`). An amount broken across a line break is rejected too.
- `revise(draft, new_text, allowed, new_subject=None) -> RevisionResult(ok, draft, lock)` - checks subject and body together against `draft.figures`; returns the draft with the new text and a new hash only when the lock passes, otherwise the original draft and the diff.
- `ledger_figures(invoices, extra=()) -> Figures` - every figure the ledger holds for these invoices (numbers, references, IBANs, amounts and balances, dates), plus `extra` such as the sender's own identifiers.
- Helpers: `iban_valid(s)`, `spelled_number(words, lang)`, `broken_amounts(text)`, `describe(figure)`.

## `approval.py` - what an approval covers

- `draft_hash(text, recipients, subject) -> str` - sha256 over the canonical subject, recipients (lower-cased, sorted) and text (NFC, `\n` line endings, no trailing whitespace).
- `idempotency_key(draft_id, draft_hash) -> str` - 32 hex characters naming one send of one exact draft.
- `verify_hash(draft, text, recipients, subject) -> bool` - constant-time compare against `draft.hash`.
- `with_hash(draft) -> Draft`, `canonical_text(text) -> str`.

## `brief.py` - the morning brief

- `morning_brief(store_or_snapshot, as_of, lang="en") -> str` - Markdown: customers on hold, payments applied since the previous brief (totals per currency), invoices newly overdue in the window, the approval queue with `STALE`, `STALE IN PART` and `DO NOT APPROVE` tags, and the open balance per currency. Reads only; the caller records the run with `Store.record_brief()`.
- `brief_window(day, since=None) -> (date, date)` - from the previous brief's day, or the previous business day (a Monday covers Friday to Sunday), up to yesterday.

## `store.py` - SQLite

`Store(path)` (use `":memory:"` in tests) over tables `customers`, `invoices`, `payments`, `matches`, `drafts`, `approvals`, `sends`, `imports`, `events`, `chase_state`, `briefs`. Each method runs in a transaction. Errors: `StoreError` with `DuplicateImportError`, `HashMismatchError`, `NotApprovedError`, `AlreadySentError`, `ConflictError`, `NotFoundError`.

- Imports: `ingest(result, at=None, allow_errors=False) -> dict` records the import and writes its records in one transaction (refuses a seen fingerprint and, unless `allow_errors`, a result with row errors; customers derived from an invoice export are written in `fill` mode); `record_import(result)`, `has_import(fingerprint)`, `list_imports() -> [ImportRecord]`.
- Ledger: `upsert_customers(customers, at=None, mode="replace")` (`replace`: name and language written, a blank e-mail or contact never clears a stored one; `fill`: only missing customers and blank fields; a hold in the store always survives, a hold on the record is applied), `upsert_invoices(invoices)`, `add_payments(payments) -> int` (known ids ignored), `unapplied_payments()` (payments with no applied match: the input for the next matching run), `apply_match(match, at=None) -> bool` (once per payment and invoice; reduces the balance, marks paid at zero), getters `get_customer`, `get_invoice`, `get_payment`, lists `list_customers`, `list_invoices(status=None, customer_id=None)`, `list_payments`, `list_matches`.
- Drafts: `create_draft(draft) -> bool` (the hash must cover the draft's own text; the same text again is a no-op, a different text under the same id refuses), `create_drafts(drafts) -> int`, `get_draft(id)`, `list_drafts(status=None, customer_id=None)`, `replace_draft(draft)` (text, recipients and hash may change, not kind, customer or invoices; a revision drops its approval; a sent draft cannot change), `reject_draft(id, reason="")`.
- Approval and send: `approve_draft(id, hash, approved_by="", at=None)` (the hash must be the stored one; held, sent and rejected drafts refuse; a customer on hold or with a disputed open invoice refuses; a reminder whose invoices are no longer all open is stale and refuses), `approval(id) -> Approval | None`, `reserve_send(id, hash, mode, at=None) -> str` (the same checks plus an approval for that hash; returns the idempotency key; a second reservation, in flight or done, is refused), `confirm_send(key)` (the draft becomes `sent`), `fail_send(key, reason)` (retry allowed under the same key), `record_send(id, hash, mode) -> str` (reserve and confirm in one step, for an `.eml` file), `list_sends() -> [SendRecord]` with `status` `reserved`, `sent` or `failed`.
- Holds: `set_hold(customer_id, reason)` moves the customer's pending and approved drafts to `held` and drops their approvals; `clear_hold(customer_id)` moves held drafts back to `pending` (they need a fresh approval). A hold arriving through `upsert_customers` does the same; a disputed invoice arriving through `upsert_invoices` blocks approval and send without touching draft rows.
- State and events: `chase_state() -> ChaseState`, `save_chase_state(state)`, `log_event(kind, ref="", detail=None, at=None)`, `events(limit=200) -> [Event]`.
- Snapshots: `status_snapshot(as_of) -> StatusSnapshot` (open balance by age bucket per currency via `bucket_for(days)`: `current`, `1-30`, `31-60`, `61-90`, `90+`; counts of pending, approved-unsent and held drafts; customers on hold or disputed; the last import; `to_dict()` for JSON), `brief_snapshot(as_of) -> BriefSnapshot` (payments applied since the previous brief, pending and held drafts, customers and invoices), `record_brief(at=None)`, `previous_brief_at()`.
- Records the store returns: `ImportRecord(fingerprint, source, kind, imported_at, rows, errors)`, `Approval(draft_id, hash, approved_at, approved_by)`, `SendRecord(idempotency_key, draft_id, hash, sent_at, mode, status, reserved_at, note)`, `Event(id, at, kind, ref, detail)`, `PaymentView(match, applied_at, payment, invoice, customer)`, `StatusSnapshot(as_of, open_by_bucket, open_count, awaiting_approval, approved_unsent, held_drafts, on_hold, last_import)` and `BriefSnapshot(as_of, previous_brief_at, payments, pending_drafts, held_drafts, customers, invoices)`.

## Versions

`ENGINE_VERSION` is the engine's own version string, reported by the plugin's status tool.
