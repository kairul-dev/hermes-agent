import assert from 'node:assert/strict'

import { test } from 'vitest'

import { attachOrReserveSpawn, attachToHostBackend, type HostSpawnGateDeps } from './host-backend-attach'

// `desktop.shared_backend_url` names one managed owner (a service on a fixed loopback port that
// deliberately runs its own checkout). With it set, Desktop attaches ONLY to that owner, after an
// authenticated PID/role proof, and never spawns a private backend beside it.

const OWNER_PORT = 65_238
const OWNER_URL = `http://127.0.0.1:${OWNER_PORT}`

const record = (port: number, pid: number) => ({
  argv: `hermes serve --host 127.0.0.1 --port ${port}`,
  create_time: 1_000,
  host: '127.0.0.1',
  install: 'abc',
  pid,
  port,
  profile: 'default',
  purpose: 'serve',
  registered_at: 2_000
})

const LEDGER = JSON.stringify([record(OWNER_PORT, 4711), record(50_000, 4712)])

function deps(overrides: Record<string, unknown> = {}) {
  return {
    log: () => {},
    probeWebSocket: async () => ({ ok: true }),
    readLedger: () => LEDGER,
    resolveServedToken: async () => 'served-token',
    waitForReady: async () => undefined,
    ...overrides
  }
}

/** Fake clock + gate: no real sleeping, and a spy on whether a spawn reservation was ever taken. */
function fakeGate() {
  let now = 0
  let takes = 0

  const gate: HostSpawnGateDeps = {
    now: () => now,
    read: () => null,
    sleep: async ms => {
      now += ms
    },
    take: () => {
      takes += 1

      return () => {}
    }
  }

  return { gate, takes: () => takes }
}

test('a required owner is attached after its identity proof, even when it runs a different commit', async () => {
  const proofs: number[] = []

  const attached = await attachToHostBackend(
    { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL },
    deps({
      // The checkout this Desktop runs from differs from the managed owner's commit: normally a refusal.
      backendCodeIdentity: async () => 'owner-commit',
      expectedCodeIdentity: async () => 'desktop-checkout-commit',
      verifyIdentity: async (rec: { pid: number }) => {
        proofs.push(rec.pid)

        return true
      }
    })
  )

  assert.equal(attached?.baseUrl, OWNER_URL)
  assert.equal(attached?.pid, 4711)
  assert.deepEqual(proofs, [4711], 'only the named owner is asked to prove its identity')
})

test('a required owner that cannot prove PID/role is not attached', async () => {
  const attached = await attachToHostBackend(
    { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL },
    deps({ verifyIdentity: async () => false })
  )

  assert.equal(attached, null)
})

test('a required owner whose proof throws is not attached', async () => {
  const attached = await attachToHostBackend(
    { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL },
    deps({
      verifyIdentity: async () => {
        throw new Error('connection refused')
      }
    })
  )

  assert.equal(attached, null)
})

test('records on other ports are ignored while an owner is required', async () => {
  const onlyOther = JSON.stringify([record(50_000, 4712)])

  const attached = await attachToHostBackend(
    { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL },
    deps({ readLedger: () => onlyOther, verifyIdentity: async () => true })
  )

  assert.equal(attached, null)
})

test('without a required owner a commit mismatch is still refused (upstream behavior unchanged)', async () => {
  const attached = await attachToHostBackend(
    { isolated: false, ledgerPath: '/ledger.json' },
    deps({
      backendCodeIdentity: async () => 'owner-commit',
      expectedCodeIdentity: async () => 'desktop-checkout-commit'
    })
  )

  assert.equal(attached, null)
})

test('a missing required owner never yields a spawn reservation; the wait ends with a clear error', async () => {
  const { gate, takes } = fakeGate()

  await assert.rejects(
    attachOrReserveSpawn(
      { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL },
      deps({ readLedger: () => null, verifyIdentity: async () => true }),
      gate,
      { pollMs: 500, waitBudgetMs: 5_000 }
    ),
    /shared Hermes backend on http:\/\/127\.0\.0\.1:65238 is not ready/
  )
  assert.equal(takes(), 0, 'the spawn gate must never be taken while an owner is required')
})

test('a required owner that appears during the wait is attached', async () => {
  const { gate, takes } = fakeGate()
  let reads = 0

  const outcome = await attachOrReserveSpawn(
    { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL },
    deps({
      readLedger: () => (++reads < 4 ? null : LEDGER),
      verifyIdentity: async () => true
    }),
    gate,
    { pollMs: 500, waitBudgetMs: 60_000 }
  )

  assert.ok('attached' in outcome)
  assert.equal(outcome.attached.baseUrl, OWNER_URL)
  assert.equal(takes(), 0)
})

test('aborting the wait stops it immediately', async () => {
  const { gate } = fakeGate()
  const controller = new AbortController()
  controller.abort(new Error('app is quitting'))

  await assert.rejects(
    attachOrReserveSpawn(
      { isolated: false, ledgerPath: '/ledger.json', requiredBaseUrl: OWNER_URL, signal: controller.signal },
      deps({ readLedger: () => null, verifyIdentity: async () => true }),
      gate,
      { pollMs: 500, waitBudgetMs: 60_000 }
    ),
    /app is quitting/
  )
})
