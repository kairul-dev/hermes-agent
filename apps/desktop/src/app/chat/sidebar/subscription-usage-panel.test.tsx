import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/store/gateway', () => ({ requestGatewayForAgent: vi.fn() }))

import { $apiRequestScope } from '@/api/client'
import type { SubscriptionUsage, UsageProvider } from '@/api/subscription-usage'
import { fmtDayTime } from '@/lib/time'
import { requestGatewayForAgent } from '@/store/gateway'
import { $gatewayState } from '@/store/session'
import { deferred } from '@/test/deferred'

import {
  persistUsageProviderSelection,
  usageProviderFilterKey,
  usageScopeKey
} from './subscription-usage-filter'
import { SubscriptionUsagePanel, UsagePanelView, type UsagePanelViewProps } from './subscription-usage-panel'

const noop = () => {}

// Radix popovers use pointer capture and scrollIntoView; jsdom has neither, so
// opening the provider chooser needs the same shims the dropdown-menu tests use.
beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
})

const usageData = (providers: UsageProvider[]): SubscriptionUsage => ({
  schema_version: 1,
  updated_at: '2026-10-05T02:00:00.000Z',
  providers
})

const provider = (overrides: Partial<UsageProvider> = {}): UsageProvider => ({
  id: 'p',
  label: 'Provider',
  state: 'available',
  kind: 'subscription',
  detail: '',
  windows: [],
  ...overrides
})

const view = (props: Partial<UsagePanelViewProps> = {}) =>
  render(
    <UsagePanelView
      collapsed={false}
      data={null}
      failed={false}
      loading={false}
      onRefresh={noop}
      onToggleCollapsed={noop}
      profile="default"
      ready
      {...props}
    />
  )

afterEach(cleanup)

describe('UsagePanelView structure', () => {
  it('renders header, footer, and one collapsed row per provider inside the card', () => {
    const providers = Array.from({ length: 6 }, (_, index) => provider({ id: `p${index}`, label: `Provider ${index}` }))

    view({ data: usageData(providers) })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })

    expect(panel.className).toContain('max-h-[190px]')
    expect(panel.querySelectorAll('details')).toHaveLength(6)

    for (const row of panel.querySelectorAll('details')) {
      expect(row.open).toBe(false)
      expect(row.querySelector('summary')).not.toBeNull()
    }

    expect(within(panel).getByRole('button', { name: 'Collapse subscription usage' })).toBeTruthy()
    expect(within(panel).getByRole('button', { name: 'Refresh subscription usage' })).toBeTruthy()
    expect(panel.querySelector('[data-testid="subscription-usage-updated"]')).not.toBeNull()
    expect(panel.textContent).toContain('Provider 5')
    expect(panel.textContent).toContain('Updated')
    expect(panel.textContent).toContain('default')

    // The provider list is the only scroller; header and footer stay fixed
    // outside it (so six providers never push the timestamp out of the card).
    const scroller = panel.querySelector('.overflow-y-auto')

    expect(scroller).not.toBeNull()
    expect(scroller?.className).toContain('min-h-0')
    expect(scroller?.querySelector('footer')).toBeNull()
    expect(scroller?.querySelector('header')).toBeNull()
  })

  it('renders a mixed set of states and kinds together', () => {
    view({
      data: usageData([
        provider({ id: 'sub', label: 'Claude', windows: [{ label: 'Weekly', remaining_percent: 42, reset_at: null }] }),
        provider({ id: 'bal', kind: 'balance', label: 'OpenRouter', balance: { amount: '12.50', currency: 'USD' } }),
        provider({ id: 'loc', kind: 'local', label: 'Local models' }),
        provider({ id: 'down', label: 'Down service', state: 'unavailable' }),
        provider({ id: 'err', label: 'Broken service', state: 'error', detail: 'API key rejected' })
      ])
    })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })

    expect(panel.querySelectorAll('details')).toHaveLength(5)
    expect(screen.getByText('42% left')).toBeTruthy()
    expect(screen.getByText('12.50 USD')).toBeTruthy()
    expect(screen.getByText('Local')).toBeTruthy()
    expect(screen.getAllByText('Unavailable')).toHaveLength(2)
    expect(screen.getByText('API key rejected')).toBeTruthy()
  })

  it('never uses fixed-position or absolute overlays', () => {
    view({ data: usageData([provider({ label: 'Claude' })]) })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })

    expect(panel.querySelectorAll('[class*="absolute"], [class*="fixed"]')).toHaveLength(0)
  })

  it('reports loading feedback on the refresh control without hiding the card', () => {
    view({ loading: true, ready: true })

    const refresh = screen.getByRole('button', { name: 'Refresh subscription usage' })

    expect(refresh.getAttribute('aria-busy')).toBe('true')
    expect(screen.getByText('Loading usage…')).toBeTruthy()
  })

  it('invokes onRefresh without navigating or focusing anything else', () => {
    const onRefresh = vi.fn()

    view({ data: usageData([provider()]), onRefresh })

    fireEvent.click(screen.getByRole('button', { name: 'Refresh subscription usage' }))

    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('reflects the collapsed state with accessible labels and hides the body', () => {
    const onToggleCollapsed = vi.fn()
    const collapsed = view({ collapsed: true, data: usageData([provider({ label: 'Claude' })]), onToggleCollapsed })

    expect(screen.queryByText('Claude')).toBeNull()

    const expand = screen.getByRole('button', { name: 'Expand subscription usage' })

    expect(expand.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(expand)
    expect(onToggleCollapsed).toHaveBeenCalledTimes(1)

    collapsed.rerender(
      <UsagePanelView
        collapsed={false}
        data={usageData([provider({ label: 'Claude' })])}
        failed={false}
        loading={false}
        onRefresh={noop}
        onToggleCollapsed={onToggleCollapsed}
        profile="default"
        ready
      />
    )

    expect(screen.getByText('Claude')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Collapse subscription usage' }).getAttribute('aria-expanded')).toBe(
      'true'
    )
  })
})

describe('UsagePanelView states', () => {
  it('shows the waiting message until the gateway is ready', () => {
    view({ ready: false })

    expect(screen.getByText('Waiting for gateway…')).toBeTruthy()
  })

  it('shows a loading message while the first fetch is in flight', () => {
    view({ loading: true, ready: true })

    expect(screen.getByText('Loading usage…')).toBeTruthy()
  })

  it('shows a fixed, credential-free failure message when nothing loaded', () => {
    view({ failed: true, ready: true })

    expect(screen.getByText("Couldn't load usage")).toBeTruthy()
  })

  it('shows an empty-provider message for an account with no connected providers', () => {
    view({ data: usageData([]) })

    expect(screen.getByText('No connected providers')).toBeTruthy()
  })

  it('keeps the last good quota and marks it stale when a refresh fails', () => {
    view({ data: usageData([provider({ label: 'Claude' })]), failed: true, ready: true })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })
    const updated = panel.querySelector<HTMLTimeElement>('[data-testid="subscription-usage-updated"]')

    expect(panel.textContent).toContain('Claude')
    expect(panel.textContent).toContain("Couldn't refresh — showing earlier data")
    expect(updated?.dateTime).toBe('2026-10-05T02:00:00.000Z')
  })

  it('shows refreshing feedback while keeping the rows visible', () => {
    view({ data: usageData([provider({ label: 'Claude' })]), loading: true, ready: true })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })

    expect(panel.textContent).toContain('Refreshing…')
    expect(panel.textContent).toContain('Claude')
  })
})

describe('UsagePanelView provider rows', () => {
  const rowFor = (label: string) => {
    const row = screen.getByText(label).closest('details')

    expect(row).not.toBeNull()

    return row as HTMLElement
  }

  it('summarizes an available subscription with the MIN remaining percent across its windows', () => {
    view({
      data: usageData([
        provider({
          label: 'Claude',
          windows: [
            { label: 'Session', remaining_percent: 80, reset_at: null },
            { label: 'Weekly', remaining_percent: 42, reset_at: null },
            { label: 'Monthly', remaining_percent: 66.6, reset_at: null }
          ]
        })
      ])
    })

    expect(within(rowFor('Claude')).getByText('42% left')).toBeTruthy()
  })

  it('floors a fractional minimum so the summary never overstates the quota', () => {
    view({
      data: usageData([
        provider({ label: 'Claude', windows: [{ label: 'Weekly', remaining_percent: 12.34, reset_at: null }] })
      ])
    })

    expect(within(rowFor('Claude')).getByText('12.3% left')).toBeTruthy()
  })

  it('shows a balance as the reported currency amount, never a fabricated percentage', () => {
    view({
      data: usageData([
        provider({
          id: 'openrouter',
          kind: 'balance',
          label: 'OpenRouter',
          balance: { amount: '12.50', currency: 'USD' }
        })
      ])
    })

    const row = rowFor('OpenRouter')

    expect(within(row).getByText('12.50 USD')).toBeTruthy()
    expect(within(row).getByText('Balance: 12.50 USD')).toBeTruthy()
    expect(row.textContent).not.toContain('%')
  })

  it('labels local and unavailable providers without inventing a metric', () => {
    view({
      data: usageData([
        provider({ id: 'local', kind: 'local', label: 'Local models', state: 'unavailable' }),
        provider({ id: 'down', label: 'Down service', state: 'unavailable' }),
        provider({ id: 'broken', label: 'Broken service', state: 'error', detail: 'API key rejected' })
      ])
    })

    expect(within(rowFor('Local models')).getByText('Local')).toBeTruthy()
    expect(screen.getAllByText('Unavailable')).toHaveLength(2)
    // The error's reason rides from `detail` into the expanded row body.
    expect(within(rowFor('Broken service')).getByText('API key rejected')).toBeTruthy()
  })

  it('renders no metric, ratio, or bar when the data is missing or incomplete', () => {
    view({
      data: usageData([
        provider({ id: 'half', label: 'Half data' }),
        provider({ id: 'nobal', kind: 'balance', label: 'No balance set' })
      ])
    })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })

    expect(panel.querySelectorAll('[role="progressbar"]')).toHaveLength(0)
    expect(panel.textContent).not.toContain('%')
    expect(within(rowFor('Half data')).queryByText('Unavailable')).toBeNull()
  })

  it('lists each window remaining and reset in the row and mirrors it in the summary title', () => {
    const resetAt = '2026-10-12T09:00:00.000Z'

    view({
      data: usageData([
        provider({
          label: 'Claude',
          windows: [
            { label: 'Weekly', remaining_percent: 42, reset_at: resetAt },
            { label: 'Monthly', remaining_percent: 10, reset_at: null }
          ]
        })
      ])
    })

    const row = rowFor('Claude')

    expect(within(row).getByText(`Weekly: 42% left, resets ${fmtDayTime.format(new Date(resetAt))}`)).toBeTruthy()
    expect(within(row).getByText('Monthly: 10% left')).toBeTruthy()

    const title = row.querySelector('summary')?.getAttribute('title') ?? ''

    expect(title).toContain('Claude')
    expect(title).toContain('42% left')
    expect(title).toContain('Weekly')
  })

  it('shows the plan only when the backend reports one, never assuming a paid tier', () => {
    view({
      data: usageData([
        provider({ id: 'with', label: 'WithPlan', plan: 'Pro' }),
        provider({ id: 'without', label: 'NoPlan' })
      ])
    })

    expect(screen.getByText('Plan: Pro')).toBeTruthy()
    expect(rowFor('NoPlan').textContent).not.toContain('Plan:')
    expect(rowFor('WithPlan').textContent).not.toContain('Paid')
  })

  it('renders provider text as plain text, never as markup', () => {
    const nasty = '<img src=x onerror="alert(1)">'

    view({ data: usageData([provider({ label: nasty, detail: `${nasty} detail` })]) })

    const panel = screen.getByRole('region', { name: 'Subscription usage' })
    const row = rowFor(nasty)

    // No element was created from the injected string: it stays a text node
    // (the only raw-HTML sink in React would be dangerouslySetInnerHTML).
    expect(panel.querySelector('img')).toBeNull()
    expect(panel.querySelector('script')).toBeNull()
    expect(within(row).getByText(nasty)).toBeTruthy()
    expect(row.querySelector('summary span')?.textContent).toBe(nasty)
    expect(panel.textContent).toContain(nasty)
  })
})

describe('UsagePanelView provider chooser', () => {
  const threeProviders = () =>
    usageData([
      provider({ id: 'alpha', label: 'Alpha' }),
      provider({ id: 'beta', label: 'Beta' }),
      provider({ id: 'gamma', label: 'Gamma' })
    ])

  const openChooser = () => {
    const trigger = screen.getByRole('button', { name: 'Choose providers' })

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
  }

  const panel = () => screen.getByRole('region', { name: 'Subscription usage' })

  it('renders no chooser control when no selection handler is provided', () => {
    view({ data: threeProviders() })

    expect(screen.queryByRole('button', { name: 'Choose providers' })).toBeNull()
  })

  it('keeps its chooser in the header while the card is collapsed, even with none selected', () => {
    view({ collapsed: true, data: threeProviders(), onSelectionChange: noop, selection: [] })

    const region = panel()

    expect(within(region).getByRole('button', { name: 'Choose providers' })).toBeTruthy()
    expect(within(region).queryByText('Alpha')).toBeNull()
  })

  it('shows an explicit none selection as an empty view with the chooser still available', () => {
    view({ data: threeProviders(), onSelectionChange: noop, selection: [] })

    expect(within(panel()).getByText('No providers selected')).toBeTruthy()
    expect(within(panel()).queryByText('No connected providers')).toBeNull()
    expect(within(panel()).getByRole('button', { name: 'Choose providers' })).toBeTruthy()
  })

  it('filters the rows to an explicit whitelist while all mode keeps a later-discovered provider', () => {
    const { rerender } = view({ data: threeProviders(), selection: ['beta'] })

    expect(within(panel()).queryByText('Alpha')).toBeNull()
    expect(within(panel()).getByText('Beta')).toBeTruthy()
    expect(within(panel()).queryByText('Gamma')).toBeNull()

    rerender(
      <UsagePanelView
        collapsed={false}
        data={usageData([
          provider({ id: 'alpha', label: 'Alpha' }),
          provider({ id: 'beta', label: 'Beta' }),
          provider({ id: 'gamma', label: 'Gamma' }),
          provider({ id: 'delta', label: 'Delta' })
        ])}
        failed={false}
        loading={false}
        onRefresh={noop}
        onSelectionChange={noop}
        onToggleCollapsed={noop}
        profile="default"
        ready
        selection={null}
      />
    )

    expect(within(panel()).getByText('Delta')).toBeTruthy()
  })

  it('opens a checkbox list that reflects the current selection without hiding unchecked providers', () => {
    view({ data: threeProviders(), onSelectionChange: noop, selection: ['beta'] })

    openChooser()

    expect(screen.getByRole('checkbox', { name: 'Alpha' }).getAttribute('aria-checked')).toBe('false')
    expect(screen.getByRole('checkbox', { name: 'Beta' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('checkbox', { name: 'Gamma' }).getAttribute('aria-checked')).toBe('false')
  })

  it('reports a combined whitelist when one provider is unchecked from all mode', () => {
    const onSelectionChange = vi.fn()

    view({ data: threeProviders(), onSelectionChange })

    openChooser()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Beta' }))

    expect(onSelectionChange).toHaveBeenCalledWith(['alpha', 'gamma'])
  })

  it('marks every provider as selected while all mode is active', () => {
    view({ data: threeProviders(), onSelectionChange: noop, selection: null })

    openChooser()

    for (const name of ['Alpha', 'Beta', 'Gamma']) {
      expect(screen.getByRole('checkbox', { name }).getAttribute('aria-checked')).toBe('true')
    }
  })

  it('reports the null whitelist from Show all and the empty whitelist from Clear selection', () => {
    const onSelectionChange = vi.fn()

    view({ data: threeProviders(), onSelectionChange, selection: ['beta'] })

    openChooser()
    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))
    expect(onSelectionChange).toHaveBeenCalledWith(null)

    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }))
    expect(onSelectionChange).toHaveBeenCalledWith([])
  })

  it('says the selection cannot be saved when persistence failed and never claims otherwise', () => {
    const { rerender } = view({
      data: threeProviders(),
      onSelectionChange: noop,
      selection: ['beta'],
      selectionPersistFailed: true
    })

    openChooser()

    expect(screen.getByText("Couldn't save selection — it applies for this session only")).toBeTruthy()

    rerender(
      <UsagePanelView
        collapsed={false}
        data={threeProviders()}
        failed={false}
        loading={false}
        onRefresh={noop}
        onSelectionChange={noop}
        onToggleCollapsed={noop}
        profile="default"
        ready
        selection={['beta']}
      />
    )

    expect(screen.queryByText("Couldn't save selection — it applies for this session only")).toBeNull()
  })
})

// ── The stateful wrapper: scope handling, stale rejection, refresh safety ──

const requestMock = vi.mocked(requestGatewayForAgent)

interface CliReply {
  blocked: boolean
  code: number
  output: string
}

const cliReply = (usage: SubscriptionUsage): CliReply => ({
  blocked: false,
  code: 0,
  output: JSON.stringify(usage)
})

const usagePayload = (
  providers: Array<Partial<UsageProvider>>,
  updatedAt = '2026-10-05T02:00:00.000Z'
): SubscriptionUsage => ({
  schema_version: 1,
  updated_at: updatedAt,
  providers: providers.map(partial => ({
    id: partial.id ?? partial.label ?? 'provider',
    label: 'Provider',
    state: 'available',
    kind: 'subscription',
    detail: '',
    windows: [],
    ...partial
  }))
})

/** Queue one reply per request, FIFO. A deferred promise keeps a call in flight. */
const respond = (...replies: Array<CliReply | Promise<CliReply> | Promise<never>>) => {
  for (const reply of replies) {
    requestMock.mockImplementationOnce(() => Promise.resolve(reply))
  }
}

beforeEach(() => {
  requestMock.mockReset()
  $gatewayState.set('open')
  $apiRequestScope.set({ connectionId: null, profile: 'default' })
})

afterEach(() => {
  cleanup()
  $gatewayState.set('idle')
  $apiRequestScope.set({ connectionId: null, profile: null })
})

describe('SubscriptionUsagePanel', () => {
  it('does not query while the gateway is closed, then queries the captured scope once it opens', async () => {
    $gatewayState.set('idle')
    $apiRequestScope.set({ connectionId: 'conn-1', profile: 'beta' })
    respond(cliReply(usagePayload([{ label: 'Beta provider' }])))

    render(<SubscriptionUsagePanel />)
    await act(async () => {})

    expect(requestMock).not.toHaveBeenCalled()

    act(() => $gatewayState.set('open'))

    await waitFor(() => expect(requestMock).toHaveBeenCalledTimes(1))
    expect(requestMock).toHaveBeenCalledWith(
      'conn-1',
      'beta',
      'cli.exec',
      { argv: ['usage', '--all', '--json'], timeout: 25 },
      30_000
    )
    expect(await screen.findByText('Beta provider')).toBeTruthy()
  })

  it('renders the loaded providers, timestamp, and scope profile', async () => {
    respond(
      cliReply(
        usagePayload([{ label: 'Claude', windows: [{ label: 'Weekly', remaining_percent: 42, reset_at: null }] }])
      )
    )

    render(<SubscriptionUsagePanel />)

    expect(await screen.findByText('Claude')).toBeTruthy()
    expect(screen.getByText('42% left')).toBeTruthy()

    const panel = screen.getByRole('region', { name: 'Subscription usage' })
    const updated = panel.querySelector<HTMLTimeElement>('[data-testid="subscription-usage-updated"]')

    // The footer timestamp is the payload's updated_at — never the attempt time.
    expect(updated?.dateTime).toBe('2026-10-05T02:00:00.000Z')
    expect(within(panel).getByText('default')).toBeTruthy()
  })

  it('resets the displayed quota immediately when the profile scope changes', async () => {
    const beta = deferred<CliReply>()

    $apiRequestScope.set({ connectionId: null, profile: 'alpha' })
    respond(cliReply(usagePayload([{ label: 'Alpha provider' }])), beta.promise)

    render(<SubscriptionUsagePanel />)
    await screen.findByText('Alpha provider')

    act(() => $apiRequestScope.set({ connectionId: null, profile: 'beta' }))

    // The previous scope's quota is hidden the moment the scope changes…
    expect(screen.queryByText('Alpha provider')).toBeNull()
    expect(screen.getByText('Loading usage…')).toBeTruthy()
    // …and the new scope is queried with its own captured identity.
    expect(requestMock).toHaveBeenNthCalledWith(
      2,
      null,
      'beta',
      'cli.exec',
      { argv: ['usage', '--all', '--json'], timeout: 25 },
      30_000
    )

    beta.resolve(cliReply(usagePayload([{ label: 'Beta provider' }])))

    expect(await screen.findByText('Beta provider')).toBeTruthy()
    expect(screen.queryByText('Alpha provider')).toBeNull()
  })

  it('rejects a late response from a previous scope instead of clobbering the new one', async () => {
    const alpha = deferred<CliReply>()
    const beta = deferred<CliReply>()

    $apiRequestScope.set({ connectionId: null, profile: 'alpha' })
    respond(alpha.promise, beta.promise)

    render(<SubscriptionUsagePanel />)
    await act(async () => {})

    act(() => $apiRequestScope.set({ connectionId: null, profile: 'beta' }))
    beta.resolve(cliReply(usagePayload([{ label: 'Beta provider' }])))
    expect(await screen.findByText('Beta provider')).toBeTruthy()

    // The stale alpha reply lands last — it must be ignored, not overwrite beta.
    alpha.resolve(cliReply(usagePayload([{ label: 'Alpha provider' }])))
    await act(async () => {})

    expect(screen.getByText('Beta provider')).toBeTruthy()
    expect(screen.queryByText('Alpha provider')).toBeNull()
  })

  it('queries a reset (null-profile) scope as the default profile', async () => {
    $apiRequestScope.set({ connectionId: null, profile: 'beta' })
    respond(
      cliReply(usagePayload([{ label: 'Beta provider' }])),
      cliReply(usagePayload([{ label: 'Default provider' }]))
    )

    render(<SubscriptionUsagePanel />)
    await screen.findByText('Beta provider')

    act(() => $apiRequestScope.set({ connectionId: null, profile: null }))

    expect(await screen.findByText('Default provider')).toBeTruthy()
    expect(requestMock).toHaveBeenNthCalledWith(
      2,
      null,
      'default',
      'cli.exec',
      { argv: ['usage', '--all', '--json'], timeout: 25 },
      30_000
    )
  })

  it('shows a fixed credential-free failure state when the fetch fails', async () => {
    const failing = deferred<CliReply>()

    respond(failing.promise)

    const { container } = render(<SubscriptionUsagePanel />)

    await act(async () => {})
    failing.reject(new Error('gateway exploded with sk-live-xyz'))

    expect(await screen.findByText("Couldn't load usage")).toBeTruthy()
    expect(container.textContent).not.toContain('sk-live-xyz')
  })

  it('keeps the last good quota and its timestamp when a refresh fails', async () => {
    respond(cliReply(usagePayload([{ label: 'Claude' }])))

    const { container } = render(<SubscriptionUsagePanel />)

    await screen.findByText('Claude')

    const updatedBefore = screen.getByTestId('subscription-usage-updated')
    const dateTimeBefore = updatedBefore.getAttribute('datetime')
    const textBefore = updatedBefore.textContent

    const refresh = deferred<CliReply>()

    respond(refresh.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh subscription usage' }))

    // Loading feedback shows while the old quota stays visible.
    expect(await screen.findByText('Refreshing…')).toBeTruthy()
    expect(screen.getByText('Claude')).toBeTruthy()

    refresh.reject(new Error('token sk-live-abc'))

    expect(await screen.findByText("Couldn't refresh — showing earlier data")).toBeTruthy()
    expect(screen.getByText('Claude')).toBeTruthy()

    // The displayed time still derives from the payload's updated_at — the
    // failed attempt's wall clock never replaces it.
    const updatedAfter = screen.getByTestId('subscription-usage-updated')

    expect(updatedAfter.getAttribute('datetime')).toBe(dateTimeBefore)
    expect(updatedAfter.textContent).toBe(textBefore)
    expect(container.textContent).not.toContain('sk-live-abc')
  })

  it('collapses and expands the card through the wrapper state', async () => {
    respond(cliReply(usagePayload([{ label: 'Claude' }])))

    render(<SubscriptionUsagePanel />)
    await screen.findByText('Claude')

    fireEvent.click(screen.getByRole('button', { name: 'Collapse subscription usage' }))
    expect(screen.queryByText('Claude')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Expand subscription usage' }))
    expect(screen.getByText('Claude')).toBeTruthy()
  })

  it('coalesces refresh clicks while a request is already in flight', async () => {
    respond(cliReply(usagePayload([{ label: 'Claude' }])))

    render(<SubscriptionUsagePanel />)
    await screen.findByText('Claude')

    const inFlight = deferred<CliReply>()

    respond(inFlight.promise, cliReply(usagePayload([{ label: 'Claude' }], '2026-10-05T03:00:00.000Z')))
    const refreshButton = () => screen.getByRole('button', { name: 'Refresh subscription usage' })

    fireEvent.click(refreshButton())
    fireEvent.click(refreshButton())

    // The second click rides the in-flight request instead of stacking another.
    expect(requestMock).toHaveBeenCalledTimes(2)

    inFlight.resolve(cliReply(usagePayload([{ label: 'Claude' }], '2026-10-05T03:00:00.000Z')))

    await waitFor(() =>
      expect(screen.getByTestId('subscription-usage-updated').getAttribute('datetime')).toBe('2026-10-05T03:00:00.000Z')
    )

    // Once settled, a further click is a fresh request again.
    fireEvent.click(refreshButton())
    await waitFor(() => expect(requestMock).toHaveBeenCalledTimes(3))
  })
})

/* eslint-disable no-restricted-globals -- visibility/resume polling is the behavior under test */
describe('SubscriptionUsagePanel polling', () => {
  const POLL_MS = 5 * 60_000

  const flushAsync = () => act(async () => void (await vi.advanceTimersByTimeAsync(0)))

  const allowReplies = (usage: SubscriptionUsage) => {
    requestMock.mockImplementation(() => Promise.resolve(cliReply(usage)))
  }

  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('polls at most every five minutes and pauses while the document is hidden', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')

    allowReplies(usagePayload([{ label: 'Claude' }]))

    render(<SubscriptionUsagePanel />)
    await flushAsync()

    expect(requestMock).toHaveBeenCalledTimes(1)

    // Nothing before the five-minute mark.
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS - 60_000)))
    expect(requestMock).toHaveBeenCalledTimes(1)

    await act(async () => void (await vi.advanceTimersByTimeAsync(60_000)))
    expect(requestMock).toHaveBeenCalledTimes(2)

    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS)))
    expect(requestMock).toHaveBeenCalledTimes(3)

    // Hidden: ticks are skipped, not queued up.
    visibility.mockReturnValue('hidden')
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    expect(requestMock).toHaveBeenCalledTimes(3)
  })

  it('refreshes on visibility resume only when the shown data is outdated', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const stale = usagePayload([{ label: 'Claude' }], new Date(Date.now() - 10 * 60_000).toISOString())
    const fresh = usagePayload([{ label: 'Claude' }], new Date(Date.now()).toISOString())

    respond(cliReply(stale), cliReply(fresh))

    render(<SubscriptionUsagePanel />)
    await flushAsync()

    expect(requestMock).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('subscription-usage-updated').getAttribute('datetime')).toBe(stale.updated_at)

    // A resume event while hidden never fetches.
    visibility.mockReturnValue('hidden')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    await flushAsync()
    expect(requestMock).toHaveBeenCalledTimes(1)

    // Resuming with outdated data fetches exactly once…
    visibility.mockReturnValue('visible')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    await flushAsync()
    expect(requestMock).toHaveBeenCalledTimes(2)

    // …and once the shown data is fresh, further resumes are inert.
    visibility.mockReturnValue('hidden')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    visibility.mockReturnValue('visible')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    await flushAsync()
    expect(requestMock).toHaveBeenCalledTimes(2)
  })

  it('stops polling and reacting to visibility once unmounted', async () => {
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')

    allowReplies(usagePayload([{ label: 'Claude' }]))

    const { unmount } = render(<SubscriptionUsagePanel />)

    await flushAsync()
    expect(requestMock).toHaveBeenCalledTimes(1)

    unmount()

    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 4)))
    visibility.mockReturnValue('visible')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    await flushAsync()

    expect(requestMock).toHaveBeenCalledTimes(1)
  })

  it('ignores an in-flight reply that lands after unmount', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')

    const pending = deferred<CliReply>()

    respond(pending.promise)

    const { unmount } = render(<SubscriptionUsagePanel />)

    await flushAsync()
    unmount()

    pending.resolve(cliReply(usagePayload([{ label: 'Claude' }])))
    await flushAsync()

    expect(requestMock).toHaveBeenCalledTimes(1)
  })

  it('never overlaps requests for the same scope', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')

    const inFlight = deferred<CliReply>()

    respond(
      cliReply(usagePayload([{ label: 'Claude' }])),
      inFlight.promise,
      cliReply(usagePayload([{ label: 'Claude' }])),
      cliReply(usagePayload([{ label: 'Claude' }]))
    )

    render(<SubscriptionUsagePanel />)
    await flushAsync()
    expect(requestMock).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh subscription usage' }))
    expect(requestMock).toHaveBeenCalledTimes(2)

    // Two poll windows elapse while the refresh is still unresolved — the
    // ticks must ride the in-flight request, not stack new ones.
    await act(async () => void (await vi.advanceTimersByTimeAsync(POLL_MS * 2)))
    expect(requestMock).toHaveBeenCalledTimes(2)

    inFlight.resolve(cliReply(usagePayload([{ label: 'Claude' }])))
    await flushAsync()
    expect(screen.getByText('Claude')).toBeTruthy()
  })
})

// ── The provider chooser's persistence: per scope, honest, crash-proof ──

describe('SubscriptionUsagePanel provider selection', () => {
  const panel = () => screen.getByRole('region', { name: 'Subscription usage' })

  const openChooser = () => {
    const trigger = screen.getByRole('button', { name: 'Choose providers' })

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)
  }

  beforeEach(() => {
    window.localStorage.clear()
  })

  afterEach(() => {
    window.localStorage.clear()
  })

  it('restores a saved whitelist for the same connection and profile after a remount', async () => {
    persistUsageProviderSelection(usageScopeKey(null, 'default'), ['Alpha'])

    respond(cliReply(usagePayload([{ label: 'Alpha' }, { label: 'Beta' }])))
    const first = render(<SubscriptionUsagePanel />)

    expect(await within(panel()).findByText('Alpha')).toBeTruthy()
    expect(within(panel()).queryByText('Beta')).toBeNull()

    first.unmount()

    respond(cliReply(usagePayload([{ label: 'Alpha' }, { label: 'Beta' }])))
    render(<SubscriptionUsagePanel />)

    expect(await within(panel()).findByText('Alpha')).toBeTruthy()
    expect(within(panel()).queryByText('Beta')).toBeNull()
  })

  it('saves the choice for this scope and refilters without refetching', async () => {
    respond(cliReply(usagePayload([{ label: 'Alpha' }, { label: 'Beta' }])))
    render(<SubscriptionUsagePanel />)
    await within(panel()).findByText('Beta')

    openChooser()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Beta' }))

    expect(within(panel()).queryByText('Beta')).toBeNull()
    expect(within(panel()).getByText('Alpha')).toBeTruthy()
    expect(window.localStorage.getItem(usageProviderFilterKey(usageScopeKey(null, 'default')))).toBe('["Alpha"]')
    // Display-only: the choice never triggers a fetch.
    expect(requestMock).toHaveBeenCalledTimes(1)
  })

  it('persists Show all and Clear selection as distinct states', async () => {
    respond(cliReply(usagePayload([{ label: 'Alpha' }, { label: 'Beta' }])))
    render(<SubscriptionUsagePanel />)
    await within(panel()).findByText('Beta')

    openChooser()
    fireEvent.click(screen.getByRole('button', { name: 'Clear selection' }))

    expect(within(panel()).getByText('No providers selected')).toBeTruthy()
    expect(window.localStorage.getItem(usageProviderFilterKey(usageScopeKey(null, 'default')))).toBe('[]')

    fireEvent.click(screen.getByRole('button', { name: 'Show all' }))

    expect(within(panel()).getByText('Beta')).toBeTruthy()
    expect(window.localStorage.getItem(usageProviderFilterKey(usageScopeKey(null, 'default')))).toBe('null')
  })

  it("never leaks one gateway's whitelist onto another scope that saved nothing", async () => {
    persistUsageProviderSelection(usageScopeKey('conn-1', 'alpha'), ['Alpha only'])

    $apiRequestScope.set({ connectionId: 'conn-1', profile: 'alpha' })
    respond(
      cliReply(usagePayload([{ label: 'Alpha only' }, { label: 'Shared' }])),
      cliReply(usagePayload([{ label: 'Beta only' }, { label: 'Shared' }]))
    )

    render(<SubscriptionUsagePanel />)
    await within(panel()).findByText('Alpha only')
    expect(within(panel()).queryByText('Shared')).toBeNull()

    act(() => $apiRequestScope.set({ connectionId: 'conn-2', profile: 'beta' }))

    expect(await within(panel()).findByText('Beta only')).toBeTruthy()
    expect(within(panel()).getByText('Shared')).toBeTruthy()
  })

  it("applies the new scope's own stored whitelist the moment the scope changes", async () => {
    persistUsageProviderSelection(usageScopeKey('conn-1', 'alpha'), ['Alpha only'])
    persistUsageProviderSelection(usageScopeKey('conn-2', 'beta'), ['Beta only'])

    $apiRequestScope.set({ connectionId: 'conn-1', profile: 'alpha' })
    respond(
      cliReply(usagePayload([{ label: 'Alpha only' }, { label: 'Shared' }])),
      cliReply(usagePayload([{ label: 'Beta only' }, { label: 'Shared' }]))
    )

    render(<SubscriptionUsagePanel />)
    await within(panel()).findByText('Alpha only')
    expect(within(panel()).queryByText('Shared')).toBeNull()

    act(() => $apiRequestScope.set({ connectionId: 'conn-2', profile: 'beta' }))

    expect(await within(panel()).findByText('Beta only')).toBeTruthy()
    expect(within(panel()).queryByText('Shared')).toBeNull()
    expect(within(panel()).queryByText('Alpha only')).toBeNull()
  })

  it('shows every provider when the saved preference is corrupt instead of crashing', async () => {
    window.localStorage.setItem(usageProviderFilterKey(usageScopeKey(null, 'default')), '{not json')

    respond(cliReply(usagePayload([{ label: 'Alpha' }, { label: 'Beta' }])))
    render(<SubscriptionUsagePanel />)

    expect(await within(panel()).findByText('Alpha')).toBeTruthy()
    expect(within(panel()).getByText('Beta')).toBeTruthy()
  })

  it('keeps the choice for this session and says it cannot be saved when storage rejects writes', async () => {
    respond(cliReply(usagePayload([{ label: 'Alpha' }, { label: 'Beta' }])))
    render(<SubscriptionUsagePanel />)
    await within(panel()).findByText('Beta')

    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage unavailable')
    })

    try {
      openChooser()
      fireEvent.click(screen.getByRole('checkbox', { name: 'Beta' }))

      // The choice still filters for this session…
      expect(within(panel()).queryByText('Beta')).toBeNull()
      expect(within(panel()).getByText('Alpha')).toBeTruthy()
      // …and the chooser says it cannot be saved instead of claiming it was.
      expect(screen.getByText("Couldn't save selection — it applies for this session only")).toBeTruthy()
      expect(window.localStorage.getItem(usageProviderFilterKey(usageScopeKey(null, 'default')))).toBeNull()
    } finally {
      setItem.mockRestore()
    }
  })
})
