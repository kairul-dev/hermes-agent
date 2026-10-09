import { useStore } from '@nanostores/react'
import { type CSSProperties, useEffect, useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { Button } from '@/components/ui/button'
import { DisclosureCaret } from '@/components/ui/disclosure-caret'
import { Progress } from '@/components/ui/progress'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import {
  ACCOUNT_USAGE_FRESH_MS,
  formatCountdown,
  minutesSince,
  resetCountdown,
  usageFraction,
  usageIsCritical
} from '@/lib/account-usage'
import { RefreshCw } from '@/lib/icons'
import { MINUTE, relativeTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import {
  $accountUsage,
  $accountUsageFailed,
  $accountUsageLoading,
  type AccountUsageState,
  refreshAccountUsage
} from '@/store/account-usage'
import { $activeConnectionId } from '@/store/connections'
import { $sidebarUsageOpen, setSidebarUsageOpen } from '@/store/layout'
import { $activeGatewayProfile } from '@/store/profile'
import { $gatewayState } from '@/store/session'
import type { AccountUsageProvider, AccountUsageWindow } from '@/types/hermes'

// The backend caches for a minute; the panel only needs to keep a long-lived
// window honest, so a slow backstop is enough. Focus and manual refresh cover
// the moments the user actually looks.
const BACKSTOP_REFRESH_MS = 5 * MINUTE
const CLOCK_TICK_MS = 30_000

// Provider identity hue (tokens live in styles.css). Unknown providers fall
// back to the theme primary rather than rendering colorless.
const PROVIDER_ACCENT: Record<string, string> = {
  anthropic: 'var(--usage-claude)',
  'openai-codex': 'var(--usage-codex)'
}

function useNow(intervalMs: number, active: boolean): number {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!active) {
      return
    }

    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)

    return () => window.clearInterval(id)
  }, [active, intervalMs])

  return now
}

/** Connected panel: fetches, keeps fresh, and renders nothing until the active
 *  profile has at least one provider that reports subscription limits. */
export function AccountUsagePanel() {
  const { requestGateway } = useGatewayRequest()
  const gatewayState = useStore($gatewayState)
  const connectionId = useStore($activeConnectionId)
  const profile = useStore($activeGatewayProfile)
  const state = useStore($accountUsage)
  const loading = useStore($accountUsageLoading)
  const failed = useStore($accountUsageFailed)
  const open = useStore($sidebarUsageOpen)
  const visible = usePaneVisible()
  const now = useNow(CLOCK_TICK_MS, visible)
  const scope = `${connectionId ?? ''}|${profile}`

  useEffect(() => {
    if (gatewayState !== 'open' || !visible) {
      return
    }

    const refresh = () => void refreshAccountUsage(requestGateway, { scope })
    const hidden = () => document.visibilityState === 'hidden'

    refresh()

    const interval = window.setInterval(() => !hidden() && refresh(), BACKSTOP_REFRESH_MS)

    // Coming back to the window is when stale numbers hurt; the age check keeps
    // a flurry of focus events from re-asking inside the backend's cache span.
    const onActive = () => {
      const held = $accountUsage.get()

      if (!hidden() && (!held || held.scope !== scope || Date.now() - held.receivedAt > ACCOUNT_USAGE_FRESH_MS)) {
        refresh()
      }
    }

    window.addEventListener('focus', onActive)
    document.addEventListener('visibilitychange', onActive)

    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', onActive)
      document.removeEventListener('visibilitychange', onActive)
    }
  }, [gatewayState, requestGateway, scope, visible])

  // Held keyed by the scope it describes: never paint the previous profile's
  // account under the one that just became active.
  const current = state?.scope === scope ? state : null

  return (
    <AccountUsageView
      failed={failed}
      loading={loading}
      now={now}
      onRefresh={() => void refreshAccountUsage(requestGateway, { force: true, scope })}
      onToggle={() => setSidebarUsageOpen(!open)}
      open={open}
      state={current}
    />
  )
}

interface AccountUsageViewProps {
  failed: boolean
  loading: boolean
  now: number
  onRefresh: () => void
  onToggle: () => void
  open: boolean
  state: AccountUsageState | null
}

export function AccountUsageView({ failed, loading, now, onRefresh, onToggle, open, state }: AccountUsageViewProps) {
  const { t } = useI18n()
  const copy = t.sidebar.accountUsage

  if (!state?.providers.length) {
    return null
  }

  const stale = failed || state.providers.some(provider => provider.stale)
  const age = minutesSince(state.receivedAt, now)

  return (
    <section
      aria-label={copy.title}
      className="mb-1.5 flex shrink-0 flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2.5 py-2"
      data-slot="account-usage"
    >
      <div className="flex items-center justify-between gap-2">
        <button
          aria-expanded={open}
          aria-label={open ? copy.hide : copy.show}
          className="flex min-w-0 items-center gap-1.5 rounded-sm text-[0.75rem] font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
          onClick={onToggle}
          type="button"
        >
          <DisclosureCaret className="text-(--ui-text-tertiary)" open={open} />
          <span className="min-w-0 truncate">{copy.title}</span>
        </button>

        {open && (
          <Tip label={copy.refresh}>
            <Button aria-label={copy.refresh} disabled={loading} onClick={onRefresh} size="icon-xs" variant="ghost">
              <RefreshCw className={cn(loading && 'animate-spin')} />
            </Button>
          </Tip>
        )}
      </div>

      {open && (
        <>
          {state.providers.map((provider, index) => (
            <ProviderUsage index={index} key={provider.id} now={now} provider={provider} />
          ))}

          <p className={cn('text-[0.625rem]', stale ? 'text-destructive' : 'text-(--ui-text-tertiary)')}>
            {stale ? copy.stale : age < 1 ? copy.updatedJustNow : copy.updatedAgo(relativeTime(state.receivedAt, now))}
          </p>
        </>
      )}
    </section>
  )
}

function ProviderUsage({ index, now, provider }: { index: number; now: number; provider: AccountUsageProvider }) {
  const accent = PROVIDER_ACCENT[provider.id] ?? 'var(--theme-primary)'

  return (
    <div
      className={cn('flex flex-col gap-1.5', index > 0 && 'border-t border-(--ui-stroke-tertiary) pt-2')}
      data-provider={provider.id}
      style={{ '--usage-accent': accent } as CSSProperties}
    >
      <p className="flex items-center gap-1.5 text-[0.75rem] font-medium text-foreground">
        <span aria-hidden="true" className="size-2 shrink-0 rounded-full bg-(--usage-accent)" />
        <span className="min-w-0 truncate">{provider.label}</span>
      </p>

      {provider.windows.map(window => (
        <WindowUsage key={window.kind} now={now} provider={provider} window={window} />
      ))}
    </div>
  )
}

function WindowUsage({
  now,
  provider,
  window
}: {
  now: number
  provider: AccountUsageProvider
  window: AccountUsageWindow
}) {
  const { t } = useI18n()
  const copy = t.sidebar.accountUsage
  const label = window.kind === 'five_hour' ? copy.fiveHour : copy.weekly
  const countdown = resetCountdown(window.reset_at, now)
  const reset = window.reset_at ? (countdown ? copy.resetsIn(formatCountdown(countdown)) : copy.resetsNow) : null
  const critical = usageIsCritical(window)

  const row = (
    <div className="flex flex-col gap-1" data-window={window.kind}>
      <div className="flex items-baseline justify-between gap-2 text-[0.6875rem]">
        <span className="text-(--ui-text-secondary)">{label}</span>

        <span className="tabular-nums text-(--ui-text-tertiary)">
          {copy.percentUsed(Math.round(window.used_percent))}
        </span>
      </div>

      <Progress
        aria-label={`${provider.label} ${label}`}
        className="bg-(--ui-stroke-tertiary)"
        destructive={critical}
        fillClassName={
          critical
            ? undefined
            : window.kind === 'five_hour'
              ? 'bg-(--usage-accent)'
              : // Weekly is the quieter sibling: a paler tint of the provider hue (toward white on dark surfaces, where fading to transparent would only read as muddy).
                'bg-[color-mix(in_srgb,var(--usage-accent)_55%,transparent)] dark:bg-[color-mix(in_srgb,var(--usage-accent)_50%,white)]'
        }
        size="sm"
        value={usageFraction(window)}
      />

      {/* The 5-hour clock is the one people watch, so it is always on screen;
          the weekly reset is days out and only worth a hover. */}
      {window.kind === 'five_hour' && reset && <p className="text-[0.625rem] text-(--ui-text-tertiary)">{reset}</p>}
    </div>
  )

  return window.kind === 'weekly' && reset ? <Tip label={reset}>{row}</Tip> : row
}
