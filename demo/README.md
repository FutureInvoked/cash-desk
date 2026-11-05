# Demo data (fake)

Everything here is made up: invented companies on `.example` domains,
placeholder contact names, and bank accounts taken from publicly documented
IBAN examples (the German example `DE89 3704 0044 0532 0130 00`). No real person, company or
account appears.

Everything is in EUR. Set the plugin's `currency` setting (for example `USD`) and the
demo is shown in that currency instead; only the label changes.

The files are written for **2026-11-10** as the "today" of the demo. When Cash Desk
loads them it moves every invoice and payment date by the gap between that day and the
real today, so the demo always happens today and every "N days overdue" is true. The engine on its own (outside the plugin) reads the
dates as written; use `as_of=2026-11-10` there.

| File | Format | Rows |
|---|---|---|
| `customers.csv` | generic customers | 8 customers, all in English |
| `invoices.csv` | generic invoices | 11 invoices, all in EUR |
| `bank_day1.csv` | bank statement, day 1 (2026-11-06) | 2 lines |
| `bank_day2.csv` | bank statement, day 2 (2026-11-09) | 1 line |

## What the data produces (dates as written, for 2026-11-10)

- **Reminder 7**: Northgate Sample Studio Ltd, INV-2026-0041 due 2026-11-03.
- **Reminder 14**: Bluebell Design Studio Ltd, INV-2026-0042 due 2026-10-27 for
  exactly EUR 1,250.00. This is the Figure Lock demo draft.
- **Reminder 21**: Greenhill Outfitters Ltd, INV-2026-0036, EUR 4,840.00.
- **Reminder 28**: Birchfield Example Consulting GmbH, INV-2026-0031 at 28
  days plus INV-2026-0044 at 10 days: one draft listing both.
- **Reminder 28** (backlog): Stonebridge Holdings Ltd, INV-2026-0027 at 36
  days, EUR 12,100.00. Cash Desk never wrote to them before, so this first reminder
  states the real age and claims no earlier reminder. If it is still unpaid a week
  later, an internal escalation note is drafted for the reviewer.
- **Void**: INV-2026-0038 (Northgate) is void and never chased.
- **Dispute**: Oakridge Placeholder Partners LLP has INV-2026-0033 disputed,
  which also holds INV-2026-0040.
- **Day 1 bank**: B1-01 pays INV-2026-0045 (Fernhill Test Media Ltd) by
  reference and exact amount, so it is applied automatically and a thank-you is
  drafted. B1-02 (EUR 700.00 from "BIRCHFIELD CONSULTING", no reference) is a
  fuzzy candidate for INV-2026-0044 (EUR 720.00) and goes to the review queue;
  nothing is applied.
- **Day 2 bank**: B2-01 pays INV-2026-0043 (Lindenway Logistics Ltd) by its
  payment reference and exact amount, EUR 3,025.00: applied automatically, thank-you
  drafted. Nothing else is drafted twice on the same day.

## Figure Lock demo

Take the reminder-14 draft for Bluebell Design Studio Ltd. Rewording it passes.
Changing `EUR 1,250.00` to `€1,520.00` and adding a foreign account
(`GB82 WEST 1234 5698 7654 32`, another public example) is rejected with a
two-line diff: the changed amount and the extra IBAN.
