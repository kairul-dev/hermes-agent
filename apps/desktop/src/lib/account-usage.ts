import type { AccountUsageWindow } from '@/types/hermes'

import { DAY, HOUR, MINUTE } from './time'

/** At or above this a window is about to cut the user off — the bar turns to
 *  the destructive color instead of the provider's. */
export const ACCOUNT_USAGE_CRITICAL_PERCENT = 90

/** A refresh younger than this is not worth another round trip (the backend
 *  caches for the same span). */
export const ACCOUNT_USAGE_FRESH_MS = MINUTE

export function usageIsCritical(window: Pick<AccountUsageWindow, 'used_percent'>): boolean {
  return window.used_percent >= ACCOUNT_USAGE_CRITICAL_PERCENT
}

/** 0–1 fill for `Progress`; never NaN. */
export function usageFraction(window: Pick<AccountUsageWindow, 'used_percent'>): number {
  const pct = Number(window.used_percent)

  return Number.isFinite(pct) ? Math.min(1, Math.max(0, pct / 100)) : 0
}

export type CountdownUnit = 'day' | 'hour' | 'minute'

/** The two most significant non-zero units, coarsest first. */
export type ResetCountdown = Array<{ unit: CountdownUnit; value: number }>

/** Time until `resetAt` as days+hours, hours+minutes, or minutes — or null when
 *  the reset is unknown or already past (the caller decides how to say "now").
 *  A sub-minute remainder reads as 1m so a live window never shows "0m". */
export function resetCountdown(resetAt: null | string | undefined, nowMs = Date.now()): null | ResetCountdown {
  const target = resetAt ? Date.parse(resetAt) : Number.NaN

  if (!Number.isFinite(target) || target <= nowMs) {
    return null
  }

  const remaining = Math.max(MINUTE, target - nowMs)
  const days = Math.floor(remaining / DAY)
  const hours = Math.floor((remaining % DAY) / HOUR)
  const minutes = Math.floor((remaining % HOUR) / MINUTE)

  const pairs: ResetCountdown =
    days > 0
      ? [
          { unit: 'day', value: days },
          { unit: 'hour', value: hours }
        ]
      : hours > 0
        ? [
            { unit: 'hour', value: hours },
            { unit: 'minute', value: minutes }
          ]
        : [{ unit: 'minute', value: minutes }]

  // "2h 0m" reads as "2h".
  return pairs.filter(part => part.value > 0)
}

const UNIT_FORMAT: Record<CountdownUnit, Intl.NumberFormat> = {
  day: new Intl.NumberFormat(undefined, { style: 'unit', unit: 'day', unitDisplay: 'narrow' }),
  hour: new Intl.NumberFormat(undefined, { style: 'unit', unit: 'hour', unitDisplay: 'narrow' }),
  minute: new Intl.NumberFormat(undefined, { style: 'unit', unit: 'minute', unitDisplay: 'narrow' })
}

/** "2h 14m" / "3d 4h" / "45m", localized by Intl. */
export function formatCountdown(countdown: ResetCountdown): string {
  return countdown.map(({ unit, value }) => UNIT_FORMAT[unit].format(value)).join(' ')
}

/** Whole minutes since `sinceMs`, floored at 0 (a skewed clock never reads as
 *  the future). 0 means "just now". */
export function minutesSince(sinceMs: number, nowMs = Date.now()): number {
  return Math.max(0, Math.floor((nowMs - sinceMs) / MINUTE))
}
