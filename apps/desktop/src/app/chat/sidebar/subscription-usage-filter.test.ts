import { beforeEach, describe, expect, it, vi } from 'vitest'

import { storageWriteTarget } from '@/test/storage-spy'

import {
  filterUsageProviders,
  isUsageProviderSelected,
  parseUsageProviderSelection,
  persistUsageProviderSelection,
  readUsageProviderSelection,
  toggleUsageProviderSelection,
  usageProviderFilterKey,
  usageScopeKey
} from './subscription-usage-filter'

beforeEach(() => {
  window.localStorage.clear()
})

describe('usage provider selection persistence', () => {
  describe('parseUsageProviderSelection', () => {
    it('distinguishes absence (all) from an explicit empty whitelist (none)', () => {
      expect(parseUsageProviderSelection(null)).toBeNull()
      expect(parseUsageProviderSelection('null')).toBeNull()
      expect(parseUsageProviderSelection('[]')).toEqual([])
    })

    it('keeps string ids and drops anything else from a stored whitelist', () => {
      expect(parseUsageProviderSelection('["claude","openrouter"]')).toEqual(['claude', 'openrouter'])
      expect(parseUsageProviderSelection('["claude",7,"","openrouter",null]')).toEqual(['claude', 'openrouter'])
    })

    it('falls back to showing all providers for corrupt or non-whitelist storage', () => {
      expect(parseUsageProviderSelection('{not json')).toBeNull()
      expect(parseUsageProviderSelection('"claude"')).toBeNull()
      expect(parseUsageProviderSelection('{"claude":true}')).toBeNull()
      expect(parseUsageProviderSelection('42')).toBeNull()
    })
  })

  describe('scope identity', () => {
    it('keys the preference by connection AND profile so gateways never share it', () => {
      const localBeta = usageScopeKey(null, 'beta')
      const remoteBeta = usageScopeKey('conn-1', 'beta')
      const localDefault = usageScopeKey(null, 'default')

      expect(new Set([localBeta, remoteBeta, localDefault]).size).toBe(3)
      expect(usageProviderFilterKey(localBeta)).not.toBe(usageProviderFilterKey(remoteBeta))
    })

    it('reads a whitelist only back for the scope it was stored for', () => {
      window.localStorage.setItem(usageProviderFilterKey(usageScopeKey('conn-1', 'beta')), '["claude"]')

      expect(readUsageProviderSelection(usageScopeKey('conn-1', 'beta'))).toEqual(['claude'])
      expect(readUsageProviderSelection(usageScopeKey(null, 'beta'))).toBeNull()
      expect(readUsageProviderSelection(usageScopeKey('conn-1', 'default'))).toBeNull()
    })

    it('reads corrupt stored data as all instead of crashing', () => {
      window.localStorage.setItem(usageProviderFilterKey(usageScopeKey(null, 'default')), '{oops')

      expect(readUsageProviderSelection(usageScopeKey(null, 'default'))).toBeNull()
    })
  })

  describe('persistUsageProviderSelection', () => {
    const scope = usageScopeKey(null, 'default')

    it('stores all, none, and explicit whitelists distinguishably', () => {
      expect(persistUsageProviderSelection(scope, ['claude'])).toBe(true)
      expect(window.localStorage.getItem(usageProviderFilterKey(scope))).toBe('["claude"]')
      expect(readUsageProviderSelection(scope)).toEqual(['claude'])

      expect(persistUsageProviderSelection(scope, [])).toBe(true)
      expect(window.localStorage.getItem(usageProviderFilterKey(scope))).toBe('[]')
      expect(readUsageProviderSelection(scope)).toEqual([])

      expect(persistUsageProviderSelection(scope, null)).toBe(true)
      expect(window.localStorage.getItem(usageProviderFilterKey(scope))).toBe('null')
      expect(readUsageProviderSelection(scope)).toBeNull()
    })

    it('reports not-persisted when storage rejects the write instead of claiming success', () => {
      // Where setItem lives depends on the runtime (jsdom: Storage.prototype; Node 26 shim: own property).
      const setItem = vi.spyOn(storageWriteTarget(), 'setItem').mockImplementation(() => {
        throw new Error('QuotaExceededError')
      })

      try {
        expect(persistUsageProviderSelection(scope, ['claude'])).toBe(false)
      } finally {
        setItem.mockRestore()
      }
    })

    it('reports not-persisted when the write is dropped silently and never reads back', () => {
      const setItem = vi.spyOn(storageWriteTarget(), 'setItem').mockImplementation(() => {})

      try {
        expect(persistUsageProviderSelection(scope, ['claude'])).toBe(false)
      } finally {
        setItem.mockRestore()
      }
    })
  })

  describe('selection math', () => {
    const providers = [{ id: 'a' }, { id: 'b' }, { id: 'c' }]

    it('treats all as selected and keeps an explicit whitelist exact', () => {
      expect(isUsageProviderSelected(null, 'claude')).toBe(true)
      expect(isUsageProviderSelected([], 'claude')).toBe(false)
      expect(isUsageProviderSelected(['claude'], 'claude')).toBe(true)
      expect(isUsageProviderSelected(['claude'], 'openrouter')).toBe(false)
    })

    it('filters rows by the whitelist, keeping every row visible for all', () => {
      expect(filterUsageProviders(providers, null)).toEqual(providers)
      expect(filterUsageProviders(providers, [])).toEqual([])
      expect(filterUsageProviders(providers, ['b', 'not-loaded'])).toEqual([{ id: 'b' }])
    })

    it('unchecking a provider from all materializes the current set minus that provider', () => {
      expect(toggleUsageProviderSelection(null, ['a', 'b', 'c'], 'b', false)).toEqual(['a', 'c'])
    })

    it('checking a provider adds it without duplicating what is already there', () => {
      expect(toggleUsageProviderSelection([], ['a', 'b'], 'b', true)).toEqual(['b'])
      expect(toggleUsageProviderSelection(['b'], ['a', 'b'], 'a', true)).toEqual(['b', 'a'])
      expect(toggleUsageProviderSelection(['a', 'b'], ['a', 'b'], 'b', true)).toEqual(['a', 'b'])
    })

    it('keeps ids that are not in the current payload when another provider is toggled', () => {
      expect(toggleUsageProviderSelection(['gone', 'a'], ['a', 'b'], 'a', false)).toEqual(['gone'])
      expect(toggleUsageProviderSelection(null, ['a', 'b'], 'b', true)).toEqual(['a', 'b'])
    })

    it('hides providers discovered later while an explicit whitelist is active', () => {
      const withoutB = toggleUsageProviderSelection(null, ['a', 'b'], 'b', false)

      expect(withoutB).toEqual(['a'])
      expect(isUsageProviderSelected(withoutB, 'newcomer')).toBe(false)
      expect(isUsageProviderSelected(null, 'newcomer')).toBe(true)
    })
  })
})
