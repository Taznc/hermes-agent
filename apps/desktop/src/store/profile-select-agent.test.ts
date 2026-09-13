import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'

// selectAgent — the registry-aware sibling of selectProfile, and the IN-SOURCE
// door the fleet rail uses to pick another profile on the connection it is
// already enumerating.
//
// The invariant that earns this file: "am I switching?" must be judged on the
// (connection, profile) PAIR. The same profile name commonly exists on several
// registered sources, so comparing profile keys alone silently treats
// local `default` → remote `default` as a no-op — the user picks their remote
// box, nothing re-homes, and the window stays on the local backend. That is the
// exact confusion #85731 reports from the rail.
//
// Cross-source switching is NOT this door: every such switch goes through
// store/connections selectConnection's two-phase commit (see
// app/command-palette/agent-row-switch.test.ts). selectAgent stays for the
// same-machine profile pick, where that commit's machine-context reset would
// close terminals and wipe session lists the user is still looking at.

// A truthy resolution means "activation landed" — resolving false models a
// disposed target, which must publish nothing.
const ensureGatewayForAgent = vi.fn(async (_connectionId: null | string, _profile: string) => true)
const ensureGatewayForProfile = vi.fn(async (_profile: string) => undefined)
const openGatewayForProfile = vi.fn(async (_profile: string) => undefined)
// The registry's own "which source is live" answer (store/gateway). Since
// #92194, selectProfile routes a null-connection pick through
// activateOnCurrentSource, which asks the REGISTRY — not the
// $activeGatewayConnection atom. The stubbed ensure* doubles never move this
// registry, so it stays on the primary (null) unless a test steers it with
// mockReturnValue — same harness shape as profile-select-source.test.ts,
// which owns the registry-routing cases.
const activeGatewayConnectionId = vi.fn<() => null | string>(() => null)
const $gateway = atom<unknown>({ id: 'live-socket' })
const resetStarmapGraph = vi.fn()
const wipeSessionListsForGatewaySwitch = vi.fn()

vi.mock('@/store/gateway', () => ({
  $gateway,
  activeGatewayConnectionId,
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
vi.mock('@/store/gateway-switch', () => ({ wipeSessionListsForGatewaySwitch }))

const {
  $activeGatewayConnection,
  $activeGatewayProfile,
  $freshSessionRequest,
  $newChatProfile,
  $showAllProfiles,
  selectAgent
} = await import('./profile')

const { $connection } = await import('./session')

const remoteConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: 'https://homelab.invalid', mode: 'remote', profile: 'default', ...over }) as HermesConnection

const localConn = (over: Partial<HermesConnection> = {}): HermesConnection =>
  ({ baseUrl: '', mode: 'local', profile: 'default', ...over }) as HermesConnection

const getConnection = vi.fn<(profile?: string | null) => Promise<HermesConnection>>()

const getConnectionFor =
  vi.fn<(payload: { connectionId?: null | string; profile?: null | string }) => Promise<HermesConnection>>()

/** Let the fire-and-forget ensureGatewayAgent chain inside selectAgent settle. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  getConnection.mockReset()
  getConnectionFor.mockReset()
  getConnection.mockResolvedValue(localConn())
  getConnectionFor.mockResolvedValue(remoteConn())
  // Cleared per-test: call counts leak across cases and
  // "not.toHaveBeenCalled()" would see the PREVIOUS test's dial.
  ensureGatewayForAgent.mockClear()
  ensureGatewayForProfile.mockClear()
  activeGatewayConnectionId.mockReset()
  activeGatewayConnectionId.mockReturnValue(null)
  $gateway.set({ id: 'live-socket' })
  $activeGatewayProfile.set('default')
  $activeGatewayConnection.set(null)
  $newChatProfile.set(null)
  $showAllProfiles.set(false)
  $connection.set(localConn())
  vi.stubGlobal('window', { hermesDesktop: { getConnection, getConnectionFor } })
})

afterEach(() => {
  vi.unstubAllGlobals()
  $connection.set(null)
})

describe('selectAgent switches on the (connection, profile) pair', () => {
  it('re-homes onto a remote source even when the profile NAME is unchanged', async () => {
    // Sitting on local `default`; picking `default` on a registered remote box
    // is a real backend change that a name-only comparison would swallow.
    const before = $freshSessionRequest.get()

    selectAgent('hermes-dev', 'default')
    await settle()

    expect(ensureGatewayForAgent).toHaveBeenCalledWith('hermes-dev', 'default')
    expect($activeGatewayProfile.get()).toBe('default')
    expect($activeGatewayConnection.get()).toBe('hermes-dev')
    // A fresh draft was requested: this is a context switch, not a no-op.
    expect($freshSessionRequest.get()).toBeGreaterThan(before)
  })

  it('does not request a fresh session when re-selecting the agent already active', async () => {
    $activeGatewayProfile.set('default')
    $activeGatewayConnection.set('hermes-dev')

    const before = $freshSessionRequest.get()

    selectAgent('hermes-dev', 'default')
    await settle()

    expect($freshSessionRequest.get()).toBe(before)
  })

  it('tracks the active connection so a later switch is seen as a change', async () => {
    selectAgent('hermes-dev', 'default')
    await settle()
    expect($activeGatewayConnection.get()).toBe('hermes-dev')

    // Onto the registry's own local entry: still an agent dial, and the
    // published connection must follow, or the next selection would compare
    // against a stale id.
    selectAgent('local', 'default')
    await settle()

    expect(ensureGatewayForAgent).toHaveBeenLastCalledWith('local', 'default')
    expect($activeGatewayConnection.get()).toBe('local')
  })

  it('leaves the all-profiles browse view and points new chats at the target', async () => {
    $showAllProfiles.set(true)

    selectAgent('hermes-dev', 'research')
    await settle()

    expect($showAllProfiles.get()).toBe(false)
    expect($newChatProfile.get()).toBe('research')
  })

  it('never falls through to the local-pool profile door', async () => {
    // The pair IS the identity. Falling back to ensureGatewayProfile here would
    // re-home the window to this device's same-named profile — the #92194
    // class — so an agent pick must always dial its own connection.
    selectAgent('hermes-dev', 'research')
    await settle()

    expect(ensureGatewayForAgent).toHaveBeenCalledWith('hermes-dev', 'research')
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
  })

  it('ignores a whitespace-only connectionId rather than dialing anything', async () => {
    // No connection means no (connection, profile) pair, so there is nothing
    // this door can identify. It must not silently retarget the local pool:
    // that is an authoritative write to a source the caller never named.
    selectAgent('   ', 'research')
    await settle()

    expect(ensureGatewayForAgent).not.toHaveBeenCalled()
    expect(ensureGatewayForProfile).not.toHaveBeenCalled()
    expect($activeGatewayProfile.get()).toBe('default')
  })
})