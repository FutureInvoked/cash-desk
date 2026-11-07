# Weekly use

What a normal week looks like, what pauses a reminder, and how to resume it.

Import each week's exports. A newer export never undoes a payment Cash Desk
applied. An open invoice that the next export from the same source no longer lists (usually
because it was paid) is held as "not in the latest export" instead of being chased; the import
says which. If a file is deliberately partial (one month, one customer), import it with
`open_list: false`; if you know a held invoice is still owed, `/cashdesk still-open <invoice> <why>`
chases it again. Two open invoices that look like one debt imported twice (`INV-0041`
and `inv-0041`, `1041` and `INV-1041`, or a Stripe invoice synced into Xero with the Stripe number
as its reference) for the same amount and customer or address are held, so no reminder asks for
both; `/cashdesk not-duplicate <invoice> <other> <why>` releases a genuinely separate pair. Two customer records with the same email address share holds and disputes, and only one reminder goes to an address within three days. A contact list (`Customer, Email, Full Name`, as QuickBooks and
Xero export it) meets the invoices by name, so reminders get their email addresses. When a
newer reminder stage is drafted, the older unsent one is replaced, so two reminders for one
invoice never wait side by side.

An approval does not override a later hold. A recorded promise, an open credit attached
to that customer, an unresolved payment identified with that invoice, or an invoice missing from the latest complete
open-invoice export also stops reminders drafted or approved earlier. These checks use
the same current rules as planning, including `ignore_balances_below`. The old text stays
visible with a **DO NOT APPROVE** or **STALE** warning; Activity explains why it was paused.
Resolve the reason, run `cashdesk_draft`, read the replacement and approve it again.
Resolving a hold never restores the old approval. A new IBAN or payment reference also
requires a replacement, even if the amount is unchanged.
Rewording paused text does not lift its hold or remove the need for that replacement.
If an exact receipt is ready to apply but matching has not run, run `cashdesk_match`
before drafting again; checking eligibility never applies money by itself.

On the first use of an older ledger, Cash Desk adds the payment-instruction basis to its
SQLite schema. It cannot establish which bank details an old draft used from today's
invoice, so old unsent drafts without that basis lose their approval and need redrafting.
The migration preserves invoices, payments, prior sends and the old draft text.

Every command Cash Desk names in a pause is listed by `/cashdesk help`.
