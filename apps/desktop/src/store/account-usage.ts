import { atom } from 'nanostores'

import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import type { AccountUsageProvider, AccountUsageResponse } from '@/types/hermes'

/**
 * Subscription-limit usage (Codex / Claude 5-hour + weekly windows) for the
 * sidebar panel. The backend is authoritative and caches for a minute; this is
 * the renderer's cache of it.
 *
 * Held keyed by the scope it describes (connection + profile): the accounts
 * differ per profile, so a switch must never paint the previous profile's
 * numbers under the new one. A response is committed only if it is still the
 * newest request for the CURRENT scope — an older or other-scope reply that
 * lands late is dropped.
 */
export interface AccountUsageState {
  providers: AccountUsageProvider[]
  /** Client clock at receipt — "updated" age is measured here, never against
   *  the backend's clock (remote / cloud backends can be skewed). */
  receivedAt: number
  scope: string
}

type GatewayRequest = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

export const $accountUsage = atom<AccountUsageState | null>(null)
export const $accountUsageLoading = atom(false)
/** The last refresh failed (transport / backend error). Data, if any, is kept. */
export const $accountUsageFailed = atom(false)

let generation = 0
let inFlight: { promise: Promise<void>; scope: string } | null = null

export function resetAccountUsage(): void {
  generation += 1
  inFlight = null
  $accountUsage.set(null)
  $accountUsageLoading.set(false)
  $accountUsageFailed.set(false)
}

interface RefreshOptions {
  /** Bypass the backend's cache (the manual refresh button). */
  force?: boolean
  /** Whose account to read. Profiles share one socket under an app-global
   *  remote connection and the backend picks the profile from this param, so
   *  omitting it would answer with the launch profile's account. */
  profile: string
  scope: string
}

export function refreshAccountUsage(
  request: GatewayRequest,
  { force = false, profile, scope }: RefreshOptions
): Promise<void> {
  // Single flight per scope: focus + interval + mount can all ask at once. A
  // forced refresh is a deliberate user intent and always goes out.
  if (!force && inFlight?.scope === scope) {
    return inFlight.promise
  }

  const mine = ++generation
  const current = () => mine === generation

  $accountUsageLoading.set(true)

  const promise = request<AccountUsageResponse>('account.usage', force ? { profile, refresh: true } : { profile })
    .then(result => {
      if (!current()) {
        return
      }

      $accountUsage.set({
        providers: Array.isArray(result?.providers) ? result.providers : [],
        receivedAt: Date.now(),
        scope
      })
      $accountUsageFailed.set(false)
    })
    .catch(error => {
      if (!current()) {
        return
      }

      // A backend that predates the RPC simply has no panel — not a failure.
      if (isMissingRpcMethod(error)) {
        $accountUsage.set({ providers: [], receivedAt: Date.now(), scope })
        $accountUsageFailed.set(false)

        return
      }

      $accountUsageFailed.set(true)
    })
    .finally(() => {
      if (current()) {
        $accountUsageLoading.set(false)
        inFlight = null
      }
    })

  inFlight = { promise, scope }

  return promise
}
