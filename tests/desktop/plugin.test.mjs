// Execute the unmodified Desktop ESM with isolated SDK/React test doubles.
// No Hermes process, browser, network, approval or ledger is involved.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { createContext, SourceTextModule, SyntheticModule } from 'node:vm'

const entry = fileURLToPath(new URL('../../desktop/plugin.js', import.meta.url))
const source = readFileSync(entry, 'utf8')
const draft = {
  id: 'demo-reminder', customer: 'Example Client', customer_id: 'C1', kind: 'stage14',
  label: 'Reminder 14', lang: 'en', subject: 'Read this reminder', body: 'Please pay EUR 600.00.',
  recipients: ['accounts@client.example'], hash: 'a'.repeat(64), figures: [],
}

async function load(mode = 'ready') {
  const context = createContext({})
  const reads = [], actions = [], navigations = [], registrations = []
  const state = new Map()
  let hookScope = null
  const jsx = (type, props, key) => ({ type, props: props ?? {}, key })
  const component = tag => props => jsx(tag, props)
  const fixtures = {
    '/status': { ledger: mode === 'empty' ? 'empty' : 'ready', chip: 'EUR 600.00 overdue · 1 to approve',
      open_by_bucket_display: { EUR: { '1-30': 'EUR 600.00' } }, open_total_display: { EUR: 'EUR 600.00' } },
    '/drafts?status=pending': { drafts: [draft] },
    '/holds': { customers: [] }, '/review': { payments: [] }, '/events?limit=12': { events: [] },
  }
  if (mode === 'payer-evidence') fixtures['/review'] = { payments: [], bank_evidence_questions: [{ id: 'payer-question', file: 'retained receipt P1', row_number: 0, detail: 'incompatible legal forms need verification', named: ['INV-1001'], evidence: { kind: 'payer', payers: ['Acme GmbH'], customer_names: ['Acme Ltd'] } }] }
  if (mode === 'reference-evidence') fixtures['/review'] = { payments: [], bank_evidence_questions: [{ id: 'reference-question', file: 'retained receipt P1', row_number: 0, detail: 'old automatic allocation needs verification', named: ['INV-1001'], evidence: { kind: 'reference', unknown_references: ['INV-9999'] } }] }
  if (mode === 'currency-evidence') fixtures['/review'] = { payments: [], bank_evidence_questions: [{ id: 'currency-question', file: 'currency.csv', row_number: 3, detail: 'native currency evidence disagrees', named: ['INV-A1'], evidence: { kind: 'currency' } }] }
  if (mode === 'bank-evidence') fixtures['/review'] = { payments: [], bank_evidence_questions: [{ id: 'bank-question', file: 'bank.csv', row_number: 2, detail: 'payer aliases disagree', named: ['INV-A1', 'INV-B1'] }] }
  if (mode === 'identities') fixtures['/review'] = { payments: [], customer_identity_questions: [{ number: 'INV-A1', detail: 'original customer ID uncertain', affected_invoices: ['INV-A1'] }] }
  if (mode === 'returns') fixtures['/review'] = { payments: [], return_questions: [{ id: 'return-observation', receipts: ['P1'], affected_invoices: ['INV-A1'] }] }
  if (mode === 'no-data') {
    fixtures['/status'] = { ledger: 'ready' }
    fixtures['/drafts?status=pending'] = { drafts: [] }
  }
  const sdk = {
    host: { navigate: path => navigations.push(path),
      notify: () => assert.fail('Unexpected notification'), notifyError: () => assert.fail('Unexpected error notification') },
    useQuery(options) {
      assert.equal(options.refetchInterval, 5000)
      assert.equal(options.retry, 2)
      const data = options.queryFn()
      return { data, isLoading: mode === 'loading', isError: mode === 'error' }
    },
    useMutation: () => ({ isPending: false, mutate(args) { actions.push(args); assert.fail('A mutation must never run in a load test') } }),
    queryClient: { invalidateQueries: () => assert.fail('Unexpected invalidation') },
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'sidebar.nav', STATUSBAR_AREAS: { right: 'statusBar.right' }, PALETTE_AREA: 'palette',
    Button: component('button'), ScrollArea: component('section'),
    EmptyState: props => jsx('div', { children: [props.title, props.description] }),
    GlyphSpinner: () => jsx('span', { role: 'progressbar' }),
    // Badge/ErrorState intentionally absent: exercise the supported SDK fallbacks.
  }
  const react = { useState(initial) {
    assert.ok(hookScope, 'useState must run inside a component')
    const slot = `${hookScope.path}:${hookScope.index++}`
    if (!state.has(slot)) state.set(slot, initial)
    return [state.get(slot), value => state.set(slot, value)]
  } }
  const dependencies = new Map()
  for (const [name, exports] of [['@hermes/plugin-sdk', sdk], ['react', react], ['react/jsx-runtime', { jsx, jsxs: jsx }]]) {
    dependencies.set(name, new SyntheticModule(Object.keys(exports), function () {
      for (const [key, value] of Object.entries(exports)) this.setExport(key, value)
    }, { context, identifier: name }))
  }
  const module = new SourceTextModule(source, { context, identifier: entry })
  await module.link(specifier => {
    assert.ok(dependencies.has(specifier), `Unexpected import: ${specifier}`)
    return dependencies.get(specifier)
  })
  await module.evaluate()
  const plugin = module.namespace.default
  assert.equal(plugin.id, 'cash-desk')
  assert.equal(plugin.name, 'Cash Desk')
  plugin.register({ registerMany: items => registrations.push(...items), rest(path, options) {
    assert.equal(options, undefined, 'Rendering must only read the backend')
    assert.ok(Object.hasOwn(fixtures, path), `Unexpected backend read: ${path}`)
    reads.push(path)
    return fixtures[path]
  } })
  function expand(node, path = 'root') {
    if (Array.isArray(node)) return node.map((child, i) => expand(child, `${path}/${child?.key ?? i}`))
    if (node == null || typeof node !== 'object') return node
    if (typeof node.type === 'function') {
      const previous = hookScope
      hookScope = { path, index: 0 }
      let rendered
      try { rendered = node.type(node.props) } finally { hookScope = previous }
      return expand(rendered, `${path}/render`)
    }
    return { ...node, props: { ...node.props, children: expand(node.props.children, `${path}/children`) } }
  }
  const render = id => {
    const registration = registrations.find(item => item.id === id)
    assert.equal(typeof registration?.render, 'function', `${id} must have a render function`)
    return expand(registration.render(), id)
  }
  return { registrations, render, reads, actions, navigations }
}

function nodes(tree) {
  if (tree == null || typeof tree !== 'object') return []
  if (Array.isArray(tree)) return tree.flatMap(nodes)
  return [tree, ...nodes(tree.props?.children)]
}
function text(tree) {
  if (tree == null || typeof tree === 'boolean') return ''
  if (Array.isArray(tree)) return tree.map(text).join(' ')
  if (typeof tree === 'object') return text(tree.props?.children)
  return String(tree)
}

test('the actual module links and registers page, navigation, chip and palette', async () => {
  const app = await load()
  assert.deepEqual(app.registrations.map(item => item.id), ['page', 'nav', 'chip', 'open'])
  const [page, nav, chip, open] = app.registrations
  assert.equal(page.area, 'routes'); assert.equal(page.data.path, '/cash-desk')
  assert.equal(nav.area, 'sidebar.nav'); assert.equal(nav.data.label, 'Cash Desk'); assert.equal(nav.data.path, '/cash-desk')
  assert.equal(chip.area, 'statusBar.right'); assert.equal(open.area, 'palette')
  assert.equal(open.data.id, 'cash-desk.open'); assert.equal(open.data.label, 'Open Cash Desk')
  const pageTree = app.render('page')
  assert.match(text(pageTree), /Approval queue/)
  assert.match(text(pageTree), /EUR 600\.00/)
  const chipTree = app.render('chip')
  assert.equal(text(chipTree), 'EUR 600.00 overdue · 1 to approve')
  chipTree.props.onClick(); open.data.run()
  assert.deepEqual(app.navigations, ['/cash-desk', '/cash-desk'])
  assert.deepEqual([...new Set(app.reads)], ['/status', '/drafts?status=pending', '/holds', '/events?limit=12', '/review'])
  assert.deepEqual(app.actions, [])
})

test('approval controls load disabled until the exact body and hash are opened', async () => {
  const app = await load()
  let tree = app.render('page')
  const closed = nodes(tree).find(n => n.type === 'button' && text(n) === 'Read to approve')
  assert.ok(closed); assert.equal(closed.props.disabled, true)
  assert.equal(nodes(tree).some(n => n.type === 'pre'), false)
  const subject = nodes(tree).find(n => n.type === 'button' && text(n) === draft.subject)
  subject.props.onClick() // Read only: do not invoke Approve, Hold, Reject or demo actions.
  tree = app.render('page')
  const approve = nodes(tree).find(n => n.type === 'button' && text(n) === 'Approve')
  assert.ok(approve); assert.equal(approve.props.disabled, false)
  assert.equal(text(nodes(tree).find(n => n.type === 'pre')), draft.body)
  assert.ok(text(tree).includes(draft.hash))
  assert.ok(nodes(tree).some(n => n.type === 'button' && text(n) === 'Hold'))
  assert.ok(nodes(tree).some(n => n.type === 'button' && text(n) === 'Reject'))
  assert.deepEqual(app.actions, [])
})

for (const mode of ['empty', 'loading', 'error']) {
  test(`the page renders its ${mode} state without a mutation`, async () => {
    const app = await load(mode)
    const tree = app.render('page')
    if (mode === 'empty') assert.match(text(tree), /No ledger yet.*Load demo data/)
    if (mode === 'loading') assert.ok(nodes(tree).some(n => n.props.role === 'progressbar'))
    if (mode === 'error') assert.match(text(tree), /Cash Desk backend unreachable/)
    assert.deepEqual(app.actions, [])
  })
}

test('empty collections and a missing chip label render useful fallbacks', async () => {
  const app = await load('no-data')
  const page = text(app.render('page'))
  for (const message of ['No open invoices.', 'Nothing is waiting for approval.', 'No payment is waiting for a person.', 'No customer is on hold.', 'No activity yet.']) {
    assert.ok(page.includes(message), message)
  }
  assert.equal(text(app.render('chip')), 'Cash Desk')
  assert.deepEqual(app.actions, [])
})

test('the ESM compiler rejects the original unescaped-apostrophe class of error', () => {
  assert.throws(() => new SourceTextModule("export default 'today's reminders'"), SyntaxError)
})


test('return-only lifecycle evidence stays visible without a cash Apply action', async () => {
  const app = await load('returns')
  const tree = app.render('page')
  assert.match(text(tree), /Returned payment review/)
  assert.match(text(tree), /P1/)
  assert.match(text(tree), /INV-A1/)
  assert.match(text(tree), /allocations are unchanged/)
  assert.doesNotMatch(text(tree), /No payment is waiting for a person/)
  assert.equal(nodes(tree).filter(n => n.type === 'button' && /^(Apply|Dismiss)$/.test(text(n))).length, 0)
  assert.deepEqual(app.actions, [])
})


test('customer identity-only questions are visible without incoming cash placement', async () => {
  const app = await load('identities')
  const tree = app.render('page')
  assert.match(text(tree), /Customer identity review/)
  assert.match(text(tree), /INV-A1/)
  assert.match(text(tree), /original source IDs/)
  assert.doesNotMatch(text(tree), /No payment is waiting for a person/)
  assert.equal(nodes(tree).filter(n => n.type === 'button' && /^(Apply|Dismiss)$/.test(text(n))).length, 0)
  assert.deepEqual(app.actions, [])
})


test('bank alias questions stay visible without placement controls or false empty text', async () => {
  const app = await load('bank-evidence')
  const tree = app.render('page')
  assert.match(text(tree), /Bank payer\/reference evidence conflicts/)
  assert.match(text(tree), /bank.csv row 2/)
  assert.match(text(tree), /INV-A1, INV-B1/)
  assert.match(text(tree), /whole ledger/)
  assert.doesNotMatch(text(tree), /No payment is waiting for a person/)
  assert.equal(nodes(tree).filter(n => n.type === 'button' && /^(Apply|Dismiss)$/.test(text(n))).length, 0)
  assert.deepEqual(app.actions, [])
})


test('bank currency questions disclose paused authority without incoming cash controls', async () => {
  const app = await load('currency-evidence')
  const tree = app.render('page')
  assert.match(text(tree), /Bank currency evidence conflicts/)
  assert.match(text(tree), /currency.csv row 3/)
  assert.match(text(tree), /whole ledger/)
  assert.doesNotMatch(text(tree), /No payment is waiting for a person/)
  assert.equal(nodes(tree).filter(n => n.type === 'button' && /^(Apply|Dismiss)$/.test(text(n))).length, 0)
  assert.deepEqual(app.actions, [])
})


test('historical reference questions show unknown targets without incoming Apply controls', async () => {
  const app = await load('reference-evidence')
  const tree = app.render('page')
  assert.match(text(tree), /Reference evidence needs review/)
  assert.match(text(tree), /INV-1001/)
  assert.match(text(tree), /INV-9999/)
  assert.match(text(tree), /whole ledger/)
  assert.doesNotMatch(text(tree), /No payment is waiting for a person/)
  assert.equal(nodes(tree).filter(n => n.type === 'button' && /^(Apply|Dismiss)$/.test(text(n))).length, 0)
  assert.deepEqual(app.actions, [])
})


test('historical legal entity questions show both names without incoming cash actions', async () => {
  const app = await load('payer-evidence')
  const tree = app.render('page')
  assert.match(text(tree), /Payer legal identity needs review/)
  assert.match(text(tree), /Acme GmbH/)
  assert.match(text(tree), /Acme Ltd/)
  assert.match(text(tree), /whole ledger/)
  assert.doesNotMatch(text(tree), /No payment is waiting for a person/)
  assert.equal(nodes(tree).filter(n => n.type === 'button' && /^(Apply|Dismiss)$/.test(text(n))).length, 0)
  assert.deepEqual(app.actions, [])
})
