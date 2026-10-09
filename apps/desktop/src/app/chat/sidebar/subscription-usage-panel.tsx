import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { $apiRequestScope } from '@/api/client'
import {
  fetchSubscriptionUsage,
  type SubscriptionUsage,
  type UsageProvider,
  type UsageWindow
} from '@/api/subscription-usage'
import { Checkbox } from '@/components/ui/checkbox'
import { Codicon } from '@/components/ui/codicon'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { fmtDayTime, relativeTime } from '@/lib/time'
import { $gatewayState } from '@/store/session'

import {
  filterUsageProviders,
  isUsageProviderSelected,
  persistUsageProviderSelection,
  readUsageProviderSelection,
  toggleUsageProviderSelection,
  type UsageProviderSelection,
  usageScopeKey
} from './subscription-usage-filter'

const WAITING_COPY = 'Waiting for gateway…'
const LOADING_COPY = 'Loading usage…'
const FAILED_COPY = "Couldn't load usage"
const EMPTY_COPY = 'No connected providers'
const REFRESHING_COPY = 'Refreshing…'
const STALE_COPY = "Couldn't refresh — showing earlier data"
const CHOOSE_PROVIDERS_COPY = 'Choose providers'
const SHOW_ALL_COPY = 'Show all'
const CLEAR_SELECTION_COPY = 'Clear selection'
const NONE_SELECTED_COPY = 'No providers selected'
const UNSAVED_SELECTION_COPY = "Couldn't save selection — it applies for this session only"

/** No poll tick fires more often than this while the panel is mounted. */
export const USAGE_POLL_INTERVAL_MS = 5 * 60_000

/** The wrapper's per-scope state: `key` is the scope it belongs to, so a
 *  stale entry can never render under a different scope. */
interface PanelState {
  key: string
  data: SubscriptionUsage | null
  failed: boolean
  loading: boolean
}

/** The provider-whitelist state, keyed the same way: the moment the scope
 *  changes, the new scope's own preference reads — never the old one's. */
interface SelectionState {
  key: string
  persistFailed: boolean
  value: UsageProviderSelection
}

/** Props for the presentational usage card. Everything renders as plain text
 *  (no HTML injection) and the card keeps its header/footer fixed while the
 *  provider list scrolls. Suitable for a screenshot harness with literal data. */
export interface UsagePanelViewProps {
  /** Whole-card collapse state (controlled). */
  collapsed: boolean
  /** The latest SUCCESSFUL payload for the current scope; null before first load. */
  data: SubscriptionUsage | null
  /** The LAST refresh attempt failed; `data` (if any) is stale. */
  failed: boolean
  /** A fetch is in flight (first load or refresh). */
  loading: boolean
  /** Manual refresh — never navigates, focuses, or opens anything else. */
  onRefresh: () => void
  /** Supply to render the provider chooser; omit it for pure literal
   *  renderings so the control never appears without a way to change it. */
  onSelectionChange?: (selection: UsageProviderSelection) => void
  onToggleCollapsed: () => void
  /** Scope profile the payload belongs to (footer attribution). */
  profile: string
  /** Gateway readiness; false shows the waiting state until data arrives. */
  ready: boolean
  /** Display-only provider whitelist: null shows all (including providers
   *  discovered later), empty shows none, otherwise only the listed ids.
   *  Defaults to all so static renderings keep every row visible. */
  selection?: UsageProviderSelection
  /** The last save attempt failed; the chooser says the choice only applies
   *  for this session instead of pretending it was saved. */
  selectionPersistFailed?: boolean
}

function bodyCopy(
  data: SubscriptionUsage | null,
  failed: boolean,
  loading: boolean,
  ready: boolean,
  visibleCount: number
): string {
  if (data) {
    if (visibleCount > 0) {
      return ''
    }

    return data.providers.length === 0 ? EMPTY_COPY : NONE_SELECTED_COPY
  }

  if (!ready) {
    return WAITING_COPY
  }

  return failed && !loading ? FAILED_COPY : LOADING_COPY
}

// The card is a glance, not a dashboard: a subscription shows the smallest
// remaining percentage across its windows (floored, so the glance never
// overstates); a balance shows the reported amount as-is; never a made-up
// ratio for incomplete data. `null` means "no metric to show".
function formatPercent(value: number): string {
  const shown = Number.isInteger(value) ? value : Math.floor(value * 10) / 10

  return `${shown}% left`
}

function providerMetric(provider: UsageProvider): null | string {
  if (provider.kind === 'local') {
    return 'Local'
  }

  if (provider.state !== 'available') {
    return 'Unavailable'
  }

  if (provider.windows.length > 0) {
    return formatPercent(Math.min(...provider.windows.map(window => window.remaining_percent)))
  }

  if (provider.balance) {
    return `${provider.balance.amount} ${provider.balance.currency}`
  }

  return null
}

function windowLine(window: UsageWindow): string {
  const reset = window.reset_at ? `, resets ${fmtDayTime.format(new Date(window.reset_at))}` : ''

  return `${window.label}: ${formatPercent(window.remaining_percent)}${reset}`
}

function providerTitle(provider: UsageProvider): string {
  const parts = [provider.label]
  const metric = providerMetric(provider)

  if (metric) {
    parts.push(metric)
  }

  for (const window of provider.windows) {
    parts.push(windowLine(window))
  }

  if (provider.detail) {
    parts.push(provider.detail)
  }

  return parts.join(' · ')
}

function UsageRow({ provider }: { provider: UsageProvider }) {
  const metric = providerMetric(provider)

  const hasBody =
    Boolean(provider.detail) || Boolean(provider.plan) || provider.windows.length > 0 || Boolean(provider.balance)

  return (
    <details className="group/usage-row rounded-sm">
      <summary
        className="flex min-w-0 cursor-pointer list-none items-center gap-1.5 rounded-sm px-1.5 py-1 hover:bg-(--ui-control-hover-background)"
        title={providerTitle(provider)}
      >
        <DisclosureCaret className="transition-transform group-open/usage-row:rotate-90" open={false} />
        <span className="min-w-0 flex-1 truncate text-(--ui-text-secondary)">{provider.label}</span>
        {metric ? <span className="shrink-0 tabular-nums text-(--ui-text-tertiary)">{metric}</span> : null}
      </summary>
      <div className="grid gap-0.5 pb-1 pl-5 pr-1.5 text-[0.6875rem] leading-4 text-(--ui-text-tertiary)">
        {provider.detail ? <p className="wrap-anywhere">{provider.detail}</p> : null}
        {provider.plan ? <p>Plan: {provider.plan}</p> : null}
        {provider.windows.length > 0 ? (
          <ul className="grid gap-0.5">
            {provider.windows.map(window => (
              <li
                className="truncate"
                key={`${window.label}:${window.remaining_percent}:${window.reset_at ?? ''}`}
                title={windowLine(window)}
              >
                {windowLine(window)}
              </li>
            ))}
          </ul>
        ) : null}
        {provider.balance ? (
          <p>
            Balance: {provider.balance.amount} {provider.balance.currency}
          </p>
        ) : null}
        {hasBody ? null : <p>No usage details</p>}
      </div>
    </details>
  )
}

/** Header chooser for the provider whitelist: one checkbox per provider in
 *  the current payload, plus whole-selection actions. Radix supplies the
 *  popover's keyboard/focus handling; every change reports the next
 *  whitelist upward (null = all, empty = none). */
function ProviderChooser({
  onSelectionChange,
  persistFailed,
  providers,
  selection
}: {
  onSelectionChange: (selection: UsageProviderSelection) => void
  persistFailed: boolean
  providers: UsageProvider[]
  selection: UsageProviderSelection
}) {
  const ids = providers.map(provider => provider.id)

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          aria-label={CHOOSE_PROVIDERS_COPY}
          className="grid size-5 shrink-0 place-items-center rounded-sm text-(--ui-text-tertiary) transition-colors hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary) data-[state=open]:bg-(--ui-control-active-background) data-[state=open]:text-(--ui-text-primary)"
          type="button"
        >
          <Codicon name="checklist" size="0.75rem" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-60 p-1.5">
        {providers.length > 0 ? (
          <div className="grid max-h-56 gap-px overflow-y-auto">
            {providers.map(entry => (
              <label
                className="flex min-w-0 cursor-pointer items-center gap-2 rounded-sm px-1.5 py-1 hover:bg-(--ui-control-hover-background)"
                key={entry.id}
              >
                <Checkbox
                  aria-label={entry.label}
                  checked={isUsageProviderSelected(selection, entry.id)}
                  onCheckedChange={next =>
                    onSelectionChange(toggleUsageProviderSelection(selection, ids, entry.id, next === true))
                  }
                />
                <span className="min-w-0 flex-1 truncate text-[0.6875rem] text-(--ui-text-secondary)">
                  {entry.label}
                </span>
              </label>
            ))}
          </div>
        ) : (
          <p className="px-1.5 py-1 text-[0.6875rem] text-(--ui-text-tertiary)">{EMPTY_COPY}</p>
        )}
        <div className="mt-1 grid gap-px border-t border-(--ui-stroke-quaternary) pt-1">
          <button
            className="flex w-full items-center rounded-sm px-1.5 py-1 text-left text-[0.6875rem] text-(--ui-text-secondary) transition-colors hover:bg-(--ui-control-hover-background)"
            onClick={() => onSelectionChange(null)}
            type="button"
          >
            {SHOW_ALL_COPY}
          </button>
          <button
            className="flex w-full items-center rounded-sm px-1.5 py-1 text-left text-[0.6875rem] text-(--ui-text-secondary) transition-colors hover:bg-(--ui-control-hover-background)"
            onClick={() => onSelectionChange([])}
            type="button"
          >
            {CLEAR_SELECTION_COPY}
          </button>
        </div>
        {persistFailed ? (
          <p className="mt-1 px-1.5 pb-0.5 text-[0.625rem] leading-4 text-(--ui-text-tertiary)">
            {UNSAVED_SELECTION_COPY}
          </p>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}

/** The usage card itself: header (collapse + refresh), scrolling provider
 *  list, and a fixed footer with the data timestamp and scope profile. No
 *  effects — the wrapper supplies state; the screenshot harness can supply
 *  literal data. */
export function UsagePanelView({
  collapsed,
  data,
  failed,
  loading,
  onRefresh,
  onSelectionChange,
  onToggleCollapsed,
  profile,
  ready,
  selection = null,
  selectionPersistFailed = false
}: UsagePanelViewProps) {
  const providers = data?.providers ?? []
  const visibleProviders = filterUsageProviders(providers, selection)
  const copy = bodyCopy(data, failed, loading, ready, visibleProviders.length)
  const showRows = data !== null && visibleProviders.length > 0

  return (
    <section
      aria-label="Subscription usage"
      className="my-1.5 flex max-h-[190px] shrink-0 flex-col overflow-hidden rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-card) text-xs"
      role="region"
    >
      <header className="flex shrink-0 items-center gap-1 px-1.5 py-0.5">
        <button
          aria-expanded={!collapsed}
          aria-label={collapsed ? 'Expand subscription usage' : 'Collapse subscription usage'}
          className="flex min-w-0 flex-1 items-center gap-1 rounded-sm px-1 py-0.5 text-left text-(--ui-text-secondary) transition-colors hover:bg-(--ui-control-hover-background)"
          onClick={onToggleCollapsed}
          type="button"
        >
          <DisclosureCaret open={!collapsed} />
          <span className="min-w-0 truncate text-[0.6875rem] font-medium">Usage</span>
        </button>
        {onSelectionChange ? (
          <ProviderChooser
            onSelectionChange={onSelectionChange}
            persistFailed={selectionPersistFailed}
            providers={providers}
            selection={selection}
          />
        ) : null}
        <button
          aria-busy={loading}
          aria-label="Refresh subscription usage"
          className="grid size-5 shrink-0 place-items-center rounded-sm text-(--ui-text-tertiary) transition-colors hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary) disabled:opacity-50"
          disabled={loading || !ready}
          onClick={onRefresh}
          type="button"
        >
          <Codicon name="refresh" size="0.75rem" spinning={loading} />
        </button>
      </header>

      {!collapsed && (
        <>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-1.5 pb-1">
            {showRows ? (
              <div className="grid gap-px">
                {visibleProviders.map(provider => (
                  <UsageRow key={provider.id} provider={provider} />
                ))}
              </div>
            ) : (
              <p className="px-1 py-1 text-[0.6875rem] text-(--ui-text-tertiary)">{copy}</p>
            )}
          </div>
          <footer className="grid shrink-0 gap-0.5 border-t border-(--ui-stroke-quaternary) px-2 py-0.5 text-[0.625rem] text-(--ui-text-quaternary)">
            {data && loading ? (
              <p aria-live="polite" role="status">
                {REFRESHING_COPY}
              </p>
            ) : null}
            {data && !loading && failed ? <p role="status">{STALE_COPY}</p> : null}
            <div className="flex min-w-0 items-center justify-between gap-2">
              <time className="min-w-0 truncate" data-testid="subscription-usage-updated" dateTime={data?.updated_at}>
                {data ? `Updated ${relativeTime(Date.parse(data.updated_at))}` : 'Updated —'}
              </time>
              <span className="min-w-0 truncate">{profile}</span>
            </div>
          </footer>
        </>
      )}
    </section>
  )
}

export function SubscriptionUsagePanel() {
  const scope = useStore($apiRequestScope)
  const gatewayState = useStore($gatewayState)
  const ready = gatewayState === 'open'
  const connectionId = scope.connectionId ?? null
  const profile = scope.profile || 'default'
  // A composite key: a profile belongs to ONE gateway, so the same name on two
  // connections is two scopes. The display gate below uses it to drop the old
  // scope's quota the moment the scope changes (no cross-profile carryover).
  const scopeKey = usageScopeKey(connectionId, profile)

  const [collapsed, setCollapsed] = useState(false)
  const [panel, setPanel] = useState<PanelState>({ key: scopeKey, data: null, loading: false, failed: false })

  const [selection, setSelection] = useState<SelectionState>(() => ({
    key: scopeKey,
    persistFailed: false,
    value: readUsageProviderSelection(scopeKey)
  }))

  // Bumped whenever the scope session changes (scope/readiness effect re-runs
  // or unmounts). A fetch only lands if its generation is still current, so a
  // late reply from a previous scope can never overwrite the new scope's view.
  const genRef = useRef(0)
  // Generation whose fetch is currently in flight. Refresh clicks and poll
  // ticks for the SAME scope ride it instead of stacking overlapping requests.
  const inFlightRef = useRef<null | number>(null)

  const load = useCallback((key: string, forConnectionId: null | string, forProfile: string) => {
    const gen = genRef.current

    if (inFlightRef.current === gen) {
      return
    }

    inFlightRef.current = gen

    setPanel(previous =>
      previous.key === key ? { ...previous, loading: true } : { key, data: null, loading: true, failed: false }
    )

    void fetchSubscriptionUsage(forConnectionId, forProfile)
      .then(data => {
        if (genRef.current === gen) {
          setPanel({ key, data, loading: false, failed: false })
        }
      })
      .catch(() => {
        if (genRef.current === gen) {
          // A failed refresh never drops the last good quota: the old payload
          // (and its updated_at) stays on screen, marked stale by `failed`.
          setPanel(previous =>
            previous.key === key
              ? { ...previous, loading: false, failed: true }
              : { key, data: null, loading: false, failed: true }
          )
        }
      })
      .finally(() => {
        if (inFlightRef.current === gen) {
          inFlightRef.current = null
        }
      })
  }, [])

  // eslint-disable-next-line no-restricted-syntax -- request-token ref, not an atom mirror (see the rule comment)
  useEffect(() => {
    genRef.current += 1

    if (!ready) {
      return
    }

    load(scopeKey, connectionId, profile)

    return () => {
      // Invalidate this generation on scope change and unmount alike.
      genRef.current += 1
    }
  }, [load, scopeKey, ready, connectionId, profile])

  // Display only the current scope's state: a stale key reads as a fresh,
  // loading session — never as the previous profile's quota.
  const current: PanelState =
    panel.key === scopeKey ? panel : { key: scopeKey, data: null, loading: ready, failed: false }

  // The same gate for the whitelist: a scope switch shows the new scope's
  // stored preference immediately, never the previous gateway's choice.
  const currentSelection: SelectionState =
    selection.key === scopeKey
      ? selection
      : { key: scopeKey, persistFailed: false, value: readUsageProviderSelection(scopeKey) }

  const changeSelection = useCallback(
    (next: UsageProviderSelection) => {
      const persisted = persistUsageProviderSelection(scopeKey, next)

      setSelection({ key: scopeKey, persistFailed: !persisted, value: next })
    },
    [scopeKey]
  )

  const panelRef = useRef(panel)
  panelRef.current = panel

  const latestScopeRef = useRef({ connectionId, profile, scopeKey })
  latestScopeRef.current = { connectionId, profile, scopeKey }

  // Poll cadence: never more than once per USAGE_POLL_INTERVAL_MS while the
  // panel is mounted, skipped entirely while the document is hidden. A
  // visibility resume refreshes only when the shown data is already outdated
  // — and never navigates, focuses, or opens anything.
  useEffect(() => {
    if (!ready) {
      return
    }

    const tick = () => {
      if (document.visibilityState === 'hidden') {
        return
      }

      const latest = latestScopeRef.current

      load(latest.scopeKey, latest.connectionId, latest.profile)
    }

    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        return
      }

      const latest = latestScopeRef.current
      const entry = panelRef.current
      const updatedAt = entry.key === latest.scopeKey ? entry.data?.updated_at : null

      if (!updatedAt || Date.now() - Date.parse(updatedAt) >= USAGE_POLL_INTERVAL_MS) {
        load(latest.scopeKey, latest.connectionId, latest.profile)
      }
    }

    const interval = window.setInterval(tick, USAGE_POLL_INTERVAL_MS)

    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [load, ready])

  return (
    <UsagePanelView
      collapsed={collapsed}
      data={current.data}
      failed={current.failed}
      loading={current.loading}
      onRefresh={() => load(scopeKey, connectionId, profile)}
      onSelectionChange={changeSelection}
      onToggleCollapsed={() => setCollapsed(value => !value)}
      profile={profile}
      ready={ready}
      selection={currentSelection.value}
      selectionPersistFailed={currentSelection.persistFailed}
    />
  )
}
