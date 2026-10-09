// Typed adapter for the backend's `hermes usage --all --json` contract.
//
// The desktop never inspects raw CLI text: the parser copies the contract's
// field names EXACTLY (schema_version / updated_at / providers / id / label /
// state / kind / detail / windows / remaining_percent / reset_at / balance /
// amount / currency / plan) and normalizes every entry defensively. Error
// messages raised from this module are fixed strings — never raw command
// output or exception text, which can carry credentials.

import { requestGatewayForAgent } from '@/store/gateway'

/** One rate-limit window of a subscription provider. */
export interface UsageWindow {
  label: string
  /** Percent remaining, finite and within 0..100. */
  remaining_percent: number
  /** ISO-8601 reset instant, or null when the provider reports none. */
  reset_at: null | string
}

/** Account balance for a `balance`-kind provider (decimal string, as reported). */
export interface UsageBalance {
  amount: string
  currency: string
}

export type UsageProviderKind = 'balance' | 'local' | 'subscription'
export type UsageProviderState = 'available' | 'error' | 'unavailable'

export interface UsageProvider {
  id: string
  label: string
  state: UsageProviderState
  kind: UsageProviderKind
  detail: string
  windows: UsageWindow[]
  balance?: UsageBalance
  plan?: string
}

export interface SubscriptionUsage {
  schema_version: 1
  updated_at: string
  providers: UsageProvider[]
}

/** Any failure raised while fetching or reading usage data. Messages are
 *  fixed strings — they NEVER embed command output or exception text. */
export class SubscriptionUsageError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'SubscriptionUsageError'
  }
}

/** The CLI produced output that is not a supported usage payload. */
export class SubscriptionUsageParseError extends SubscriptionUsageError {
  constructor(message: string) {
    super(message)
    this.name = 'SubscriptionUsageParseError'
  }
}

export const PARSE_JSON_MESSAGE = 'Usage data was not valid JSON.'
export const PARSE_VERSION_MESSAGE = 'Unsupported usage data version.'
export const PARSE_TIMESTAMP_MESSAGE = 'Usage data had no valid timestamp.'
export const PARSE_PROVIDERS_MESSAGE = 'Usage data had no provider list.'
export const FETCH_FAILED_MESSAGE = 'Could not load subscription usage.'
export const FETCH_BLOCKED_MESSAGE = 'Subscription usage is unavailable for this profile.'

/** `cli.exec` reply shape (gateway-contract.openrpc.json). */
interface CliExecResult {
  blocked?: boolean
  code?: number
  output?: string
}

export const USAGE_CLI_ARGV = ['usage', '--all', '--json'] as const
export const USAGE_CLI_TIMEOUT_S = 25
export const USAGE_REQUEST_TIMEOUT_MS = 30_000

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function normalizeWindow(value: unknown): UsageWindow[] {
  if (!isRecord(value) || typeof value.label !== 'string' || !value.label) {
    return []
  }

  const percent = value.remaining_percent

  if (typeof percent !== 'number' || !Number.isFinite(percent) || percent < 0 || percent > 100) {
    return []
  }

  return [
    {
      label: value.label,
      remaining_percent: percent,
      reset_at:
        typeof value.reset_at === 'string' && Number.isFinite(Date.parse(value.reset_at)) ? value.reset_at : null
    }
  ]
}

const DECIMAL_AMOUNT = /^-?\d+(\.\d+)?$/

function normalizeBalance(value: unknown): undefined | UsageBalance {
  if (!isRecord(value)) {
    return undefined
  }

  const amount = typeof value.amount === 'string' ? value.amount.trim() : ''
  const currency = typeof value.currency === 'string' ? value.currency.trim() : ''

  if (!DECIMAL_AMOUNT.test(amount) || !currency) {
    return undefined
  }

  return { amount, currency }
}

function normalizeProvider(value: unknown): UsageProvider[] {
  if (!isRecord(value)) {
    return []
  }

  const id = typeof value.id === 'string' ? value.id : ''

  if (!id) {
    return []
  }

  const state: UsageProviderState =
    value.state === 'available' || value.state === 'unavailable' || value.state === 'error' ? value.state : 'error'

  const kind: UsageProviderKind = value.kind === 'balance' || value.kind === 'local' ? value.kind : 'subscription'
  const balance = normalizeBalance(value.balance)

  return [
    {
      id,
      label: typeof value.label === 'string' && value.label ? value.label : id,
      state,
      kind,
      detail: typeof value.detail === 'string' ? value.detail : '',
      windows: Array.isArray(value.windows) ? value.windows.flatMap(normalizeWindow) : [],
      ...(balance ? { balance } : {}),
      ...(typeof value.plan === 'string' && value.plan ? { plan: value.plan } : {})
    }
  ]
}

/** Parse the CLI's JSON output into the typed usage payload. Throws a
 *  SubscriptionUsageParseError with a fixed message on anything malformed —
 *  raw snippets are never repeated back. */
export function parseSubscriptionUsage(output: string): SubscriptionUsage {
  let root: unknown

  try {
    root = JSON.parse(output)
  } catch {
    throw new SubscriptionUsageParseError(PARSE_JSON_MESSAGE)
  }

  if (!isRecord(root)) {
    throw new SubscriptionUsageParseError(PARSE_JSON_MESSAGE)
  }

  if (root.schema_version !== 1) {
    throw new SubscriptionUsageParseError(PARSE_VERSION_MESSAGE)
  }

  if (typeof root.updated_at !== 'string' || !Number.isFinite(Date.parse(root.updated_at))) {
    throw new SubscriptionUsageParseError(PARSE_TIMESTAMP_MESSAGE)
  }

  if (!Array.isArray(root.providers)) {
    throw new SubscriptionUsageParseError(PARSE_PROVIDERS_MESSAGE)
  }

  return {
    schema_version: 1,
    updated_at: root.updated_at,
    providers: root.providers.flatMap(normalizeProvider)
  }
}

/** Run `usage --all --json` on the backend that owns (connectionId, profile)
 *  and parse its reply. Every failure path is sanitized to a fixed message. */
export async function fetchSubscriptionUsage(connectionId: null | string, profile: string): Promise<SubscriptionUsage> {
  let result: CliExecResult

  try {
    result = await requestGatewayForAgent<CliExecResult>(
      connectionId,
      profile,
      'cli.exec',
      { argv: [...USAGE_CLI_ARGV], timeout: USAGE_CLI_TIMEOUT_S },
      USAGE_REQUEST_TIMEOUT_MS
    )
  } catch {
    throw new SubscriptionUsageError(FETCH_FAILED_MESSAGE)
  }

  if (!result || result.blocked === true) {
    throw new SubscriptionUsageError(FETCH_BLOCKED_MESSAGE)
  }

  if (result.code !== 0 || typeof result.output !== 'string') {
    throw new SubscriptionUsageError(FETCH_FAILED_MESSAGE)
  }

  return parseSubscriptionUsage(result.output)
}
