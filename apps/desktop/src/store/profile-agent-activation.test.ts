import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

import { deferred } from '../test/deferred'

// Registry-agent activation (ensureGatewayAgent — the SDK ensureAgent door).
// Two regressions pinned here:
//  1. Activating an ALREADY-OPEN registry agent must still resync
//     $connection (via getConnectionFor) and move $activeGatewayProfile —
//     previously only a freshly-dialed socket synced $connection (inside
//     openSecondary), so re-activating an open agent left REST/fs/media and
//     image-attach routing on the previous backend (same class as #46651).
//  2. Agent activations share the gatewaySwitch mutex with profile switches —
//     without it, two rapid activations could complete out of order and the
//     EARLIER setActive() landed last.

const ensureGatewayForAgent = vi.fn(async (_connectionId: null | string, _profile: string) => true)
const ensureGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()

vi.mock('@/store/gateway', () => ({
  $gateway,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
  openGatewayForProfile
}))
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  setApiRequestProfile: vi.fn()
}))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph }))

const { $activeGatewayConnection, $activeGatewayProfile, ensureGatewayAgent, ensureGatewayProfile, ensureGatewaySessionProfile } =
  await import('./profile')
const { $connection } = await import('./session')

const agentConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: 'https://homelab.invalid', mode: 'remote', profile: 'research', ...over }) as HermesConnection

const localConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

const getConnection = vi.fn<(profile?: string | null) => Promise<HermesConnection>>()

const getConnectionFor =
  vi.fn<(payload: { connectionId?: null | string; profile?: null | string }) => Promise<HermesConnection>>()

beforeEach(() => {
  getConnection.mockReset()
  getConnectionFor.mockReset()
  ensureGatewayForAgent.mockClear()
  ensureGatewayForProfile.mockClear()
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  $connection.set(localConn())
  vi.stubGlobal('window', { hermesDesktop: { getConnection, getConnectionFor } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
})

describe('ensureGatewayAgent → $connection / $activeGatewayProfile sync', () => {
  it('resyncs $connection and $activeGatewayProfile even when the agent socket is already open', async () => {
    // The store-level activation resolves instantly (socket already open) —
    // exactly the case that used to skip the sync entirely.
    getConnectionFor.mockResolvedValue(agentConn())

    await ensureGatewayAgent('homelab', 'research')

    expect(ensureGatewayForAgent).toHaveBeenCalledWith('homelab', 'research')
    expect(getConnectionFor).toHaveBeenCalledWith({ connectionId: 'homelab', profile: 'research' })
    expect($activeGatewayProfile.get()).toBe('research')
    expect($connection.get()?.mode).toBe('remote')
    expect($connection.get()?.profile).toBe('research')
  })

  it('leaves the prior connection intact when the descriptor fetch fails', async () => {
    getConnectionFor.mockRejectedValue(new Error('source unreachable'))

    await ensureGatewayAgent('homelab', 'research')

    expect($activeGatewayProfile.get()).toBe('research')
    // Best-effort: boot/reconnect resyncs later; we must not null it out here.
    expect($connection.get()?.mode).toBe('local')
  })

  it('does not republish a registry identity invalidated during activation', async () => {
    ensureGatewayForAgent.mockResolvedValueOnce(false)

    await ensureGatewayAgent('removed-source', 'research')

    expect($activeGatewayProfile.get()).toBe('default')
    expect($connection.get()?.mode).toBe('local')
    // The descriptor lookup DOES run: it is issued concurrently with the dial
    // so nothing awaits between the activation verdict and the publication
    // frame. The invariant that matters — nothing is PUBLISHED for a dead
    // target — is asserted above; the lookup itself is a read-only probe.
    expect(getConnectionFor).toHaveBeenCalledTimes(1)
  })

  it('falls through to the profile path for a null connectionId', async () => {
    getConnection.mockResolvedValue(agentConn({ mode: 'local', profile: 'research' }))

    await ensureGatewayAgent(null, 'research')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('research')
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
    expect(getConnectionFor).not.toHaveBeenCalled()
  })

  it('keeps an explicit local registry id on the registry-aware path', async () => {
    getConnectionFor.mockResolvedValue(localConn({ profile: 'research' }))

    await ensureGatewayAgent('local', 'research')

    expect(ensureGatewayForAgent).toHaveBeenCalledWith('local', 'research')
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect(getConnectionFor).toHaveBeenCalledWith({ connectionId: 'local', profile: 'research' })
  })
})

describe('ensureGatewayAgent shares the gatewaySwitch mutex with profile switches', () => {
  it('serializes an agent activation behind an in-flight profile switch', async () => {
    const profileGate = deferred()
    const order: string[] = []

    ensureGatewayForProfile.mockImplementation(async (profile: string) => {
      order.push(`profile:${profile}`)
      await profileGate.promise
    })
    ensureGatewayForAgent.mockImplementation(async (_connectionId, profile) => {
      order.push(`agent:${profile}`)

      return true
    })
    getConnection.mockResolvedValue(localConn({ profile: 'worker' }))
    getConnectionFor.mockResolvedValue(agentConn())

    // Start a profile switch that stalls mid-flight, then an agent
    // activation. The agent activation must NOT start until the profile
    // switch settles — otherwise the earlier setActive could land last.
    const profileSwitch = ensureGatewayProfile('worker')
    await Promise.resolve()
    const agentSwitch = ensureGatewayAgent('homelab', 'research')
    await Promise.resolve()

    expect(order).toEqual(['profile:worker'])

    profileGate.resolve()
    await profileSwitch
    await agentSwitch

    expect(order).toEqual(['profile:worker', 'agent:research'])
    // The LAST activation wins the active pointer.
    expect($activeGatewayProfile.get()).toBe('research')
    expect($connection.get()?.profile).toBe('research')
  })

  it('serializes a profile switch behind an in-flight agent activation', async () => {
    const agentGate = deferred()
    const order: string[] = []

    ensureGatewayForAgent.mockImplementation(async (_connectionId, profile) => {
      order.push(`agent:${profile}`)
      await agentGate.promise

      return true
    })
    ensureGatewayForProfile.mockImplementation(async (profile: string) => {
      order.push(`profile:${profile}`)
    })
    getConnection.mockResolvedValue(localConn({ profile: 'worker' }))
    getConnectionFor.mockResolvedValue(agentConn())

    const agentSwitch = ensureGatewayAgent('homelab', 'research')
    await Promise.resolve()
    const profileSwitch = ensureGatewayProfile('worker')
    await Promise.resolve()

    expect(order).toEqual(['agent:research'])

    agentGate.resolve()
    await agentSwitch
    await profileSwitch

    expect(order).toEqual(['agent:research', 'profile:worker'])
    expect($activeGatewayProfile.get()).toBe('worker')
  })
})

describe('ensureGatewaySessionProfile (source-preserving session-create swap)', () => {
  // The silent-local-fallback-on-Send class: a send/fork/branch resolves only a
  // profile NAME, and routing that name through the local-pool door while a
  // registry agent is active re-homed the whole window to this device.
  it('stays on the active registry connection when the profile matches', async () => {
    getConnectionFor.mockResolvedValue(agentConn())
    await ensureGatewayAgent('hermes-dev', 'claudecode')
    expect($activeGatewayConnection.get()).toBe('hermes-dev')
    ensureGatewayForAgent.mockClear()
    ensureGatewayForProfile.mockClear()

    await ensureGatewaySessionProfile('claudecode')

    // Re-dials the AGENT door — never the local-pool profile door.
    expect(ensureGatewayForAgent).toHaveBeenCalledWith('hermes-dev', 'claudecode')
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($activeGatewayConnection.get()).toBe('hermes-dev')
    expect($activeGatewayProfile.get()).toBe('claudecode')
  })

  it('treats a null/empty profile as the active one and stays on the connection', async () => {
    getConnectionFor.mockResolvedValue(agentConn())
    await ensureGatewayAgent('hermes-dev', 'claudecode')
    ensureGatewayForAgent.mockClear()
    ensureGatewayForProfile.mockClear()

    await ensureGatewaySessionProfile(null)
    await ensureGatewaySessionProfile('   ')

    expect(ensureGatewayForAgent).toHaveBeenCalledTimes(2)
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($activeGatewayConnection.get()).toBe('hermes-dev')
  })

  it('keeps the legacy local-pool route when no registry connection is active', async () => {
    getConnection.mockResolvedValue(localConn({ profile: 'research' }))
    // The shared beforeEach resets the profile atom but not the connection one;
    // put this test on the local pool explicitly.
    $activeGatewayConnection.set(null)

    await ensureGatewaySessionProfile('research')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('research')
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
  })

  it('routes a DIFFERENT profile through the profile door (explicit cross-machine move)', async () => {
    getConnectionFor.mockResolvedValue(agentConn())
    await ensureGatewayAgent('hermes-dev', 'claudecode')
    ensureGatewayForAgent.mockClear()
    getConnection.mockResolvedValue(localConn({ profile: 'other' }))

    await ensureGatewaySessionProfile('other')

    expect(ensureGatewayForProfile).toHaveBeenCalledWith('other')
    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
  })
})
