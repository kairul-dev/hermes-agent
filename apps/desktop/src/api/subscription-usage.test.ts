import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/store/gateway', () => ({ requestGatewayForAgent: vi.fn() }))

import { requestGatewayForAgent } from '@/store/gateway'

import {
  FETCH_BLOCKED_MESSAGE,
  FETCH_FAILED_MESSAGE,
  fetchSubscriptionUsage,
  PARSE_JSON_MESSAGE,
  PARSE_PROVIDERS_MESSAGE,
  PARSE_TIMESTAMP_MESSAGE,
  PARSE_VERSION_MESSAGE,
  parseSubscriptionUsage,
  SubscriptionUsageError,
  SubscriptionUsageParseError
} from './subscription-usage'

// The `usage --all --json` wire contract. Field names are copied EXACTLY from
// the backend schema; nothing is renamed between the CLI and the UI types.
const FULL_PAYLOAD = {
  schema_version: 1,
  updated_at: '2026-10-05T02:00:00.000Z',
  providers: [
    {
      id: 'anthropic',
      label: 'Claude',
      state: 'available',
      kind: 'subscription',
      detail: 'Pro plan, renews Oct 12',
      windows: [
        { label: 'Session', remaining_percent: 80, reset_at: '2026-10-05T06:00:00.000Z' },
        { label: 'Weekly', remaining_percent: 42, reset_at: null }
      ],
      plan: 'Pro'
    },
    {
      id: 'openrouter',
      label: 'OpenRouter',
      state: 'available',
      kind: 'balance',
      detail: '',
      windows: [],
      balance: { amount: '12.50', currency: 'USD' }
    },
    {
      id: 'local',
      label: 'Local models',
      state: 'available',
      kind: 'local',
      detail: '',
      windows: []
    }
  ]
}

const requestMock = vi.mocked(requestGatewayForAgent)

const parse = (value: unknown) => parseSubscriptionUsage(JSON.stringify(value))

describe('parseSubscriptionUsage', () => {
  it('parses a full payload, keeping the contract field names', () => {
    expect(parseSubscriptionUsage(JSON.stringify(FULL_PAYLOAD))).toEqual(FULL_PAYLOAD)
  })

  it('copies only the known provider fields, dropping extras', () => {
    const parsed = parse({
      schema_version: 1,
      updated_at: '2026-10-05T02:00:00.000Z',
      providers: [
        {
          id: 'p1',
          label: 'One',
          state: 'available',
          kind: 'subscription',
          detail: '',
          windows: [],
          api_key: 'sk-should-never-reach-the-app',
          internal_notes: { secret: true }
        }
      ]
    })

    expect(parsed.providers[0]).toEqual({
      id: 'p1',
      label: 'One',
      state: 'available',
      kind: 'subscription',
      detail: '',
      windows: []
    })
  })

  it('rejects invalid JSON with a fixed message that never echoes the raw output', () => {
    const raw = 'usage failed for key sk-live-deadbeef: not json'

    expect(() => parseSubscriptionUsage(raw)).toThrow(SubscriptionUsageParseError)
    expect(() => parseSubscriptionUsage(raw)).toThrow(PARSE_JSON_MESSAGE)

    try {
      parseSubscriptionUsage(raw)
    } catch (error) {
      expect((error as Error).message).not.toContain('sk-live-deadbeef')
      expect((error as Error).message).not.toContain('not json')
    }
  })

  it('rejects an unsupported or missing schema version', () => {
    expect(() => parse({ ...FULL_PAYLOAD, schema_version: 2 })).toThrow(PARSE_VERSION_MESSAGE)
    expect(() => {
      const { schema_version: _dropped, ...rest } = FULL_PAYLOAD

      return parse(rest)
    }).toThrow(PARSE_VERSION_MESSAGE)
  })

  it('rejects a missing or unparseable updated_at', () => {
    expect(() => parse({ ...FULL_PAYLOAD, updated_at: undefined })).toThrow(PARSE_TIMESTAMP_MESSAGE)
    expect(() => parse({ ...FULL_PAYLOAD, updated_at: 'not-a-date' })).toThrow(PARSE_TIMESTAMP_MESSAGE)
  })

  it('normalizes malformed reset timestamps to null, preserving valid quotas', () => {
    const payload = structuredClone(FULL_PAYLOAD)
    payload.providers[0].windows[0].reset_at = 'not-a-date'
    const parsed = parse(payload)

    expect(parsed.providers[0].windows[0].reset_at).toBeNull()
    expect(parsed.providers[0].windows[0].remaining_percent).toBe(80)
  })

  it('rejects a payload whose providers are not an array', () => {
    expect(() => parse({ ...FULL_PAYLOAD, providers: {} })).toThrow(PARSE_PROVIDERS_MESSAGE)
  })

  it('keeps only windows with finite in-range percentages', () => {
    // The overflow percent must ride in as RAW JSON text: JSON.parse turns
    // `1e999` into Infinity, which the parser then drops. A numeric literal
    // would lose precision before the parser ever saw the wire value.
    const windowsJson = [
      '{"label":"Overflow","remaining_percent":1e999,"reset_at":null}',
      '{"label":"TooHigh","remaining_percent":150,"reset_at":null}',
      '{"label":"Negative","remaining_percent":-5,"reset_at":null}',
      '{"label":"Stringy","remaining_percent":"80","reset_at":null}',
      '{"label":"Empty","remaining_percent":0,"reset_at":null}',
      '{"label":"Full","remaining_percent":100,"reset_at":null}',
      '{"label":"Weekly","remaining_percent":42,"reset_at":"2026-10-12T00:00:00.000Z"}'
    ].join(',')

    const parsed = parseSubscriptionUsage(
      `{"schema_version":1,"updated_at":"${FULL_PAYLOAD.updated_at}","providers":[{"id":"anthropic","label":"Claude","state":"available","kind":"subscription","detail":"","windows":[${windowsJson}]}]}`
    )

    expect(parsed.providers[0].windows).toEqual([
      { label: 'Empty', remaining_percent: 0, reset_at: null },
      { label: 'Full', remaining_percent: 100, reset_at: null },
      { label: 'Weekly', remaining_percent: 42, reset_at: '2026-10-12T00:00:00.000Z' }
    ])
  })

  it('nulls a non-string reset_at and drops a window without a usable label', () => {
    const parsed = parse({
      ...FULL_PAYLOAD,
      providers: [
        {
          ...FULL_PAYLOAD.providers[0],
          windows: [
            { label: 'Weekly', remaining_percent: 42, reset_at: 1735689600 },
            { label: '', remaining_percent: 50, reset_at: null },
            { remaining_percent: 50, reset_at: null }
          ]
        }
      ]
    })

    expect(parsed.providers[0].windows).toEqual([{ label: 'Weekly', remaining_percent: 42, reset_at: null }])
  })

  it('drops provider entries that are not objects or have no id', () => {
    const parsed = parse({
      ...FULL_PAYLOAD,
      providers: ['nope', null, { label: 'No id' }, { id: '', label: 'Blank id' }, FULL_PAYLOAD.providers[2]]
    })

    expect(parsed.providers.map(provider => provider.id)).toEqual(['local'])
  })

  it('labels a provider without a label by its id and normalizes its detail', () => {
    const parsed = parse({
      ...FULL_PAYLOAD,
      providers: [{ id: 'p1', state: 'available', kind: 'subscription', detail: 42, windows: 'nope' }]
    })

    expect(parsed.providers[0]).toEqual({
      id: 'p1',
      label: 'p1',
      state: 'available',
      kind: 'subscription',
      detail: '',
      windows: []
    })
  })

  it('maps unknown states to error and unknown kinds to subscription', () => {
    const parsed = parse({
      ...FULL_PAYLOAD,
      providers: [
        { ...FULL_PAYLOAD.providers[0], id: 'a', state: 'weird' },
        { ...FULL_PAYLOAD.providers[0], id: 'b', kind: 'weird' }
      ]
    })

    expect(parsed.providers[0].state).toBe('error')
    expect(parsed.providers[1].kind).toBe('subscription')
  })

  it('keeps only balances that are a decimal string with a currency', () => {
    const balanceOf = (balance: unknown) =>
      parse({
        ...FULL_PAYLOAD,
        providers: [{ ...FULL_PAYLOAD.providers[1], balance }]
      }).providers[0].balance

    expect(balanceOf({ amount: '12.50', currency: 'USD' })).toEqual({ amount: '12.50', currency: 'USD' })
    expect(balanceOf({ amount: '-3', currency: 'EUR' })).toEqual({ amount: '-3', currency: 'EUR' })
    expect(balanceOf({ amount: 12.5, currency: 'USD' })).toBeUndefined()
    expect(balanceOf({ amount: 'NaN', currency: 'USD' })).toBeUndefined()
    expect(balanceOf({ amount: '12.50', currency: '' })).toBeUndefined()
    expect(balanceOf('nope')).toBeUndefined()
  })

  it('accepts an empty provider list', () => {
    expect(parse({ schema_version: 1, updated_at: FULL_PAYLOAD.updated_at, providers: [] })).toEqual({
      schema_version: 1,
      updated_at: FULL_PAYLOAD.updated_at,
      providers: []
    })
  })
})

describe('fetchSubscriptionUsage', () => {
  beforeEach(() => {
    requestMock.mockReset()
  })

  it('runs usage --all --json through the captured connection and profile', async () => {
    requestMock.mockResolvedValue({ blocked: false, code: 0, output: JSON.stringify(FULL_PAYLOAD) })

    await expect(fetchSubscriptionUsage('conn-1', 'beta')).resolves.toEqual(FULL_PAYLOAD)
    expect(requestMock).toHaveBeenCalledTimes(1)
    expect(requestMock).toHaveBeenCalledWith(
      'conn-1',
      'beta',
      'cli.exec',
      { argv: ['usage', '--all', '--json'], timeout: 25 },
      30_000
    )
  })

  it('treats a blocked command as unavailable without echoing its output', async () => {
    requestMock.mockResolvedValue({ blocked: true, code: 0, output: 'blocked: token sk-live-secret' })

    await expect(fetchSubscriptionUsage(null, 'default')).rejects.toThrow(SubscriptionUsageError)
    await expect(fetchSubscriptionUsage(null, 'default')).rejects.toThrow(FETCH_BLOCKED_MESSAGE)
  })

  it('treats a non-zero exit as a failure without echoing its output', async () => {
    requestMock.mockResolvedValue({ blocked: false, code: 1, output: 'err token=abc123' })

    await expect(fetchSubscriptionUsage(null, 'default')).rejects.toThrow(FETCH_FAILED_MESSAGE)
  })

  it('sanitizes transport failures', async () => {
    requestMock.mockRejectedValue(new Error('gateway exploded with sk-live-xyz'))

    try {
      await fetchSubscriptionUsage(null, 'default')
      throw new Error('expected fetchSubscriptionUsage to reject')
    } catch (error) {
      expect(error).toBeInstanceOf(SubscriptionUsageError)
      expect((error as Error).message).toBe(FETCH_FAILED_MESSAGE)
      expect((error as Error).message).not.toContain('sk-live-xyz')
    }
  })

  it('sanitizes malformed output', async () => {
    requestMock.mockResolvedValue({ blocked: false, code: 0, output: 'not-json sk-live-qq' })

    try {
      await fetchSubscriptionUsage(null, 'default')
      throw new Error('expected fetchSubscriptionUsage to reject')
    } catch (error) {
      expect(error).toBeInstanceOf(SubscriptionUsageParseError)
      expect((error as Error).message).toBe(PARSE_JSON_MESSAGE)
      expect((error as Error).message).not.toContain('sk-live-qq')
    }
  })
})
