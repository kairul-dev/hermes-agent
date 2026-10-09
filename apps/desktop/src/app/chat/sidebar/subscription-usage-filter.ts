import { readKey, writeKey } from '@/lib/storage'

// ── Subscription-usage provider visibility preference ────────────────────────
// Display-only whitelist for the usage card's provider rows, keyed by the
// same connection+profile scope the card already fetches under. `null` means
// "all providers show" (so providers discovered later stay visible); `[]`
// means "none"; otherwise only the listed provider ids show. Persisted as a
// JSON string, parsed defensively: corrupt or unavailable storage always
// reads back as "all" — never a crash, never silently hiding data.

export const USAGE_PROVIDER_FILTER_PREFIX = 'hermes.desktop.subscriptionUsage.providerFilter.v1'

export type UsageProviderSelection = null | string[]

/** The same composite identity the usage card scopes its fetch to: a profile
 *  belongs to ONE gateway, and the same name on two connections is two
 *  scopes, so preferences can never leak across gateways. */
export function usageScopeKey(connectionId: null | string, profile: string): string {
  return JSON.stringify([connectionId ?? null, profile])
}

export function usageProviderFilterKey(scopeKey: string): string {
  return `${USAGE_PROVIDER_FILTER_PREFIX}:${scopeKey}`
}

/** Safe decode of the raw stored string. Absence, `null`, malformed JSON, or
 *  any non-array shape all read as "all"; arrays keep only usable string ids. */
export function parseUsageProviderSelection(raw: null | string): UsageProviderSelection {
  if (raw === null) {
    return null
  }

  try {
    const parsed: unknown = JSON.parse(raw)

    if (parsed === null) {
      return null
    }

    return Array.isArray(parsed)
      ? parsed.filter((id): id is string => typeof id === 'string' && id.length > 0)
      : null
  } catch {
    return null
  }
}

export function readUsageProviderSelection(scopeKey: string): UsageProviderSelection {
  return parseUsageProviderSelection(readKey(usageProviderFilterKey(scopeKey)))
}

/** Persist the whitelist and report whether it actually landed. The stored
 *  value is read back byte-for-byte: a write swallowed by unavailable storage
 *  (private modes, disabled storage, quota) reads back as null and reports
 *  false, so the caller can keep the choice for this session only and say so
 *  instead of pretending it saved. */
export function persistUsageProviderSelection(scopeKey: string, selection: UsageProviderSelection): boolean {
  const key = usageProviderFilterKey(scopeKey)
  const encoded = JSON.stringify(selection)

  writeKey(key, encoded)

  return readKey(key) === encoded
}

/** True when the provider is visible under this selection. `null` selects
 *  everything — including providers discovered after the choice was made. */
export function isUsageProviderSelected(selection: UsageProviderSelection, id: string): boolean {
  return selection === null || selection.includes(id)
}

/** The rows the card should display for this selection. All mode passes the
 *  list through untouched so newly discovered providers stay visible. */
export function filterUsageProviders<T extends { id: string }>(
  providers: T[],
  selection: UsageProviderSelection
): T[] {
  return selection === null ? providers : providers.filter(provider => selection.includes(provider.id))
}

/** Next whitelist when one provider's checkbox is toggled. Unchecking from
 *  "all" materializes the current payload's ids minus the unchecked one, so
 *  later-discovered providers are hidden until explicitly chosen. */
export function toggleUsageProviderSelection(
  selection: UsageProviderSelection,
  ids: string[],
  id: string,
  selected: boolean
): UsageProviderSelection {
  const current = selection === null ? ids : selection

  if (selected) {
    return current.includes(id) ? current : [...current, id]
  }

  return current.filter(existing => existing !== id)
}
