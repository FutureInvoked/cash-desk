# What Cash Desk refuses

Every row is a real message from the plugin and the test that checks it; `tests/plugin/test_readme_refusals.py` fails if this table and the code drift apart.

<!-- refusals:start -->
| Situation | What Cash Desk answers | Test |
|---|---|---|
| A scheduled (cron) run calls a tool that writes | `from a scheduled job; scheduled runs only read` | `test_gate_blocks_writes_in_cron_and_escalates_sends` |
| Hermes cannot say whether this is a scheduled run | `cannot tell whether this is a scheduled run` | `test_gate_blocks_writes_in_cron_and_escalates_sends` |
| A delegated subagent calls a tool that writes | `inside a delegated subagent` | `test_gate_blocks_writes_inside_a_subagent` |
| Hermes approvals are switched off in the profile | `Hermes approvals are switched off in this profile` | `test_gate_blocks_sends_when_host_approvals_are_off` |
| Every send, even of an approved draft: Hermes asks a person to allow it, bound to the draft's hash | `cashdesk:send:` | `test_hermes_prompt_names_the_reminder_recipient_total_and_hash` |
| A send Cash Desk would refuse anyway (no approval of this exact text, a hold, changed figures, stale): blocked before Hermes asks anyone | `Cash Desk will not send` | `test_gate_blocks_writes_in_cron_and_escalates_sends` |
| A send of a draft nobody approved, or whose text was revised after approval | `no approval covers this exact text` | `test_send_requires_an_approval_of_the_exact_text_and_writes_once` |
| The stored text was edited outside Cash Desk after approval | `does not match its approved hash` | `test_send_refuses_a_draft_edited_in_the_ledger_after_approval` |
| `/cashdesk approve` with the hash of an older version of the text | `Re-read the draft before approving` | `test_approve_refuses_a_stale_hash_prefix` |
| A revision changes an amount, IBAN, date, invoice number or reference | `Figure Lock rejected the revision` | `test_figure_lock_accepts_rewording_and_rejects_changed_figures` |
| The same file is imported a second time (duplicate) | `already imported` | `test_import_refuses_bad_input_and_duplicates` |
| An import path outside the allowed folders | `outside the folders Cash Desk may read` | `test_import_reads_only_from_allowed_folders` |
| A second send of a draft that was already sent | `was already sent; a draft goes out once` | `test_send_requires_an_approval_of_the_exact_text_and_writes_once` |
| A send with no From address set | `set the From address (smtp_from)` | `test_send_needs_a_from_address_and_never_runs_scheduled` |
| A send from inside a scheduled run reaches the tool itself | `never sends from a scheduled run` | `test_send_needs_a_from_address_and_never_runs_scheduled` |
| Drafting before the business name is set | `set sender_name in the plugin settings` | `test_draft_needs_a_sender` |
| With `guard_outgoing_messages` on, a reply names a customer but states an amount, IBAN or invoice number the ledger does not hold for them | `Cash Desk withheld this reply` | `test_a_wrong_amount_for_that_customer_is_withheld` |
| **Load demo data** on a ledger that already holds data | `the ledger is not empty` | `test_load_demo_refuses_a_ledger_that_already_has_data` |
| A payment names an invoice but comes from someone else (another payer's name) | `check who paid before applying` | `test_a_payment_from_someone_else_is_not_applied_automatically` |
| A payment is dated more than three days before the invoice it names was issued | `check the dates before applying` | `test_a_payment_dated_before_the_invoice_goes_to_review` |
| Approving or sending a reminder whose invoice balance or due date changed since it was drafted | `not the amount this draft states` | `test_an_approved_reminder_with_an_old_amount_is_refused_and_redrafted` |
| Approving or sending old text after a new credit, promise, payment-review or missing-export hold: resolve the reason, redraft and read the replacement | `reminder is paused` | `test_new_hold_blocks_existing_draft_and_clears_approval` |
| An IBAN, payment reference or invoice customer changed: redraft and approve the replacement | `payment instructions changed` | `test_changed_payment_instructions_need_redraft_and_fresh_approval` |
| An older ledger did not store an unsent draft's payment-instruction basis: redraft before approving | `payment instructions were not recorded` | `test_existing_ledger_drafts_require_fresh_payment_instruction_basis` |
| A setting now puts the balance below the chase threshold: check the setting and redraft after resolving it | `reminder is paused` | `test_same_current_settings_gate_desktop_and_command_approvals` |
| An imported exact receipt has not been applied yet: run matching before chasing | `run cashdesk_match before chasing` | `test_exact_receipt_waiting_for_matching_blocks_an_old_approval` |
| A rewording adds a link, an email address, a percentage, a new payee or deadline, or a legal, settlement, interest, credit-reporting or suspension term | `a rewording may not add` | `test_a_rewording_may_not_add_links_addresses_percentages_or_terms` |
| A send while Hermes would pass its own prompt with nobody there (`--yolo`, or `approvals.single_query_mode`, `cron_mode` or `unattended_mode` set to `approve`) | `lets Hermes pass its prompt with nobody there` | `test_no_send_where_hermes_would_pass_its_own_prompt` |
| `/cashdesk apply` (or **Apply** on the page) for more than is left of a payment | `is left to apply` | `test_a_person_applies_or_dismisses_a_payment_waiting_for_review` |
| `/cashdesk approve` without the first characters of the hash the person read | `read the text first with /cashdesk show` | `test_approving_needs_the_hash_the_person_read` |
| A changed export may overlap local payments, or a legacy allocation has no imported settlement basis | `settlement reconciliation required` | `test_changed_export_settlement_requires_human_reconciliation` |
| Reconciling after the ledger changed since it was read | `snapshot changed` | `test_changed_export_settlement_requires_human_reconciliation` |
| A verified external settlement is negative or exceeds the invoice gross amount | `verified external settlement must be between zero and the invoice amount` | `test_reconciliation_input_refusals_are_actionable` |
| The reconciliation amount cannot be read in the invoice currency | `enter a verified external settlement in the invoice currency` | `test_reconciliation_input_refusals_are_actionable` |
| A receipt may be another observation of existing cash: inspect and verify it before Apply/Dismiss | `possible receipt overlap` | `test_changed_receipt_cannot_pay_twice_or_reuse_an_approval` |
| Applying an observation already verified as the same receipt: use its retained original | `receipt is a confirmed duplicate` | `test_changed_receipt_cannot_pay_twice_or_reuse_an_approval` |
| Receipt evidence changed after it was read: inspect the overlap again | `receipt snapshot changed` | `test_changed_receipt_cannot_pay_twice_or_reuse_an_approval` |
| A receipt decision is not one of the two supported choices | `choose same or distinct` | `test_changed_receipt_cannot_pay_twice_or_reuse_an_approval` |
| Reversing a confirmed receipt decision through another observation | `decision is already recorded` | `test_changed_receipt_cannot_pay_twice_or_reuse_an_approval` |
| Both historical observations already have allocations: keep them paused for an audited allocation correction | `both receipts already have allocations` | `test_both_historically_allocated_same_refuses_without_rewriting_history` |
| A receipt decision names records that are not in the ledger | `needs two stored receipts` | `test_receipt_commands_handle_spaces_and_reject_missing_records` |
| Applying already-consumed retained cash through a direct store match | `is left to apply` | `test_same_resolution_cannot_reopen_consumed_cash_through_direct_match` |
| Receipt alias history is corrupt: stop rather than consume uncertain money | `contains a cycle` | `test_corrupt_alias_cycle_fails_closed_without_financial_changes` |
| The same identity has conflicting placement evidence: use human Apply/Dismiss or verify existing completed allocations | `receipt allocation review required` | `test_same_after_existing_allocation_keeps_conflicting_reference_held_until_confirmation` |
| A dismissed observation belongs to the same identity: no unused cash can be resurrected | `identity group was dismissed` | `test_same_identity_honors_dismissal_on_the_newer_alias` |
| Confirming receipt placement without a pending review | `no receipt allocation review is pending` | `test_confirm_placement_requires_existing_completed_disposition_and_fresh_snapshot` |
| Unused cash remains unplaced: use Apply/Dismiss rather than confirming history | `place unused receipt money with Apply or Dismiss` | `test_confirm_placement_requires_existing_completed_disposition_and_fresh_snapshot` |
| A bank row has negative Debit/Credit magnitudes, conflicting amount/direction/currency evidence or repeated financial headers: check the original export and correct the columns | `bank amount evidence conflicts` | `test_bank_amount_evidence_conflicts` |
| A credit conflicts with a sale, source, customer, currency, document type or earlier closure: verify the accounting export | `credit identity conflict` | `test_credit_identity_conflicts_are_atomic` |
| A credit has an unknown state or contradictory paid balance | `credit update cannot be trusted` | `test_unknown_credit_status_does_not_silently_open_or_close` |
| An older credit may belong to another customer ID: re-import original evidence and verify unknown identity before chasing | `credit identity needs review` | `test_legacy_orphan_credit_pauses_chasing_until_reimport` |
| A settled credit or unapplied payment lacks a document number: no earlier credit is cleared by guessing its row position | `credit update needs a stable document number` | `test_numberless_terminal_payment_never_closes_an_unidentified_open_credit` |
| A revision changes, adds or repeats a financial line, including swapping figures between invoices | `financial lines changed` | `test_financial_lines_changed_refuses_reassigned_and_added_demands` |
| An older draft has no saved financial baseline: regenerate it before approval | `financial baseline missing` | `test_financial_baseline_missing_fails_closed_and_can_be_regenerated` |
| A terminal invoice observation conflicts with the known document/source or claims paid with a nonzero balance | `terminal invoice update conflicts` | `test_terminal_invoice_update_conflicts_leave_ledger_and_approval_unchanged` |
| Payer identity is missing, only similar/shared-word evidence, or ambiguous among known customers | `payer identity needs review` | `test_payer_identity_needs_review_for_missing_shared_or_similar_names` |
| An unresolved receipt may pay the named invoice or plausible payer debt, including partial receipts and candidates beyond the display limit | `receipt review pauses reminders` | `test_receipt_review_pauses_reminders_for_payer_debt` |
| Invoice amount/balance/paid/currency evidence disagrees, a positive balance is marked paid, or a financial header repeats | `invoice monetary evidence conflicts` | `test_invoice_monetary_evidence_conflicts_refuse_before_ingestion` |
| A provided bank ID changes financial/payer evidence, or a retained receipt gets adverse status evidence | `receipt update conflict` | `test_receipt_update_conflict_retains_changes_and_revokes_allocated_claims` |
| Statement and own-account metadata disagree | `bank account evidence conflicts` | `test_conflicting_statement_and_row_accounts_refuse_instead_of_guessing` |
| Evidence changed after a receipt update decision was prepared | `receipt update snapshot changed` | `test_stale_resolution_token_cannot_verify_changed_evidence` |
| Failed invoice update or changed review snapshot; verify the source with `/cashdesk import-errors` before chasing or applying cash | `invoice import uncertainty` | `test_empty_or_malformed_snapshot_cannot_remove_invoice_import_uncertainty` |
| Conflicting, duplicate or unknown bank lifecycle fields; verify the bank and re-export consistent statuses (allow_errors cannot bypass) | `bank status evidence conflicts` | `test_unknown_empty_or_conflicting_statuses_are_non_bypassable` |
| An unsigned positive bank amount beside a Type/Transaction Type value that names a debit Cash Desk cannot read exactly (Direct Debit, Lastschrift); re-export with signed amounts or a Debit/Credit or CdtDbtInd column | `bank direction evidence is unread` | `test_unread_debit_wording_beside_unsigned_amount_refuses_the_file` |
| An Info/Hinweis cell that names a bank lifecycle Cash Desk cannot read (for example 'Pending review'); re-export with a booked/pending status column | `names a lifecycle Cash Desk cannot read` | `test_unread_lifecycle_wording_in_info_refuses_the_file` |
| An invoice row whose balance/open-balance column is present but empty, with no paid/void status and no amount-paid figure; Cash Desk does not read the blank as the full amount owed. Fill in the open balance (0 if paid) or a paid status and re-import | `balance cell is blank` | `test_generic_blank_balance_without_status_is_a_row_error_not_a_full_debt` |
| A receipt for exactly the amount the invoice export already shows as paid (it may be the same money or a further instalment); Apply it if it is a further payment, Dismiss it if the export already counts it. Reminders for that invoice wait | `receipt may already be counted` | `test_receipt_equal_to_the_paid_part_goes_to_review_and_holds_the_invoice` |
| `cashdesk_match` asked to run for a future date; payments dated after today are not received yet | `matching cannot run for a future date` | `test_match_refuses_a_future_date_and_applies_nothing` |
| `/cashdesk not-duplicate` for two invoices that are not held as a possible duplicate pair | `are not held as a possible duplicate pair` | `test_not_duplicate_refuses_a_pair_that_is_not_held` |
| `/cashdesk still-open` for an invoice that is not held as missing from an export | `is not held as missing from an export` | `test_still_open_refuses_an_invoice_that_is_not_held` |
| Sending a reminder more than six days after it was written (its "N days overdue" is no longer true and the next stage is due); run `cashdesk_draft` and approve the fresh text | `days-overdue figures are no longer true` | `test_reminder_sent_more_than_six_days_after_writing_is_refused_and_redrafted` |
| The date_order setting decided a file whose dates all read both ways, and the other order is clearly the recent one; import it again with date_order for this file | `every date in this file reads both ways, and your date_order setting` | `test_dmy_setting_on_a_us_file_is_refused_with_both_readings` |
| A bank statement with no currency column or currency header, imported without saying the account's currency; import it again with default_currency | `has no currency column or currency in its headers` | `test_genuinely_currencyless_import_discloses_its_configured_default` |
| `/cashdesk send-status` for a draft with no send in progress | `has no send in progress` | `test_send_left_in_progress_is_resolved_with_send_status` |
| An invoice export that would hold more than half of a source's open invoices, without saying whether it is a complete list | `If it lists every open invoice` | `test_export_leaving_out_most_open_invoices_asks_which_kind_of_file_it_is` |
| Reminders (drafting, approval and sending) while a bank statement import is refused (its payments are unknown); fix and re-import that file, or draft with bank_checked=true after checking | `was refused, so payments in it are unknown` | `test_refused_statement_blocks_drafting_until_a_clean_import` |
| `cashdesk_draft` asked for a date other than today; leave the date out, or look at another day with `cashdesk_status` or the brief | `drafts are written for today only` | `test_draft_for_another_day_is_refused_and_writes_nothing` |
| An invoice export with a column that looks like an open balance (Balance Owing, Invoice Balance, Unpaid...) under a name Cash Desk does not read; rename it to balance or open balance | `is not read: Cash Desk would chase the full amount` | `test_unread_balance_like_header_refuses_the_file` |
| An invoice row with an amount-paid column whose cell is empty and no balance or paid status | `amount paid cell is blank` | `test_blank_amount_paid_cell_without_balance_is_refused` |
| An invoice row with a paid date but no open balance and no paid status | `has a paid date but no open balance` | `test_paid_date_without_balance_or_status_is_refused_not_chased` |
| Customer surplus is unresolved or its review snapshot changed; verify `/cashdesk surpluses` and accounting records before collection | `customer surplus review` | `test_surplus_verification_snapshot_cannot_be_empty` |
| A bank return may undermine a retained receipt; verify `/cashdesk returns` before approving or sending | `returned payment review` | `test_separate_return_revokes_existing_thanks_without_undo` |
| Return verification snapshot changed or bank explanation is missing; reread `/cashdesk returns` | `returned payment review` | `test_verified_unrelated_resolution_is_snapshot_bound_audited_and_requires_fresh_text` |
| A booked return observation is offered as incoming cash; verify `/cashdesk returns` first | `this observation is not verified incoming cash` | `test_positive_booked_return_observation_cannot_be_placed_as_incoming_cash` |
| A receipt may be the money a known bank return gave back; it is held from matching and Apply until `/cashdesk returns` is verified | `may have been returned` | `test_known_return_blocks_automatic_and_person_allocation` |
| Recording that a return reverses a receipt that is not the same amount and currency, or is a return itself | `is not a plausible unallocated receipt` | `test_reversal_refuses_amount_or_currency_mismatch` |
| Recording that a return reverses a receipt that is already allocated | `needs an audited correction, not this command` | `test_reversal_refuses_an_already_allocated_receipt` |
| Customer ID aliases conflict or repeat; verify the original IDs and re-export unique columns | `customer identity conflicts` | `test_invoice_conflicting_explicit_id_aliases_are_retained_as_partial_import_uncertainty` |
| An imported or historical owner is uncertain; verify `/cashdesk identities` before approving, allocating or sending | `customer identity review` | `test_explicit_id_change_on_known_invoice_retains_finances_and_revokes_old_authority` |
| Identity verification snapshot changed or an explanation is missing; reread `/cashdesk identities` | `customer identity review` | `test_human_identity_resolution_requires_snapshot_and_explanation_and_fresh_text` |
| A changed owner has settled, credit, allocation or send history; retain the hold for audited correction | `needs audited correction` | `test_customer_identity_change_never_reassigns_financial_or_sent_history` |
| A proposed owner has no retained source evidence; use an observed, verified customer ID | `verify a retained customer ID` | `test_identity_verification_cannot_guess_unobserved_customer_id` |
| A contact-only decision would transfer debt; verify the affected invoice identities separately | `verify each affected invoice separately` | `test_contact_question_cannot_bulk_transfer_debt_of_other_customer` |
| Populated invoice status/type/Stripe collection aliases conflict, are unknown or repeat; verify every lifecycle column and re-export | `invoice lifecycle evidence conflicts` | `test_all_invoice_status_aliases_are_reconciled_before_acceptance_or_skip` |
| An unissued/non-sale observation names an already issued document; verify original source history via `/cashdesk import-errors` | `skipped source observation may contradict a stored issued document` | `test_known_issued_invoice_cannot_silently_ignore_a_later_unissued_or_non_sale_row` |
| Bank payer/remittance aliases disagree or a verification snapshot changed; verify the original bank and current ledger via `/cashdesk bank-evidence` | `bank payer/reference evidence conflicts` | `test_partial_alias_questions_revoke_thanks_and_collection_without_financial_undo` |
| Statement, amount-header, row or inline bank currencies conflict or remain unresolved; verify the native amount currency and re-export via `/cashdesk bank-evidence` | `bank currency evidence conflicts` | `test_partial_currency_failure_revokes_collection_and_thanks_without_undo` |
| Customer email, contact, language or name changed, or an old draft lacks its original contact snapshot | `customer contact changed` | `test_contact_change_revokes_old_text_and_all_authority` |
| Conflicting/duplicate contact columns or missing cells make a contact update uncertain; re-export complete, agreeing columns | `customer contact evidence conflicts` | `test_uncertain_contact_columns_cannot_clear_or_partially_replace_authority` |
| Explicit payer/customer legal forms conflict; verify the bank entity before human placement or historical verification | `payer legal identity needs review` | `test_automatic_write_rechecks_payer_legal_identity_after_proposal` |
| Automatic allocation has unknown/conflicting reference identifiers, an order/transaction role, or no retained receipt; verify the remittance before human Apply | `reference evidence needs review` | `test_automatic_write_requires_reference_proof_even_for_an_old_proposal` |
<!-- refusals:end -->

**Measured.** In our test on the seven control drafts (after dismissing the demo review receipt), Figure Lock caught **74 of 74**
deliberate one-digit changes to their figures (16 dates, 16 amounts, 16 invoice numbers, 16 payment references,
10 IBANs; the first and the last digit of each occurrence, one at a time), each sent
through `cashdesk_revise`. Every untouched draft still passed. With the lock switched off
the same run reports 74 misses, so the count measures the lock, not the test.
`tests/plugin/test_mutations.py` reruns it and fails if these numbers change. Its negative
control disables both revision and storage checks. `tests/test_figure_binding.py` additionally
checks cross-invoice swaps, duplicate demands, gross-balance additions and approval/send
reservation against the saved baseline. The baseline survives reopening the ledger;
legacy drafts without one must be regenerated. This is a structural restriction, not a
claim to understand arbitrary financial prose or every commercial threat.

## From a real run

Hermes Agent 0.22.7 on macOS, the demo data, `hermes chat --oneshot` (5 November 2026).
Figure Lock refusing a changed amount and a foreign IBAN (the tool's answer, key fields
only):

```json
{"ok": false, "rejected": true, "draft_id": "bluebell-reminder14-0042-nov10",
 "diff": ["changed: EUR 1,250.00 -> EUR 1,520.00 (line 6: '€1,520.00')",
          "extra: IBAN GB82WEST12345698765432 is not a ledger figure (line 14)"],
 "message": "Figure Lock rejected the revision; the draft is unchanged. Keep every figure exactly as the ledger has it."}
```

Asking the agent to send an approved draft with nobody there to click (Hermes's own gate,
first sentence, wrapped):

```text
BLOCKED: Tool 'cashdesk_send' requires approval (Cash Desk wants to send a reminder a person
approved. Check the queue once more before allowing it.) but single-query mode (-q) runs
without a user present to approve it.
```

The reply check (`guard_outgoing_messages` on) when the agent was told to quote the wrong
amount (wrapped):

```text
Cash Desk withheld this reply: it states figures the ledger does not hold for Bluebell
Design Studio Ltd: EUR 1,520.00. Nothing was sent. Check the figures with cashdesk_queue
or cashdesk_status and answer again with the ledger's figures.
```

The same sentence with the ledger's EUR 1,250.00 was delivered word for word.

Sending by SMTP from Telegram: asked on Telegram to "Send the approved Northgate reminder",
Hermes put its approval buttons in the chat (approve once / always / deny); after one tap the
reminder went to a local test mail server over STARTTLS with a login, and the message it
received had the approved subject, body and hash (`X-Cash-Desk-Draft`). An approval left
untapped for 60 seconds timed out and nothing was sent.

