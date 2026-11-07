// Cash Desk — Hermes Desktop half. Plain ESM, no build step, no JSX syntax.
// Shows the open balance by age bucket, the approval queue with the exact text of
// every draft, customers on hold, recent activity and a status-bar chip. Every figure
// is a string the backend formatted from the ledger; this page never computes money.
import * as sdk from '@hermes/plugin-sdk'
import {
  host, useQuery, useMutation, queryClient,
  ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, PALETTE_AREA,
  Button, EmptyState, GlyphSpinner, ScrollArea,
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'cash-desk'
const PATH = '/cash-desk'
const POLL_MS = 5000
let pluginCtx = null

const Badge = sdk.Badge ?? (({ children }) => jsx('span', { style: styles.badge, children }))
const ErrorState = sdk.ErrorState ?? (({ title, description }) =>
  jsxs('div', { style: styles.muted, children: [jsx('strong', { children: title }), jsx('div', { children: description })] }))

const styles = {
  page: { padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 18, maxWidth: 1100 },
  h1: { fontSize: 18, fontWeight: 600, margin: 0 },
  h2: { fontSize: 13, fontWeight: 600, margin: '0 0 8px', color: 'var(--ui-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.4 },
  muted: { color: 'var(--ui-text-secondary)', fontSize: 12 },
  row: { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' },
  card: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: '10px 12px', minWidth: 140 },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', padding: '6px 8px', color: 'var(--ui-text-secondary)', fontWeight: 500, borderBottom: '1px solid var(--ui-stroke-secondary)' },
  td: { padding: '8px 8px', borderBottom: '1px solid var(--ui-stroke-secondary)', verticalAlign: 'top' },
  badge: { display: 'inline-block', padding: '1px 6px', borderRadius: 999, border: '1px solid var(--ui-stroke-secondary)', fontSize: 11 },
  ok: { color: 'var(--ui-accent)' },
  figure: { fontVariantNumeric: 'tabular-nums' },
  link: { background: 'none', border: 'none', padding: 0, color: 'inherit', font: 'inherit', cursor: 'pointer', textAlign: 'left' },
  body: { whiteSpace: 'pre-wrap', fontFamily: 'inherit', fontSize: 12, margin: '8px 0 0', padding: '8px 10px', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6, maxWidth: 720 },
  hash: { fontFamily: 'var(--ui-font-mono, monospace)', fontSize: 11, color: 'var(--ui-text-secondary)' },
}

const rest = (path, opts) => pluginCtx.rest(path, opts)

// Ledger timestamps are UTC ISO strings; show them in the viewer's own time zone.
function localTime(iso) {
  const d = new Date(iso ?? '')
  if (Number.isNaN(d.getTime())) return String(iso ?? '').replace('T', ' ').slice(0, 16)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function useStatus() {
  return useQuery({ queryKey: [ID, 'status'], queryFn: () => rest('/status'), refetchInterval: POLL_MS, retry: 2 })
}
function useDrafts() {
  return useQuery({ queryKey: [ID, 'drafts'], queryFn: () => rest('/drafts?status=pending'), refetchInterval: POLL_MS, retry: 2 })
}
function useHolds() {
  return useQuery({ queryKey: [ID, 'holds'], queryFn: () => rest('/holds'), refetchInterval: POLL_MS, retry: 2 })
}
function useReview() {
  return useQuery({ queryKey: [ID, 'review'], queryFn: () => rest('/review'), refetchInterval: POLL_MS, retry: 2 })
}
function useEvents() {
  return useQuery({ queryKey: [ID, 'events'], queryFn: () => rest('/events?limit=12'), refetchInterval: POLL_MS, retry: 2 })
}
function useAction(successMessage) {
  return useMutation({
    mutationFn: ({ path, body }) => rest(path, { method: 'POST', body: body ?? {} }),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: [ID] }); host.notify({ kind: 'success', message: successMessage }) },
    onError: (error) => host.notifyError(error, 'Cash Desk: the action was refused'),
  })
}

function Buckets({ snap }) {
  const display = snap.open_by_bucket_display ?? {}
  const entries = Object.entries(display)
  if (!entries.length) return jsx('div', { style: styles.muted, children: 'No open invoices.' })
  return jsx('div', { style: styles.row, children: entries.map(([cur, buckets]) =>
    jsxs('div', { style: styles.card, children: [
      jsxs('div', { style: styles.muted, children: [cur, ' · open ', jsx('span', { style: styles.figure, children: (snap.open_total_display ?? {})[cur] ?? '' })] }),
      jsx('div', { style: { ...styles.row, gap: 10, marginTop: 6 }, children: Object.entries(buckets).map(([label, amount]) =>
        jsxs('div', { children: [jsx('div', { style: styles.muted, children: label === 'current' ? 'not due' : `${label} days` }), jsx('div', { style: styles.figure, children: amount })] }, label)) }),
    ] }, cur)) })
}

function FigureBadges({ draft }) {
  const figures = draft.figures ?? []
  if (!figures.length) return jsx('span', { style: styles.muted, children: '—' })
  const bad = figures.filter(f => f.ok === false).length
  const label = bad ? `✗ ${bad} of ${figures.length} figures` : `✓ ${figures.length} figures match the ledger`
  return jsx(Badge, { children: jsx('span', { style: bad ? undefined : styles.ok, children: label }) })
}

function DraftRow({ d, approve, hold, reject }) {
  const [open, setOpen] = useState(false)
  return jsxs('tr', { children: [
    jsxs('td', { style: styles.td, children: [d.customer, jsx('div', { style: styles.muted, children: d.customer_id })] }),
    jsxs('td', { style: styles.td, children: [d.label ?? d.kind, jsx('div', { style: styles.muted, children: d.lang })] }),
    jsxs('td', { style: styles.td, children: [
      jsx('button', { style: styles.link, title: open ? 'Hide the text' : 'Read the exact text', onClick: () => setOpen(!open), children: d.subject }),
      jsx('div', { style: styles.muted, children: (d.recipients ?? []).join(', ') || 'internal note, no recipient' }),
      open ? jsx('pre', { style: styles.body, children: d.body }) : null,
      open ? jsxs('div', { style: styles.hash, children: ['hash ', d.hash] }) : null,
      jsxs('div', { style: { ...styles.row, gap: 8, marginTop: 6 }, children: [
        jsx(FigureBadges, { draft: d }),
        jsx(Button, { size: 'xs', variant: 'secondary', disabled: !open, title: open ? 'Approve exactly this text' : 'Read the text first: click the subject',
          onClick: () => open && approve.mutate({ path: `/drafts/${encodeURIComponent(d.id)}/approve`, body: { hash: d.hash } }), children: open ? 'Approve' : 'Read to approve' }),
        jsx(Button, { size: 'xs', variant: 'ghost', title: 'Hold this customer: nothing goes out until released',
          onClick: () => hold.mutate({ path: `/drafts/${encodeURIComponent(d.id)}/hold`, body: { reason: 'held from the Desktop page' } }), children: 'Hold' }),
        jsx(Button, { size: 'xs', variant: 'ghost', title: 'Reject this draft',
          onClick: () => reject.mutate({ path: `/drafts/${encodeURIComponent(d.id)}/reject`, body: { reason: 'rejected from the Desktop page' } }), children: 'Reject' }),
      ] }),
    ] }),
  ] }, d.id)
}

function Queue({ drafts, approve, hold, reject }) {
  if (!drafts.length) return jsx('div', { style: styles.muted, children: 'Nothing is waiting for approval.' })
  return jsxs('table', { style: styles.table, children: [
    jsx('thead', { children: jsx('tr', { children: ['Customer', 'Stage', 'Draft (click the subject to read the exact text)'].map((h, i) => jsx('th', { style: styles.th, children: h }, i)) }) }),
    jsx('tbody', { children: drafts.map(d => jsx(DraftRow, { d, approve, hold, reject }, d.id)) }),
  ] })
}

function IdentityQuestions({ questions }) {
  return jsx('div', { children: questions.map(q => jsxs('div', { style: { ...styles.td, marginBottom: 8 }, children: [
    jsx('strong', { children: 'Customer identity review' }),
    jsx('div', { children: `${q.number}: ${q.detail}. Affected invoices: ${q.affected_invoices.join(', ') || 'none known yet'}.` }),
    jsx('div', { style: styles.muted, children: 'Equal names do not prove customer identity. Collection and acknowledgements wait for verification of original source IDs with /cashdesk identities. Allocations and sent history are unchanged.' }),
  ] }, q.number)) })
}

function ReturnQuestions({ questions }) {
  return jsx('div', { children: questions.map(q => jsxs('div', { style: { ...styles.td, marginBottom: 8 }, children: [
    jsx('strong', { children: 'Returned payment review' }),
    jsx('div', { children: `Plausible receipts: ${q.receipts.join(', ') || 'unknown'}; affected invoices: ${q.affected_invoices.join(', ') || 'none known yet'}.` }),
    jsx('div', { style: styles.muted, children: 'Collection and acknowledgements are paused. Balances and allocations are unchanged. Verify bank and accounting evidence with /cashdesk returns; a genuine bounced allocation needs an audited correction.' }),
  ] }, q.id)) })
}

function BankEvidenceQuestions({ questions }) {
  return jsx('div', { children: questions.map(q => jsxs('div', { style: { ...styles.td, marginBottom: 8 }, children: [
    jsx('strong', { children: q.evidence?.kind === 'payer' ? 'Payer legal identity needs review' : q.evidence?.kind === 'reference' ? 'Reference evidence needs review' : q.evidence?.kind === 'currency' ? 'Bank currency evidence conflicts' : 'Bank payer/reference evidence conflicts' }),
    jsx('div', { children: `${q.file} row ${q.row_number}: ${q.detail}. Named invoices: ${q.named.join(', ') || 'unknown'}.` }),
    q.evidence?.kind === 'payer' ? jsx('div', { children: `Bank payer: ${(q.evidence.payers ?? []).join(', ')}; allocated customer: ${(q.evidence.customer_names ?? []).join(', ')}. Verify legal identity against the original bank and customer records.` }) : null,
    q.evidence?.kind === 'reference' ? jsx('div', { children: `Unknown invoice/reference identifiers: ${(q.evidence.unknown_references ?? []).join(', ') || 'none; verify identifier role and original allocation'}.` }) : null,
    jsx('div', { style: styles.muted, children: 'All allocation, collection and acknowledgements are paused across the whole ledger. Verify the original bank evidence and current ledger with /cashdesk bank-evidence. Balances, allocations and sent history are unchanged.' }),
  ] }, q.id)) })
}

function Review({ payments, returns, identities, bankEvidence, apply, dismiss }) {
  if (!payments.length) return jsx('div', { style: styles.muted, children: bankEvidence.length ? 'Bank evidence questions above await verification.' : identities.length ? 'Customer identity questions above await verification.' : returns.length ? 'Returned payment questions above await verification.' : 'No payment is waiting for a person.' })
  return jsxs('table', { style: styles.table, children: [
    jsx('thead', { children: jsx('tr', { children: ['Payment', 'From', 'Why it was not applied', 'Closest invoices'].map((h, i) => jsx('th', { style: styles.th, children: h }, i)) }) }),
    jsx('tbody', { children: payments.map(p => jsxs('tr', { children: [
      jsxs('td', { style: styles.td, children: [jsx('div', { style: { ...styles.figure, whiteSpace: 'nowrap' }, children: p.amount_display }), jsx('div', { style: { ...styles.muted, whiteSpace: 'nowrap' }, children: `${p.date} · ${p.id}` })] }),
      jsxs('td', { style: styles.td, children: [jsx('div', { children: p.counterparty || '—' }), p.reference ? jsx('div', { style: styles.muted, children: p.reference }) : null] }),
      jsxs('td', { style: { ...styles.td, ...styles.muted }, children: [p.why,
        p.incoming ? jsx('div', { style: { marginTop: 6 }, children: jsx(Button, { size: 'xs', variant: 'ghost', title: 'This receipt pays none of these invoices',
          onClick: () => dismiss.mutate({ path: `/payments/${encodeURIComponent(p.id)}/dismiss`, body: { reason: 'dismissed from the Desktop page' } }), children: 'Dismiss' }) }) : null] }),
      jsx('td', { style: styles.td, children: (p.candidates ?? []).length ? p.candidates.map(c =>
        jsxs('div', { style: { ...styles.row, gap: 6, marginBottom: 4 }, children: [jsxs('span', { children: [c.invoice_number, ' · ', c.customer, ' · ', jsx('span', { style: { ...styles.figure, whiteSpace: 'nowrap' }, children: c.balance_display })] }),
          p.incoming ? jsx(Button, { size: 'xs', variant: 'secondary', title: `Apply this payment to ${c.invoice_number} (never more than is open)`,
            onClick: () => apply.mutate({ path: `/payments/${encodeURIComponent(p.id)}/apply`, body: { invoice_number: c.invoice_number } }), children: 'Apply' }) : null] }, c.invoice_number)) : jsx('span', { style: styles.muted, children: 'none' }) }),
    ] }, p.id)) }),
  ] })
}

function Holds({ customers, release }) {
  if (!customers.length) return jsx('div', { style: styles.muted, children: 'No customer is on hold.' })
  return jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 6 }, children: customers.map(c =>
    jsxs('div', { style: { ...styles.row, justifyContent: 'space-between' }, children: [
      jsxs('div', { children: [jsx('strong', { children: c.name }), jsx('div', { style: styles.muted, children: c.hold_reason })] }),
      c.releasable === false ? jsx(Badge, { children: 'dispute' }) :
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => release.mutate({ path: `/customers/${encodeURIComponent(c.id)}/release` }), children: 'Release' }),
    ] }, c.id)) })
}

function Activity({ events }) {
  if (!events.length) return jsx('div', { style: styles.muted, children: 'No activity yet.' })
  return jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 }, children: events.map((e, i) =>
    jsxs('div', { style: styles.muted, children: [jsx('span', { style: styles.figure, children: localTime(e.at) }), ' · ', e.text ?? e.kind] }, i)) })
}

function Page() {
  const status = useStatus()
  const drafts = useDrafts()
  const holds = useHolds()
  const events = useEvents()
  const review = useReview()
  const approve = useAction('Draft approved. Ask the agent to send it with cashdesk_send; Hermes asks you once more.')
  const hold = useAction('Customer held; their drafts wait until you release them.')
  const reject = useAction('Draft rejected.')
  const release = useAction('Customer released; held drafts are pending again.')
  const applyPayment = useAction('Payment applied. Reminders that stated the old amount are redrafted on the next draft run.')
  const dismissPayment = useAction('Payment dismissed: it pays none of these invoices.')
  const demo = useAction("Demo data loaded (made-up companies, dated so that today is the demo day). Ask the agent to draft today's reminders; drafts appear here.")
  if (status.isLoading) return jsx('div', { style: styles.page, children: jsx(GlyphSpinner, {}) })
  if (status.isError) return jsx('div', { style: styles.page, children: jsx(ErrorState, { title: 'Cash Desk backend unreachable', description: 'Enable cash-desk under plugins.enabled for this profile and restart the gateway.' }) })
  const snap = status.data ?? {}
  if (snap.ledger === 'empty') {
    return jsxs('div', { style: styles.page, children: [
      jsx(EmptyState, { title: 'No ledger yet', description: 'Import customers, invoices and a bank statement with the cashdesk_import tool, then match and draft. Drafts appear here for your approval.' }),
      jsxs('div', { style: styles.row, children: [
        jsx(Button, { size: 'sm', variant: 'secondary', disabled: demo.isPending,
          title: 'Import the made-up demo customers, invoices and two bank days bundled with the plugin, moved to today, then match payments. Works only on an empty ledger; nothing is drafted or sent.',
          onClick: () => demo.mutate({ path: '/demo' }), children: demo.isPending ? 'Loading…' : 'Load demo data' }),
        jsx('span', { style: styles.muted, children: 'Made-up companies from the plugin\'s demo/ folder. Only an empty ledger accepts it.' }),
      ] }),
    ] })
  }
  return jsx(ScrollArea, { children: jsxs('div', { style: styles.page, children: [
    jsxs('div', { style: styles.row, children: [jsx('h1', { style: styles.h1, children: 'Cash Desk' }), jsx('span', { style: styles.muted, children: snap.chip }), snap.as_of ? jsx('span', { style: styles.muted, children: `as of ${snap.as_of}` }) : null] }),
    jsxs('section', { children: [jsx('h2', { style: styles.h2, children: 'Open balance by age' }), jsx(Buckets, { snap })] }),
    jsxs('section', { children: [jsx('h2', { style: styles.h2, children: 'Approval queue' }), jsx(Queue, { drafts: drafts.data?.drafts ?? [], approve, hold, reject })] }),
    jsxs('section', { children: [jsx('h2', { style: styles.h2, children: 'Payments to review' }), jsx('div', { style: { ...styles.muted, marginBottom: 6 }, children: 'Only an exact reference and amount from the right payer is applied automatically. These wait for you: Apply puts a payment on an invoice (never more than is open), Dismiss says it pays none of them.' }), jsx(BankEvidenceQuestions, { questions: review.data?.bank_evidence_questions ?? [] }), jsx(IdentityQuestions, { questions: review.data?.customer_identity_questions ?? [] }), jsx(ReturnQuestions, { questions: review.data?.return_questions ?? [] }), jsx(Review, { bankEvidence: review.data?.bank_evidence_questions ?? [], identities: review.data?.customer_identity_questions ?? [], returns: review.data?.return_questions ?? [], payments: review.data?.payments ?? [], apply: applyPayment, dismiss: dismissPayment })] }),
    jsxs('section', { children: [jsx('h2', { style: styles.h2, children: 'On hold' }), jsx(Holds, { customers: holds.data?.customers ?? [], release })] }),
    jsxs('section', { children: [jsx('h2', { style: styles.h2, children: 'Activity' }), jsx(Activity, { events: events.data?.events ?? [] })] }),
    jsx('div', { style: styles.muted, children: 'The model writes the words; the ledger owns the numbers. Approving here binds your approval to the exact text; nothing is sent from this page.' }),
  ] }) })
}

function Chip() {
  const status = useStatus()
  const label = status.data?.chip ?? 'Cash Desk'
  return jsx(Button, { size: 'xs', variant: 'ghost', title: 'Open Cash Desk', onClick: () => host.navigate(PATH), children: label })
}

export default {
  id: ID,
  name: 'Cash Desk',
  register(ctx) {
    pluginCtx = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(Page, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Cash Desk', codicon: 'credit-card' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 120, render: () => jsx(Chip, {}) },
      { id: 'open', area: PALETTE_AREA, data: { id: `${ID}.open`, label: 'Open Cash Desk', keywords: ['cash', 'invoices', 'overdue', 'reminders'], run: () => host.navigate(PATH) } },
    ])
  },
}
