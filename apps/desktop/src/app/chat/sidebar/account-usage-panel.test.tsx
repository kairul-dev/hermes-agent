import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { DAY, HOUR, MINUTE } from '@/lib/time'
import type { AccountUsageState } from '@/store/account-usage'
import type { AccountUsageProvider } from '@/types/hermes'

import { AccountUsageView } from './account-usage-panel'

vi.mock('@/i18n', () => ({ useI18n: () => ({ t: en }) }))

// The connected panel pulls in the gateway hook; the view under test is pure.
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({ useGatewayRequest: () => ({}) }))

afterEach(cleanup)

const NOW = Date.parse('2026-10-09T12:00:00Z')
const at = (offsetMs: number) => new Date(NOW + offsetMs).toISOString()
const copy = en.sidebar.accountUsage

const codex: AccountUsageProvider = {
  id: 'openai-codex',
  label: 'Codex',
  stale: false,
  windows: [
    { kind: 'five_hour', reset_at: at(2 * HOUR + 14 * MINUTE), used_percent: 38 },
    { kind: 'weekly', reset_at: at(3 * DAY), used_percent: 54 }
  ]
}

const claude: AccountUsageProvider = {
  id: 'anthropic',
  label: 'Claude',
  stale: false,
  windows: [
    { kind: 'five_hour', reset_at: at(HOUR + 42 * MINUTE), used_percent: 62 },
    { kind: 'weekly', reset_at: at(5 * DAY), used_percent: 29 }
  ]
}

const state = (providers: AccountUsageProvider[], receivedAt = NOW): AccountUsageState => ({
  providers,
  receivedAt,
  scope: 'local|default'
})

type ViewProps = React.ComponentProps<typeof AccountUsageView>

function view(overrides: Partial<ViewProps> = {}) {
  const props: ViewProps = {
    failed: false,
    loading: false,
    now: NOW,
    onRefresh: vi.fn(),
    onToggle: vi.fn(),
    open: true,
    state: state([codex, claude]),
    ...overrides
  }

  return { ...render(<AccountUsageView {...props} />), props }
}

// A provider's block, found through its visible name so the test reads like the UI.
const providerBlock = (name: string) => screen.getByText(name).closest('[data-provider]') as HTMLElement

describe('AccountUsageView', () => {
  it('shows each provider with its 5-hour and weekly usage', () => {
    view()

    const c = within(providerBlock('Codex'))
    const a = within(providerBlock('Claude'))

    expect(c.getByText('Codex')).toBeTruthy()
    expect(c.getByText('38% used')).toBeTruthy()
    expect(c.getByText('54% used')).toBeTruthy()
    expect(a.getByText('Claude')).toBeTruthy()
    expect(a.getByText('62% used')).toBeTruthy()
    expect(a.getByText('29% used')).toBeTruthy()

    // Meters are real progressbars carrying their own value.
    const bars = screen.getAllByRole('progressbar')

    expect(bars.map(bar => bar.getAttribute('aria-valuenow'))).toEqual(['38', '54', '62', '29'])
    expect(screen.getByRole('progressbar', { name: 'Codex 5-hour' })).toBeTruthy()
  })

  it('prints the live 5-hour reset countdown per provider', () => {
    view()

    expect(within(providerBlock('Codex')).getByText(/^Resets in 2\D*h\D*\s14/)).toBeTruthy()
    expect(within(providerBlock('Claude')).getByText(/^Resets in 1\D*h\D*\s42/)).toBeTruthy()
  })

  it('says "Resets now" for a window whose reset has already passed', () => {
    const lapsed: AccountUsageProvider = {
      ...codex,
      windows: [{ kind: 'five_hour', reset_at: at(-MINUTE), used_percent: 99 }]
    }

    view({ state: state([lapsed]) })

    expect(screen.getByText(copy.resetsNow)).toBeTruthy()
  })

  it('omits the reset line when the provider gives no reset time', () => {
    const unknown: AccountUsageProvider = {
      ...codex,
      windows: [{ kind: 'five_hour', reset_at: null, used_percent: 10 }]
    }

    view({ state: state([unknown]) })

    expect(screen.queryByText(/^Resets/)).toBeNull()
  })

  it('renders nothing until some provider reports limits', () => {
    const { container, rerender, props } = view({ state: null })

    expect(container.firstChild).toBeNull()

    rerender(<AccountUsageView {...props} state={state([])} />)

    expect(container.firstChild).toBeNull()
  })

  it('reports freshness from the client receipt time', () => {
    const { rerender, props } = view()

    expect(screen.getByText(copy.updatedJustNow)).toBeTruthy()

    rerender(<AccountUsageView {...props} now={NOW + 3 * MINUTE} />)

    expect(screen.queryByText(copy.updatedJustNow)).toBeNull()
    expect(screen.getByText(/^Updated /)).toBeTruthy()
  })

  it('warns honestly when showing last-known numbers (refresh failed or provider stale)', () => {
    const { unmount } = view({ failed: true })

    expect(screen.getByText(copy.stale)).toBeTruthy()
    // The old numbers stay visible rather than blanking.
    expect(screen.getByText('38% used')).toBeTruthy()

    unmount()
    view({ state: state([{ ...claude, stale: true }]) })

    expect(screen.getByText(copy.stale)).toBeTruthy()
  })

  it('marks a window about to run out distinctly from a healthy one', () => {
    const hot: AccountUsageProvider = {
      ...claude,
      windows: [
        { kind: 'five_hour', reset_at: null, used_percent: 96 },
        { kind: 'weekly', reset_at: null, used_percent: 29 }
      ]
    }

    view({ state: state([hot]) })

    const [five, weekly] = screen.getAllByRole('progressbar')
    const fill = (bar: HTMLElement) => bar.firstElementChild as HTMLElement

    expect(fill(five).className).toContain('bg-destructive')
    expect(fill(weekly).className).not.toContain('bg-destructive')
  })

  it('refresh button asks to refresh and is disabled while one is in flight', () => {
    const { props, rerender } = view()

    fireEvent.click(screen.getByRole('button', { name: copy.refresh }))
    expect(props.onRefresh).toHaveBeenCalledTimes(1)

    rerender(<AccountUsageView {...props} loading />)

    expect((screen.getByRole('button', { name: copy.refresh }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('collapses to just the header, and the toggle reflects and requests the change', () => {
    const { props, rerender } = view()

    const toggle = screen.getByRole('button', { name: copy.hide })

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(toggle)
    expect(props.onToggle).toHaveBeenCalledTimes(1)

    rerender(<AccountUsageView {...props} open={false} />)

    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(screen.queryByRole('button', { name: copy.refresh })).toBeNull()
    expect(screen.getByRole('button', { name: copy.show }).getAttribute('aria-expanded')).toBe('false')
  })
})
