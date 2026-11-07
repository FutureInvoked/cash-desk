# Reconciliation details

How Cash Desk keeps imported settlements, receipts, credits, returns and identities straight across weekly exports, and what it asks a person to verify. Read this when Cash Desk pauses an invoice and you want to know exactly why.

## Reconciling invoice settlements

Cash Desk keeps payments already reflected in the first invoice export separately from local
bank allocations. For example, EUR 1,000 gross / EUR 600 open records EUR 400 already settled;
a local EUR 600 allocation clears it, and re-importing the old export keeps it cleared.
A fully reconciled zero-balance export also closes the invoice without counting local payments twice.

A changed positive export balance can mix external receipts with local payments already reflected
by the accounting system. Cash Desk preserves the financial records and pauses reminders and
further allocations until a person verifies the overlap. A reduction of an already recorded external settlement also requires verification, even before any local allocation. This conservative pause also applies
to old ledgers with local matches whose original imported settlement is unknown; opening the
upgraded ledger changes no financial rows or sent history. Unallocated legacy invoices establish
their basis from their stored imported balance.

Use `/cashdesk settlements` to read the invoice gross amount, open balance, local allocations
and snapshot token. Check the accounting records and bank evidence, then use
`/cashdesk reconcile <invoice> <verified-external-paid> <snapshot>` with the total payments
**excluding every local allocation shown**. Use a plain decimal amount in the invoice currency.
This explicit human action can correct an earlier external settlement; it retains all local
matches and an audit event, recalculates the balance, and requires fresh drafting and approval.
A stale snapshot is refused. An ordinary re-import or customer release cannot clear this pause.
Currency or customer changes after local allocations also require review; correct an erroneous
export and reconcile the original invoice. Moving allocations to another invoice/customer or
currency is not automated.

## Receipt identity across overlapping bank exports

A line that appears exactly once in each of two downloads with the same date, amount, currency, payer, reference and description is one bank line seen twice (this week's download overlapping last week's): Cash Desk records it as the same receipt automatically and logs the decision. Two identical lines inside one download, or more copies in the later download than in the earlier one, may be genuine separate payments and still wait for a person.

A row without a bank transaction ID is an observation, not proof of new cash. Cash Desk
retains each such row from a different export instead of silently merging equal text. The
same complete file still refuses as a duplicate. Provided IDs retain their existing idempotent
behavior; distinct bank-provided IDs are treated as distinct (account-scoped ID conflicts
still need review and are not inferred from filenames).

Rows with the same positive amount, currency and booking date become possible overlaps if
either identity is derived or historically unknown. Payer, memo, reference and source text
cannot make an uncertain receipt certain. This compares all stored history, including
allocated, dismissed and previously aliased observations. It does not detect an altered
booking date/amount or prove bank-account identity. Genuine equal receipts remain records,
and can be verified as distinct; Cash Desk never merges them based on the bucket alone.

Unresolved members are excluded from automatic matching and confident received/review cash
totals. Their observed row values are shown separately as unverified evidence; do not add
them as new cash. Status warns that balances need checking. Plausible referenced/payer/amount
invoices and invoices with existing allocations are held at planning, approval and send.
Apply and Dismiss refuse those receipts; Desktop review shows why and directs you to the
human controls. Old approvals are revoked and never revived by resolving the question.

Read `/cashdesk overlaps`, check the original bank evidence and displayed allocation history,
then copy its `/cashdesk receipt <id1> <id2> same|distinct <snapshot>` command. Quote IDs that
contain spaces. **Same** keeps the row with allocations, otherwise the earlier stored row;
all original rows and decision history remain, and the other observation becomes inert. It
does not move or reword an existing allocation. **Distinct** keeps both independent receipts
and their actual remaining amounts. Every decision is audited and bound to the complete
cohort/allocations snapshot; a third export can raise a fresh question. Then run matching,
draft again, read the replacement and approve it.

Old ledgers have unknown provenance, not guessed trusted IDs. Migration adds review metadata
without rewriting payment, invoice, allocation or sent-history rows. If **both** candidate
receipts already carry allocations, Same refuses and both remain held: an audited allocation
correction is required. Cash Desk does not yet undo those historical allocations; settlement
reconciliation does not remove local matches and is not a cure for this conflict. Distinct
is available only when bank evidence verifies two actual receipts. Earlier imports may have
already discarded observations through the old content hash; migration cannot reconstruct
missing records or certify old deduplication.

A **same identity** decision does not choose between conflicting invoice/payer evidence, including differing reference/memo text whose invoice
numbers may only become known on a later import. Those groups remain in allocation review, with all relevant invoices held and automatic
matching blocked. Use normal human Apply/Dismiss to place unused money. For an already fully
allocated or dismissed group, `/cashdesk overlaps` displays every existing allocation and a
`/cashdesk receipt-keep <canonical-id> <snapshot>` command: use it only after verifying that
existing completed disposition is correct. It moves no money and cannot bypass a pending
identity question or place unused funds. If the allocations are wrong, leave the review
paused for an audited correction. A dismissal on any same-identity observation applies to
the group's unused funds; resolving identity never resurrects dismissed cash.

## Credit notes across weekly exports

Credits and unapplied QuickBooks receipts use the same customer mapping as invoices,
including a file containing only credits. A unique exact customer name links the accounting
export to an existing contact ID; ambiguous duplicate contacts refuse instead of picking one.
An open credit pauses that customer's reminders and revokes unsent approvals. Older credits
left under a derived customer ID also hold plausible owners until a compatible numbered
export verifies the identity; opening the ledger revokes affected old approvals without
rewriting the credit's financial record.

Apply the credit in the accounting system, then re-import the updated invoices and the same
numbered, typed credit. A paid/zero or void credit update closes its old hold even without a
due date; `credit_updates` reports the retained closures. The skipped-row list describes why
it is not a new open credit question. Cash Desk does not apply the credit to an invoice itself
or reduce the invoice balance by guessing; the updated invoice export provides that balance.
Draft again, read the replacement and approve it. Closing a hold never restores old approval,
and sent messages remain history.

Credit source, document type, customer, issue date and currency must remain consistent.
An open export cannot silently reopen a credit already recorded as closed. Conflicts need
verification against the accounting records. Historical credits can be adopted only when
their recognizable credit record and identifying fields agree with a fresh observation.
Unnumbered credits remain conservative holds: a later numbered row or a changed row position
does not prove which earlier observation it closes. Automatic linking/correction of those
historical holds is not available; preserve the evidence for an explicit reconciliation.
Paid/zero and void invoice observations without a due date are retained too. For an
existing invoice, source, document number, customer, currency, gross amount and issue date
must agree; the original due date and payment instructions stay unchanged. The closure
revokes unsent approvals. An initial terminal observation creates no invoice or invented
due date; it is retained so a later stale open export cannot reopen it. Counts distinguish
`invoice_closures` applied to known invoices from `unmatched_invoice_closures` retained
without a prior invoice. Unknown historical source or conflicting identity requires checking
the original export. Open-list imports apply closures before assessing missing invoices.

Automatic matching requires a known customer name. Case, punctuation, trailing legal
suffixes and Latin accents normalize consistently; non-Latin letters and significant
combining marks are preserved. A Latin bank-name truncation must keep two complete leading
words and at least twelve characters, with at least two characters in any final partial
word, and fit only one known customer. Shared industry words, spelling similarity, reordered
names, unrecognized aliases and blank payer names require **PAYER** review. Fuzzy scores
rank suggestions only. Verify alternate trading names in the statement before applying a
payment manually; Cash Desk does not learn a reusable alias from a manual allocation.

Bank descriptions remain attached as evidence. When a bank supplies no structured payer
field, Cash Desk does not guess one from arbitrary description text. Such receipts need
review even if the reference and amount agree. A fixed three-day booking-date grace permits
receipts dated up to three days before invoice issue; earlier dates require **EARLY** review.
This is a documented reconciliation policy, not proof of the bank account holder's identity.

Unresolved incoming receipts pause plausible payer invoices in the receipt currency,
including partial/split debt. With no plausible known payer, every equal-balance invoice
is protected. These safety holds use the full ledger even though only three suggestions
are displayed. Review identifies all affected invoices; verify the statement, then Apply
or Dismiss. Holds are checked at drafting, approval and send. Resolving the receipt never
restores an old approval: draft and approve fresh text. No uncertain money is auto-applied.

invoice checks reconcile all populated gross, remaining, paid and currency
aliases, embedded currency symbols and financial-header currencies. Repeated financial
headers are refused before values can be overwritten. Contradictory values/currencies,
positive paid balances and remaining balances above gross require a corrected export;
ordinary ingestion is atomic, so refused rows cannot change debt or approvals. Explicit
allow_errors can import only valid rows, never a conflicting document. Xero line-item
collapse checks every financial alias across rows. Void cancellation can leave zero due
without a payment; Stripe Amount Due is the billing obligation when Amount Remaining is
present, allowing an explicitly represented credit offset from Total. These checks do not
repair incorrect historical imports; verify those balances in the accounting system.

receipt identity/lifecycle: explicit statement-account metadata scopes supplied
transaction IDs across exports. Filenames never identify accounts. Own IBAN/account columns
and statement preambles are used; an unqualified row IBAN is not assumed to be the sender's
account. Without account evidence, identical normalized observations stay idempotent and
changed provided IDs are quarantined. All observed financial rows and non-booked status
evidence with a stable ID are retained; contradictory rows are never silently discarded.
The import response explicitly reports quarantine and the review screen disables Apply/Dismiss
for update questions. Affected receipts/allocations are excluded from verified cash, and
reminders/thank-yous lose approval without rewriting financial or sent history.

Read `/cashdesk receipt-updates` for original/observed values, account/status/raw rows,
allocations, dismissals and a snapshot. After bank verification, use the shown
`/cashdesk receipt-update <original> <observation> keep|distinct|replace <snapshot>`:
keep verifies the existing receipt; distinct confirms separate unscoped cash; replace accepts
an **unallocated** correction. Same scoped IDs and adverse statuses cannot create separate
cash. Allocated corrections remain quarantined for verified allocation reconciliation (P2c);
no command silently undoes a match. Fresh drafting/approval is required after resolution.
Known older import fingerprints can be re-observed once to recover evidence discarded by an
older release; import history and financial rows remain. Re-import original statements to
supply missing historical evidence. This cannot reconstruct rows never retained or supplied.


Stripe imports treat `Paid` as a boolean (true/false, 1/0, yes/no), separate
from explicit `Paid At` / `paid_date` dates. Populated flags and statuses must agree;
contradictory or invalid dates and duplicate identity/status/payment-date headers require a corrected
export. A known paid closure preserves the original invoice dates when the export omits a
due date; a boolean never invents a payment date.

With explicit `allow_errors`, valid invoice rows can still import, but failed updates remain
in the local ledger as raw review evidence. Identified documents and their unsent reminders
or acknowledgements pause; uncertain invoice debt cannot receive new allocations. A row
without a trustworthy document identity pauses invoices from that accounting source
(including legacy invoices whose source is unknown). `/cashdesk import-errors` shows the
file, row, raw text, source and review snapshot. Status and the brief flag the uncertainty;
retained balances describe the last accepted export and may need correction.

Re-import a corrected valid row with the same source/document/customer to resolve an
identified question. Other files or unrelated good rows do not clear it. For source-wide
or otherwise unresolved errors, a person may compare the original export and current
ledger, then use `/cashdesk import-error <id> confirm <snapshot>` shown in the review.
That audited confirmation changes no money, matches or sent history and never restores an
approval. Draft again, read and approve fresh text. Strict rejected imports make no changes;
old previously discarded errors are not automatically reconstructed or repaired.


bank status checks inspect **every populated recognized lifecycle column**:
Status, State, Transaction Status and Booking Status. Booked/completed/posted synonyms
must agree; pending, failed, cancelled, reversed/returned and refunded form separate
non-booked families. Agreeing aliases retain the original raw row; non-booked rows still
carry lifecycle evidence for known receipts and never become incoming money. Blank aliases
are ignored when another status is populated; entirely blank or unknown lifecycle evidence
requires correction. Files without a status column retain the documented booked assumption.

Duplicate normalized status headers and contradictory/unknown statuses refuse the **whole
file**, including otherwise valid rows. `allow_errors` cannot bypass this refusal. The row
number, raw reader evidence and conflicting field values identify what to verify against
the bank. Re-export with unique, consistent lifecycle fields, then import again. A refused
file changes no existing financial rows, imports or approvals; no historical reconciliation
is inferred from rejected data. A valid later adverse update for a known receipt continues
to use the receipt-update hold and person-verified recovery described above.


overpayment protection retains a positive sale's **negative exported remaining
balance** as a customer-surplus question. Operative invoice debt stays at zero; the signed
balance, gross, currency, captured customer/contact name, raw CSV row, source and fingerprint remain
separate review evidence. These observations are **not additional cash** and repeated versions
are never summed. No automatic set-off against another invoice or currency occurs.

An unresolved question pauses that customer's reminders and acknowledgements at drafting,
approval and send, including the prior owner when the first negative export changes customer identity and plausible name-derived contact aliases. New authoritative contact
IDs, later zero/void exports and ordinary settlement reconciliation do not silently erase it.
Existing financial/allocation/sent history remains; approvals stay revoked. Valid incoming bank
receipts can still be matched deliberately to their invoices; customer collection and receipt
acknowledgements wait for verification. Other customers remain eligible when their identity is
unambiguous. All currencies are retained separately and any FX/set-off decision stays outside
Cash Desk.

`/cashdesk surpluses` shows raw versions, current invoice, settlement basis and local allocations.
Compare them to accounting records, including any refund or applied credit outside Cash Desk.
Only after the surplus is accounted for, use the displayed human-only
`/cashdesk surplus-accounted <invoice> <snapshot>`. This records verification and clears the
question; it moves no money and never restores old approval. Draft/read/approve fresh text.
If later negative evidence reappears, it needs a fresh review. A changed snapshot requires
rereading; a legacy operative negative balance requires a corrected export first.

On first upgrade, known stored settlement/allocation excess and retained negative-balance
export events become historical questions without rewriting financial rows. Original customer
identity may be unavailable in old events; the current invoice/customer is a provisional
reference to verify, not proof of a historical bank credit. Entirely lost/unknown history cannot
be reconstructed. Credit-note lifecycle remains separate. Status/brief warnings and Activity
explain the pause; all raw observations and verification decisions stay in local SQLite.
No extra network access or background process is introduced.


returned-payment protection retains explicit **returned/reversed/reverted,
bounced/dishonoured and chargeback** observations as review evidence, including a separate
negative debit without an invoice reference and non-booked rows with a new or missing ID.
Ordinary outgoing supplier payments and customer refunds are not inferred to be bounced receipts.
A status-only row is not new cash. Missing or conflicting financial hints widen uncertainty;
they do not supply an invented date, amount, currency or allocation.

`/cashdesk returns` shows retained evidence, plausible historical receipts, affected invoices
and a snapshot. Payer/reference hints protect the customer's other debt too; ambiguous receipts
are all candidates, and unknown identity can conservatively pause the currency ledger (all
invoices when currency is unknown). This is a verification scope, not proof of a reversal.
Existing balances, allocations and sent history remain unchanged. Affected unsent reminders
and thank-yous lose approval; new acknowledgements, approval and send wait for verification.
The morning brief excludes affected receipt claims and warns that balances need review.
The Desktop review panel also shows return questions without an incoming-cash Apply button.
Same-ID adverse lifecycle observations continue through `/cashdesk receipt-updates`.

When the return is already known before a receipt is allocated (for example the same statement
holds +EUR 1,200 and the matching -EUR 1,200 RETURNED R01), Cash Desk does not apply that
receipt, automatically or by Apply. Any unallocated money in a receipt the return could have
given back (same amount and currency; every scoped receipt when the return has no amount) stays in review,
is not counted as incoming money, and the invoice keeps its full open balance. Receipts of other
amounts or customers are matched as usual. A person then compares the statement and either
confirms the return unrelated (the receipt is matched again) or runs the displayed
`/cashdesk return-reverses <id> <payment-id> <snapshot> <verified-bank-explanation>`: both raw
bank rows are kept, the receipt is closed as not received with the explanation in the audit log,
and chasing resumes on the next draft run. It refuses a receipt that is already allocated.

For a verified **unrelated** observation only, a person can run the displayed
`/cashdesk return-unrelated <id> <snapshot> <verified-bank-explanation>`. Compare the full bank
statement and accounting records first: every affected original receipt must remain settled.
The action records the person's explanation and releases the question; it changes no financial
rows and restores no approval. Draft/read/approve fresh text. A genuine bounced allocation
must remain held for an audited accounting/allocation correction; this command cannot reverse
it. Such financial correction is not implemented here. Lost historical evidence cannot be
reconstructed; retained explicit return payments/lifecycle evidence are conservatively reviewed
on upgrade. Return observations and verification decisions stay in local SQLite. No extra
network access, real delivery or background work is introduced.


customer-ID protection treats exported **explicit IDs as authoritative identities**,
even when names match or an explicit ID happens to equal a generated name key. Stripe `Customer`
and supported `Customer ID` / `ContactID` aliases retain their values; recognized explicit IDs in
Xero/QuickBooks-style exports and generic contacts/invoices do too. Distinct IDs keep separate
customer records, debt and recipients. Names never merge explicit IDs. Conflicting populated ID
aliases and repeated ID headers are refused. A partial invoice import retains failed identity
rows through the invoice-import uncertainty guard; conflicting-ID contact files cannot be
partially ingested.

Name-only exports retain a **derived** origin. They may adopt a single compatible known explicit
contact, including one from an earlier contacts import, only when name and available email evidence
are unambiguous. Multiple candidates, conflicting emails or unknown historical origins create a
customer identity question rather than selecting a recipient. Original ID/origin, raw row,
source/fingerprint, proposed binding and verification decisions remain in local SQLite. Supplied
IDs are never classified as derived by their spelling.

`/cashdesk identities` shows current ownership, all observations, plausible customer IDs,
affected debt, allocations and a snapshot. Unresolved identity questions pause affected customer
collection and acknowledgements, revoke unsent approval, and block allocation to an uncertain
invoice. Status, brief, slash review and Desktop disclose the pause. Original invoice/balance,
allocation and sent history stay unchanged while source identity conflicts. A fresh draft and
approval are required after verification. Equal-name customers that have distinct explicit IDs
and no uncertainty retain their independent workflows; an explicitly identified credit is not
silently transferred to a same-name customer.

Compare the original accounting export, invoice ownership and contacts before the human-only
`/cashdesk identity-verify <invoice> <customer-id> <snapshot> <verified-accounting-explanation>`.
This records a person's verification of the current owner. An owner change is permitted only to
an explicitly observed customer for a wholly unpaid sale with no allocation or send history;
only the operative owner changes, with its previous value retained in the audit. It moves no
cash, rewrites no allocations/sent records, and restores no approval. Amount/date/status changes
still need a fresh authoritative import. Settled/paid/void invoices, credits, allocation history,
unknown settlement basis and send records require a separate audited correction and remain held.
A contact-only decision cannot bulk-transfer another customer's debt.

On upgrade, non-generic invoice/credit imports without retained ID-origin proof become historical
questions. A surviving credit source-customer value is shown as a hint; its explicit/derived origin
is **unknown**. Lost original IDs cannot be reconstructed from a current name, email or ID spelling.
An unknown legacy owner conservatively pauses the whole ledger until a person verifies it;
current names cannot prove which other customers are unaffected. Matching new evidence does not
silently bless an uncertain old remap. Known derived credit adoption
still works when provenance was retained and the fresh numbered accounting evidence is compatible;
unknown legacy credit ownership stays held for verification/audited correction. No extra network,
background service or live delivery is introduced. ID-mapping protection is separate from the contact-snapshot checks described below.


invoice lifecycle checks reconcile **every populated recognized status and document
type alias**, plus Stripe collection-method aliases, before skipping unissued/bill rows, choosing
an invoice state or collapsing Xero line items. Empty aliases supply no evidence; supported
source-specific equivalents agree (for example Xero authorised/open, QBO closed/paid and Stripe
uncollectible/void). Sale, credit, supplier bill/credit and QBO unapplied-payment types remain
separate. Conflicting, repeated or unknown lifecycle evidence refuses the row/group; QBO's
supported non-invoice transaction skips remain available. Stripe Paid booleans still agree with
status and explicit paid dates. A bad later Xero line refuses its whole invoice, preserving all
raw group rows instead of silently selecting the first line.

Ordinary strict imports refuse files with row errors without ingesting them. If a person selects
partial invoice import (`allow_errors`), rejected lifecycle rows remain raw questions in
`/cashdesk import-errors`; their captured explicit/derived customer identity scopes conservative
holds over affected collection and acknowledgements, including old approvals. Credit/type
uncertainty can affect the customer's other invoices. Unknown header/group/customer scope pauses
the whole ledger until verified. Allocations and sent history remain unchanged; this check never
silently reverses money, closes or reopens an uncertain invoice. Accepted authoritative corrections
of the same source/document/customer can clear the import question; retained real credit or other
holds still apply. Snapshot-bound human verification is also available via import-errors. Neither
path restores approval: draft/read/approve fresh text.

A valid new draft or supplier/non-invoice row is still skipped. If such an observation names an
already issued ledger document, it is retained as an import question instead of silently leaving
the old collection authority current. It does not transform that document into a bill or draft.
The new optional customer-origin metadata and raw questions stay in local SQLite; no extra
network, background process or real delivery is introduced. Conflicting issue/due dates and
invoice-number aliases remain separate pending fixes; this change does not certify those fields.


bank payer/reference checks reconcile every populated recognized alias and
repeated normalized header from the original cells **before creating payment identities**.
Case, Latin accents, punctuation and equivalent legal abbreviations are normalized; explicitly
different legal forms remain conflicting. Reference separators can agree, but distinct values
(including an invoice number versus a different variable symbol) need verification; Cash Desk
never guesses that they describe the same debt. Empty companion aliases are ignored. Transaction
IDs remain separate from remittance references, and descriptions never establish payer identity.

Conflicting rows are errors and never become payments or lifecycle observations. Strict imports
refuse the file and change nothing. With explicit `allow_errors=true`, compatible rows may be
retained as payments, while conflicting rows retain original headers, cells, preamble, raw row,
file and fingerprint in SQLite. **All allocation, collection and acknowledgements across the
whole ledger pause**, including existing receipts, approvals and invoices imported later. This
conservative scope covers uncertainty about which customer or receipt the aliases describe;
balances, allocations and sent history are unchanged, rather than automatically reversed.
Status conflicts still refuse the entire file regardless of `allow_errors`.

Status, matching, brief, `/cashdesk review` and Desktop show the outstanding bank question;
`/cashdesk bank-evidence` shows all named invoice and plausible customer candidates without a
three-item limit, plus the original evidence. Compare the original bank statement and current
ledger, import any corrected cash separately, then use the human-only
`/cashdesk bank-evidence-verify <id> <snapshot> <verified-bank-explanation>` shown in that review.
A stale snapshot or missing explanation refuses verification. Each question needs verification;
a corrected export alone does not clear it. Verification records the person and explanation,
creates no cash, changes no allocation or sent history, and never restores an old approval.
Run drafting again and read/approve fresh text. Historical aliases discarded before this change
cannot be reconstructed automatically; no claim is made that old ledger rows were repaired.
Re-observing an already imported original file with `allow_errors=true` can retain newly
detected alias questions once; it preserves existing cash and allocation history.


bank currency checks establish the **native receipt amount's currency** before
constructing payment identity. Explicit labelled Currency/Account currency/Statement currency
metadata (including joined AccountCurrency/CurrencyCode and CCY labels; vertical CSV,
`Currency: GBP`, `Currency = GBP`, or horizontal account headings),
financial headers such as Amount (GBP), populated currency aliases/repeated currency headers
and inline amount currencies must agree. Recognized agreeing evidence supplies the currency;
configured EUR never overrides explicit GBP. Balance-only header currency cannot establish an
unlabelled transaction amount's currency. IBAN country, account names and arbitrary descriptions
are never currency evidence. Different account/native currencies need a correctly mapped native
amount export; Cash Desk does no FX conversion or guessed role selection.

Unknown/conflicting evidence, ambiguous bare dollar symbols without an explicit supported
native code, or unresolved labelled statement currency refuses the row. Dollar amounts can use
explicit USD/CAD/AUD/NZD; £/€ inline amounts retain their currency. Strict rejection writes
nothing. Explicit partial imports retain the original row, cells, headers, preamble and currency
sources via `/cashdesk bank-evidence`, pause all allocation/collection/acknowledgements and revoke
old approvals. Snapshot-bound human verification records an explanation without changing money,
allocations or sent history; it never restores approval. Corrected cash is imported separately.

Genuinely currency-less CSVs remain usable with the configured default, with a visible per-row
`configured default XXX used` import warning. Verify that setting against the statement; a default
is an assumption, not bank confirmation. Every accepted observation retains its explicit/default
currency basis and raw source metadata in SQLite alongside receipt observations. Receipt-update
review includes this currency history. A change in currency for the same raw bank row conservatively
holds prior/current authority, including derived IDs, without undoing historical allocations.
An already imported original file may be re-observed once to retain newly available currency
provenance; discarded original headers/preambles cannot be reconstructed without that source.
Bundled demo currency changes are explicitly marked as demo label changes, not bank conversion.


remittance checks distinguish **known candidates from proof of where cash belongs**.
Invoice numbers and payment references use whole Unicode identifier boundaries, including
combining marks: digits embedded in ORDERX20261001Z cannot prove reference 20261001. Supported
space/dash/slash/underscore separators may repeat or be omitted; literal identifier letters,
digits and leading zeroes remain significant. Numeric payment references shorter than three
digits never authorize automatic allocation. Numeric invoice numbers need a whole remittance
value or recognized invoice/payment-reference label. Ordinary Payment reference/REF/VS prefixes
remain usable; an Order, project, purchase-order, bank-reference or transaction identifier does
not become an invoice merely because the digits coincide. Reference and memo are parsed
separately, so joining unrelated fields cannot manufacture an identifier.

Recognized INV/Invoice/FA/Faktura/FI/VS labels, explicitly numbered invoice identifiers and
code-like identifiers retain additional unknown targets. INV-1001 plus absent INV-9999 cannot
silently allocate everything to the only known invoice. Unknown alphanumeric codes remain
visible in the full `unknown_references` list, alongside all named known invoices. Order/project
roles and explicit future-deposit/advance or do-not-apply wording require review; Cash Desk does
not claim general natural-language understanding of arbitrary bank descriptions. Transaction ID
columns never supply invoice proof, and amount/name similarity still cannot authorize allocation.
Supported exact-reference controls remain usable; other ambiguous spellings go to a person.

New receipts with uncertain remittance stay in review, pausing plausible collection. A person
can verify the bank evidence and deliberately Apply/split or Dismiss unused funds under existing
balance/currency limits. Automatic writes recheck reference proof and require a retained receipt;
a stale proposal cannot bypass the current rule. Nothing is silently placed on an unknown invoice.

On ledger open or relevant changes, retained **automatic reference allocations** are checked
against this policy. Uncertain old allocations create durable `/cashdesk bank-evidence` questions
and conservatively pause all allocation, collection and acknowledgements, revoke unsent approval
and exclude the old matches from verified thanks/briefs. Original balances, matches and sent
history are preserved, not reversed or repaired. Review exposes original retained bank rows,
known/unknown targets and allocations. A person may verify the original remittance and current
ledger using the shown snapshot and explanation; no approval is restored, and genuine mistaken
allocations require audited correction outside this confirmation. Identical verified evidence
stays verified across reopens; a material new target or changed receipt/allocation basis requires
new verification. Previously lost raw evidence cannot be reconstructed automatically.


payer-to-customer checks retain recognized explicit legal forms: Acme GmbH
cannot automatically pay Acme Ltd solely because the base name is unique in the ledger.
Ltd/Limited, Inc/Incorporated and dotted equivalents such as LLC/L.L.C. remain compatible;
compound forms are compared in full. Case, Latin accents, Unicode and bounded unique bank
truncation retain their documented behavior. An omitted legal form can still match an
unambiguous known base name; that is a bounded matching policy, not proof of real-world
ownership or a verified payer-alias registry. Unrecognized forms/description text are not
certified legal identity. Explicit conflicts require PAYER review and deliberate human Apply
only after checking authority to pay that customer's debt; a reference alone cannot resolve it.

New automatic Store writes recheck conflicting explicit forms against the current customer.
Historical automatic allocations with retained incompatible forms become durable bank-evidence
questions; collection and acknowledgements pause without reversing cash, allocations or sent
history. Desktop/commands show the original payer and allocated customer names. A person uses
snapshot-bound bank-evidence verification after checking the bank and legal relationship;
verification never restores old approval. Missing historical ownership and audited financial
correction remain separate work. No new network service, background job or SMTP operation.


contact freshness binds each customer-facing reminder and thank-you to the
customer ID, name, language, email and contact name stored when the draft is created.
A material change holds old unsent text and removes its approval in the same import/update
transaction. Approval, preflight and send reservation independently compare the current
contact and require the recipient to equal the current customer email. Prose revision cannot
rebind old contact authority. Run `cashdesk_draft`, read the replacement recipient/greeting,
and approve afresh; returning to the old address does not restore an old approval.

In a **customers CSV**, an explicitly present, empty `email` cell clears the stored address;
a present empty `contact_name` clears the greeting name (future drafts use “Hello,”).
Omitting that column preserves its stored value. Removing the email stops customer-facing
drafting until a current address is supplied. Do not include an empty column when you intend
“leave unchanged.” Conflicting populated contact aliases, repeated contact headers and
shortened rows missing contact cells refuse the entire contacts file, including `allow_errors`.
The refused file changes no contacts. Invoice-derived contacts still only fill blanks.
The low-level Store API keeps blank-as-unspecified compatibility unless the caller explicitly
passes `contact_fields={'email'}` or `{'contact_name'}`; CSV column presence supplies this
metadata automatically.

Old unsent drafts without an original contact snapshot are held for regeneration, never
backfilled from today's address. Already-sent drafts, recipients, allocations and send records
remain unchanged, and a contact update does not resend a sent thank-you. Unsent thank-yous
can regenerate after verification and fresh approval. Old/current contact snapshots and
invalidation events stay in local SQLite. No additional network or background job is added.
