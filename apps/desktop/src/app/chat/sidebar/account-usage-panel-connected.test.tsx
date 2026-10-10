import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetAccountUsage } from '@/store/account-usage'
import { $activeGatewayProfile } from '@/store/profile'
import { setGatewayState } from '@/store/session'
import type { AccountUsageResponse } from '@/types/hermes'

import { AccountUsagePanel } from './account-usage-panel'

const { requestGateway } = vi.hoisted(() => ({ requestGateway: vi.fn() }))

vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({ useGatewayRequest: () => ({ requestGateway }) }))

const payload: AccountUsageResponse = {
  providers: [
    {
      id: 'anthropic',
      label: 'Claude',
      stale: false,
      windows: [{ kind: 'five_hour', reset_at: null, used_percent: 62 }]
    }
  ]
}

beforeEach(() => {
  requestGateway.mockReset()
  requestGateway.mockResolvedValue(payload)
  resetAccountUsage()
  $activeGatewayProfile.set('default')
  setGatewayState('open')
})

afterEach(() => {
  cleanup()
  setGatewayState('idle')
  resetAccountUsage()
})

describe('AccountUsagePanel (connected)', () => {
  it('fetches once on mount for the active profile, and does not re-ask when its own data lands', async () => {
    render(<AccountUsagePanel />)

    await waitFor(() => expect(screen.getByText('62% used')).toBeTruthy())

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway).toHaveBeenCalledWith('account.usage', { profile: 'default' })
  })

  it('does nothing until the gateway is open', async () => {
    setGatewayState('connecting')
    render(<AccountUsagePanel />)

    await act(async () => undefined)

    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('repaints after the store is wiped without the scope moving (a gateway switch that failed after its wipe)', async () => {
    render(<AccountUsagePanel />)
    await waitFor(() => expect(screen.getByText('62% used')).toBeTruthy())
    expect(requestGateway).toHaveBeenCalledTimes(1)

    // What wipeSessionListsForGatewaySwitch() does to this store. Connection,
    // profile and gateway state are all unchanged, so nothing else would re-run.
    act(() => resetAccountUsage())

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByText('62% used')).toBeTruthy())
  })

  it('does not hot-loop when the refetch itself fails', async () => {
    render(<AccountUsagePanel />)
    await waitFor(() => expect(screen.getByText('62% used')).toBeTruthy())

    requestGateway.mockRejectedValue(new Error('connection closed'))
    act(() => resetAccountUsage())

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(2))
    await act(async () => new Promise(resolve => setTimeout(resolve, 50)))

    expect(requestGateway).toHaveBeenCalledTimes(2)
  })

  it('asks again, as the new profile, when the active profile changes', async () => {
    render(<AccountUsagePanel />)
    await waitFor(() => expect(screen.getByText('62% used')).toBeTruthy())

    act(() => $activeGatewayProfile.set('ops'))

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(2))
    expect(requestGateway).toHaveBeenLastCalledWith('account.usage', { profile: 'ops' })
  })
})
