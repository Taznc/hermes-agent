import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopAgentRoster, DesktopConnectionsRegistry } from '@/global'

// The palette's "Agents & connections" rows are a CROSS-SOURCE switching door,
// so they must commit exactly like the other two doors (the sidebar gateway
// selector and the fleet rail's at-rest squares): through selectConnection's
// two-phase commit.
//
// The invariant that earns this file is an ORDERING, not a call count. The row
// used to call ensureGatewayAgent directly, which activates and publishes the
// new source without ever raising the switch barrier — so route and session
// effects observed the new backend while $activeSessionId still named a runtime
// the PREVIOUS backend minted, and the next RPC came back "session not found"
// (#93937). Asserting only "selectConnection was called" would not catch a
// regression that re-ordered the commit; these assert that the wipe lands
// strictly BEFORE the target is published, on the real selectConnection.

const $activeGatewayProfile = atom('default')
const $newChatProfile = atom<null | string>(null)
const $showAllProfiles = atom(false)

const $connection = atom<null | {
  connectionId?: string
  mode?: 'local' | 'remote'
  profile?: string
  registryScoped?: boolean
}>(null)

// The runtime session id minted by the CURRENT backend — the binding a switch
// must sever before the next source is published.
const $activeSessionId = atom<null | string>(null)

/** Ordered trace of the commit steps, so the assertions are about sequence. */
const trace: string[] = []

interface ActivationOptions {
  beforeActivate?: () => boolean
  signal?: AbortSignal
}

const ensureGatewayAgent = vi.fn(
  async (connectionId: null | string, profile: string, options?: ActivationOptions): Promise<void> => {
    if (options?.beforeActivate && !options.beforeActivate()) {
      return
    }

    trace.push(`publish:${connectionId}::${profile}`)
    $connection.set({
      connectionId: connectionId ?? undefined,
      mode: connectionId === 'local' ? 'local' : 'remote',
      profile,
      registryScoped: true
    })
    $activeGatewayProfile.set(profile)
  }
)

const openGatewayAgent = vi.fn(async (connectionId: string, profile: string): Promise<void> => {
  trace.push(`dial:${connectionId}::${profile}`)
})

const wipeSessionListsForGatewaySwitch = vi.fn(() => {
  trace.push('wipe')
  $activeSessionId.set(null)
})

// Test double for the store's commit point with the real one's contract
// (barrier → machine-context reset → wipe, synchronously). The real
// implementation is covered by gateway-switch.test.ts.
let latestSwitchToken = 0

const beginGatewaySwitch = vi.fn(() => {
  trace.push('begin')
  wipeSessionListsForGatewaySwitch()

  return ++latestSwitchToken
})

const endGatewaySwitch = vi.fn()
const recoverActiveSourceAfterFailedGatewaySwitch = vi.fn()
const notifyError = vi.fn()

vi.mock('@/store/session', () => ({ $connection }))
vi.mock('@/store/notifications', () => ({ notifyError }))
vi.mock('@/store/gateway-switch', () => ({
  $gatewaySwitching: atom(false),
  beginGatewaySwitch,
  endGatewaySwitch,
  recoverActiveSourceAfterFailedGatewaySwitch,
  wipeSessionListsForGatewaySwitch
}))
vi.mock('@/store/profile', () => ({
  $activeGatewayProfile,
  $newChatProfile,
  $showAllProfiles,
  captureNewChatSource: vi.fn(),
  ensureGatewayAgent,
  normalizeProfileKey: (name: null | string | undefined) => (name ?? '').trim() || 'default',
  openGatewayAgent,
  refreshActiveProfile: vi.fn(async () => undefined),
  requestFreshSession: vi.fn()
}))

const { $connectionsRegistry, _resetConnectionsForTests, setConnectionsRegistry } = await import('@/store/connections')

const { switchToAgentRow } = await import('./agent-row-switch')
const { buildAgentPaletteRows } = await import('./agent-rows')

const registry: DesktopConnectionsRegistry = {
  connections: [
    { id: 'local', kind: 'local', label: 'This device', tokenPreview: null, tokenSet: false },
    { id: 'hermes-dev', kind: 'ssh', label: 'hermes-dev-env', tokenPreview: null, tokenSet: false }
  ],
  primary: 'local',
  secureTokenStorage: true,
  version: 2
}

const roster: DesktopAgentRoster = {
  agents: [
    {
      connectionId: 'local',
      connectionKind: 'local',
      connectionLabel: 'This device',
      handle: 'default',
      profile: 'default'
    },
    {
      connectionId: 'hermes-dev',
      connectionKind: 'ssh',
      connectionLabel: 'hermes-dev-env',
      handle: '@research-hermes-dev-env',
      profile: 'research'
    }
  ],
  sources: [
    { connectionId: 'local', kind: 'local', label: 'This device', reachable: true },
    { connectionId: 'hermes-dev', kind: 'ssh', label: 'hermes-dev-env', reachable: true }
  ]
} as DesktopAgentRoster

/** The rows the palette actually renders, built by the real pure builder. */
const paletteRows = (activeConnectionId: null | string, activeProfile: string) =>
  buildAgentPaletteRows({
    activeConnectionId,
    activeProfile,
    localLabel: 'This device',
    normalizeProfile: (name: string) => (name || '').trim() || 'default',
    roster
  })

const switchFailed = (device: string) => `Could not connect to ${device}`

/** Let the fire-and-forget selectConnection chain inside the row's run settle. */
const settle = async () => {
  for (let tick = 0; tick < 10; tick += 1) {
    await Promise.resolve()
  }

  await new Promise(resolve => setTimeout(resolve, 0))
}

beforeEach(() => {
  localStorage.clear()
  _resetConnectionsForTests()
  $connectionsRegistry.set(null)
  setConnectionsRegistry(registry)
  trace.length = 0
  latestSwitchToken = 0
  $activeGatewayProfile.set('default')
  $newChatProfile.set(null)
  $showAllProfiles.set(false)
  // Live on this device, with a runtime session this backend minted.
  $connection.set({ connectionId: 'local', mode: 'local', profile: 'default', registryScoped: true })
  $activeSessionId.set('runtime-minted-by-local')
  ensureGatewayAgent.mockClear()
  openGatewayAgent.mockClear()
  beginGatewaySwitch.mockClear()
  wipeSessionListsForGatewaySwitch.mockClear()
  notifyError.mockClear()
})

afterEach(() => {
  $connection.set(null)
  $activeSessionId.set(null)
})

describe('a palette row switches through selectConnection two-phase commit', () => {
  it('wipes the previous backend bindings BEFORE the target is published', async () => {
    const remoteRow = paletteRows('local', 'default').find(row => !row.isLocal)

    expect(remoteRow).toBeDefined()
    switchToAgentRow(remoteRow!, switchFailed)
    await settle()

    // Dial without activating, then commit — barrier + wipe — and only then
    // publish. A direct ensureGatewayAgent call produces ['publish:…'] alone.
    expect(trace).toEqual(['dial:hermes-dev::research', 'begin', 'wipe', 'publish:hermes-dev::research'])
    expect(trace.indexOf('wipe')).toBeLessThan(trace.indexOf('publish:hermes-dev::research'))
  })

  it('severs the previous backend runtime id before the new source can observe it', async () => {
    // The concrete #93937 symptom: the id the OLD backend minted must be gone
    // by the time the new source is published, or the next RPC sends it to a
    // backend that never minted it and gets "session not found".
    let sessionIdAtPublication: null | string = 'unset'

    ensureGatewayAgent.mockImplementationOnce(async (connectionId, profile, options?: ActivationOptions) => {
      if (options?.beforeActivate && !options.beforeActivate()) {
        return
      }

      sessionIdAtPublication = $activeSessionId.get()
      trace.push(`publish:${connectionId}::${profile}`)
      $connection.set({ connectionId: connectionId ?? undefined, mode: 'remote', profile, registryScoped: true })
    })

    const remoteRow = paletteRows('local', 'default').find(row => !row.isLocal)

    switchToAgentRow(remoteRow!, switchFailed)
    await settle()

    expect(sessionIdAtPublication).toBeNull()
  })

  it('routes the local row through the registry `local` source, not a sentinel', async () => {
    // Sitting on the remote; picking This device must dial the registry's own
    // local entry and take the identical commit path.
    $connection.set({ connectionId: 'hermes-dev', mode: 'remote', profile: 'research', registryScoped: true })
    $activeGatewayProfile.set('research')

    const localRow = paletteRows('hermes-dev', 'research').find(row => row.isLocal)

    expect(localRow?.connectionId).toBe('local')
    switchToAgentRow(localRow!, switchFailed)
    await settle()

    expect(openGatewayAgent).toHaveBeenCalledWith('local', 'default')
    expect(trace).toEqual(['dial:local::default', 'begin', 'wipe', 'publish:local::default'])
  })

  it('surfaces a failed switch with the connection-switcher error shape', async () => {
    openGatewayAgent.mockRejectedValueOnce(new Error('ssh tunnel refused'))

    const remoteRow = paletteRows('local', 'default').find(row => !row.isLocal)

    switchToAgentRow(remoteRow!, switchFailed)
    await settle()

    expect(notifyError).toHaveBeenCalledWith(expect.any(Error), 'Could not connect to hermes-dev-env')
    // A dial that never landed must not have wiped the still-active source.
    expect(wipeSessionListsForGatewaySwitch).not.toHaveBeenCalled()
    expect($connection.get()?.connectionId).toBe('local')
  })
})
