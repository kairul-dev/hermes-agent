import { describe, expect, it } from 'vitest'

import {
  ACCOUNT_USAGE_CRITICAL_PERCENT,
  formatCountdown,
  minutesSince,
  resetCountdown,
  usageFraction,
  usageIsCritical
} from './account-usage'
import { DAY, HOUR, MINUTE } from './time'

const NOW = Date.parse('2026-10-09T12:00:00Z')
const at = (offsetMs: number) => new Date(NOW + offsetMs).toISOString()

describe('resetCountdown', () => {
  it('uses the two most significant units for each magnitude', () => {
    expect(resetCountdown(at(3 * DAY + 4 * HOUR + 9 * MINUTE), NOW)).toEqual([
      { unit: 'day', value: 3 },
      { unit: 'hour', value: 4 }
    ])

    expect(resetCountdown(at(2 * HOUR + 14 * MINUTE), NOW)).toEqual([
      { unit: 'hour', value: 2 },
      { unit: 'minute', value: 14 }
    ])

    expect(resetCountdown(at(45 * MINUTE), NOW)).toEqual([{ unit: 'minute', value: 45 }])
  })

  it('drops a zero minor unit instead of printing "2h 0m"', () => {
    expect(resetCountdown(at(2 * HOUR), NOW)).toEqual([{ unit: 'hour', value: 2 }])
    expect(resetCountdown(at(2 * DAY), NOW)).toEqual([{ unit: 'day', value: 2 }])
  })

  it('never reports a live window as 0m', () => {
    expect(resetCountdown(at(5_000), NOW)).toEqual([{ unit: 'minute', value: 1 }])
  })

  it('is null when the reset is unknown, unparseable, or already past', () => {
    expect(resetCountdown(null, NOW)).toBeNull()
    expect(resetCountdown(undefined, NOW)).toBeNull()
    expect(resetCountdown('not a date', NOW)).toBeNull()
    expect(resetCountdown(at(0), NOW)).toBeNull()
    expect(resetCountdown(at(-HOUR), NOW)).toBeNull()
  })
})

describe('formatCountdown', () => {
  it('renders every part, in order, with no empty output for a live window', () => {
    const text = formatCountdown(resetCountdown(at(2 * HOUR + 14 * MINUTE), NOW)!)

    expect(text).toMatch(/^2\D*h\D*\s14\D*m/i)
    expect(text.trim()).not.toBe('')
  })
})

describe('usage meters', () => {
  it('clamps the bar fill to 0-1 and survives bad numbers', () => {
    expect(usageFraction({ used_percent: 38 })).toBeCloseTo(0.38)
    expect(usageFraction({ used_percent: 140 })).toBe(1)
    expect(usageFraction({ used_percent: -5 })).toBe(0)
    expect(usageFraction({ used_percent: Number.NaN })).toBe(0)
  })

  it('goes critical exactly at the threshold, not below it', () => {
    expect(usageIsCritical({ used_percent: ACCOUNT_USAGE_CRITICAL_PERCENT - 0.1 })).toBe(false)
    expect(usageIsCritical({ used_percent: ACCOUNT_USAGE_CRITICAL_PERCENT })).toBe(true)
  })
})

describe('minutesSince', () => {
  it('floors to whole minutes and treats a skewed future stamp as "just now"', () => {
    expect(minutesSince(NOW - 90_000, NOW)).toBe(1)
    expect(minutesSince(NOW - 20_000, NOW)).toBe(0)
    expect(minutesSince(NOW + 10 * MINUTE, NOW)).toBe(0)
  })
})
