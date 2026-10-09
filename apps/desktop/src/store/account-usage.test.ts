import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountUsageProvider, AccountUsageResponse } from '@/types/hermes'

import {
  $accountUsage,
  $accountUsageFailed,
  $accountUsageLoading,
  refreshAccountUsage,
  resetAccountUsage
} from './account-usage'

const provider = (id: string, used = 10): AccountUsageProvider => ({
  id,
  label: id,
  stale: false,
  windows: [{ kind: 'five_hour', reset_at: null, used_percent: used }]
})

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(res => {
    resolve = res
  })

  return { promise, resolve }
}

beforeEach(resetAccountUsage)

describe('refreshAccountUsage', () => {
  it('stores the providers under the scope that asked, stamped with client time', async () => {
    const request = vi.fn().mockResolvedValue({ providers: [provider('anthropic')] } satisfies AccountUsageResponse)
    const before = Date.now()

    await refreshAccountUsage(request, { profile: 'default', scope: 'local|default' })

    const state = $accountUsage.get()

    expect(state?.scope).toBe('local|default')
    expect(state?.providers.map(p => p.id)).toEqual(['anthropic'])
    expect(state!.receivedAt).toBeGreaterThanOrEqual(before)
    expect($accountUsageLoading.get()).toBe(false)
    expect($accountUsageFailed.get()).toBe(false)
  })

  it('asks the backend to bypass its cache only when forced', async () => {
    const request = vi.fn().mockResolvedValue({ providers: [] })

    await refreshAccountUsage(request, { profile: 'default', scope: 's' })
    await refreshAccountUsage(request, { force: true, profile: 'default', scope: 's' })

    expect(request).toHaveBeenNthCalledWith(1, 'account.usage', { profile: 'default' })
    expect(request).toHaveBeenNthCalledWith(2, 'account.usage', { profile: 'default', refresh: true })
  })

  it('names the profile it is asking about, so a shared socket cannot answer for another one', async () => {
    const request = vi.fn().mockResolvedValue({ providers: [] })

    await refreshAccountUsage(request, { profile: 'ops', scope: 'remote|ops' })
    await refreshAccountUsage(request, { force: true, profile: 'ops', scope: 'remote|ops' })

    for (const [, params] of request.mock.calls) {
      expect(params).toMatchObject({ profile: 'ops' })
    }
  })

  it('coalesces concurrent unforced refreshes for one scope into one request', async () => {
    const gate = deferred<AccountUsageResponse>()
    const request = vi.fn().mockReturnValue(gate.promise)

    const a = refreshAccountUsage(request, { profile: 'default', scope: 's' })
    const b = refreshAccountUsage(request, { profile: 'default', scope: 's' })

    gate.resolve({ providers: [provider('anthropic')] })
    await Promise.all([a, b])

    expect(request).toHaveBeenCalledTimes(1)
  })

  it("drops a late reply from a previous scope so one profile never shows another's account", async () => {
    const slow = deferred<AccountUsageResponse>()
    const first = refreshAccountUsage(vi.fn().mockReturnValue(slow.promise), { profile: 'work', scope: 'local|work' })

    await refreshAccountUsage(vi.fn().mockResolvedValue({ providers: [provider('openai-codex')] }), {
      profile: 'personal',
      scope: 'local|personal'
    })

    slow.resolve({ providers: [provider('anthropic')] })
    await first

    expect($accountUsage.get()?.scope).toBe('local|personal')
    expect($accountUsage.get()?.providers.map(p => p.id)).toEqual(['openai-codex'])
  })

  it('keeps the last good data and flags failure when a refresh errors', async () => {
    await refreshAccountUsage(vi.fn().mockResolvedValue({ providers: [provider('anthropic', 62)] }), {
      profile: 'default',
      scope: 's'
    })
    await refreshAccountUsage(vi.fn().mockRejectedValue(new Error('connection closed')), {
      force: true,
      profile: 'default',
      scope: 's'
    })

    expect($accountUsage.get()?.providers[0].windows[0].used_percent).toBe(62)
    expect($accountUsageFailed.get()).toBe(true)
    expect($accountUsageLoading.get()).toBe(false)

    await refreshAccountUsage(vi.fn().mockResolvedValue({ providers: [provider('anthropic', 70)] }), {
      force: true,
      profile: 'default',
      scope: 's'
    })

    expect($accountUsageFailed.get()).toBe(false)
  })

  it('treats a backend without the RPC as "nothing to show", not as a failure', async () => {
    await refreshAccountUsage(vi.fn().mockRejectedValue(new Error('method not found')), {
      profile: 'default',
      scope: 's'
    })

    expect($accountUsage.get()?.providers).toEqual([])
    expect($accountUsageFailed.get()).toBe(false)
  })

  it('tolerates a malformed payload', async () => {
    await refreshAccountUsage(vi.fn().mockResolvedValue({}), { profile: 'default', scope: 's' })

    expect($accountUsage.get()?.providers).toEqual([])
  })
})
