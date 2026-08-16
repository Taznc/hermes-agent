import { describe, expect, it } from 'vitest'

import type { DesktopAgentRoster } from '@/global'

import { buildAgentPaletteRows } from './agent-rows'

// The palette's "Agents & connections" rows are the only built-in surface that
// lists agents living on OTHER machines: the profile rail renders /api/profiles
// from whichever backend is currently active, so a remote source's profiles are
// invisible there until you are already on it (#85731).
//
// These assert the two rules that are invisible from rendering: suppression for
// single-source users, and active-row identity being the (connection, profile)
// PAIR rather than the profile name.

const normalizeProfile = (name: string) => (name || '').trim() || 'default'

const roster = (over: Partial<DesktopAgentRoster> = {}): DesktopAgentRoster =>
  ({
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
        handle: '@default-hermes-dev-env',
        profile: 'default'
      }
    ],
    sources: [
      { connectionId: 'local', kind: 'local', label: 'This device', reachable: true },
      { connectionId: 'hermes-dev', kind: 'ssh', label: 'hermes-dev-env', reachable: true }
    ],
    ...over
  }) as DesktopAgentRoster

const build = (over: Partial<Parameters<typeof buildAgentPaletteRows>[0]> = {}) =>
  buildAgentPaletteRows({
    activeConnectionId: null,
    activeProfile: 'default',
    localLabel: 'This device',
    normalizeProfile,
    roster: roster(),
    ...over
  })

describe('buildAgentPaletteRows suppression', () => {
  it('contributes nothing when only the local runtime is registered', () => {
    // Single-source users already have every agent in the profile rail; a second
    // list in the palette would be pure noise for the majority case.
    const single = roster({
      agents: [
        {
          connectionId: 'local',
          connectionKind: 'local',
          connectionLabel: 'This device',
          handle: 'default',
          profile: 'default'
        }
      ],
      sources: [{ connectionId: 'local', kind: 'local', label: 'This device', reachable: true }]
    } as Partial<DesktopAgentRoster>)

    expect(build({ roster: single })).toEqual([])
  })

  it('contributes nothing when the bridge is missing (older Desktop build)', () => {
    expect(build({ roster: null })).toEqual([])
    expect(build({ roster: undefined })).toEqual([])
  })

  it('lists every source once a second connection exists', () => {
    expect(build().map(row => row.profile)).toEqual(['default', 'default'])
  })
})

describe('buildAgentPaletteRows active identity', () => {
  it('marks the LOCAL row active when the window is on the local pool', () => {
    const rows = build({ activeConnectionId: null, activeProfile: 'default' })

    expect(rows.find(row => row.isLocal)?.isActive).toBe(true)
    // The remote row shares the profile NAME but is a different machine.
    expect(rows.find(row => !row.isLocal)?.isActive).toBe(false)
  })

  it('marks the REMOTE row active when dialed through its connection', () => {
    const rows = build({ activeConnectionId: 'hermes-dev', activeProfile: 'default' })

    expect(rows.find(row => !row.isLocal)?.isActive).toBe(true)
    expect(rows.find(row => row.isLocal)?.isActive).toBe(false)
  })

  it('never marks two rows active for the same profile name across sources', () => {
    for (const activeConnectionId of [null, 'hermes-dev']) {
      const active = build({ activeConnectionId, activeProfile: 'default' }).filter(row => row.isActive)

      expect(active).toHaveLength(1)
    }
  })
})

describe('buildAgentPaletteRows row shape', () => {
  it('gives local rows a null connectionId so selectAgent takes the profile path', () => {
    const rows = build()

    expect(rows.find(row => row.isLocal)?.connectionId).toBeNull()
    expect(rows.find(row => !row.isLocal)?.connectionId).toBe('hermes-dev')
  })

  it('labels the local row with the caller-supplied device name', () => {
    const rows = build({ localLabel: 'This Mac' })

    expect(rows.find(row => row.isLocal)?.device).toBe('This Mac')
    expect(rows.find(row => !row.isLocal)?.device).toBe('hermes-dev-env')
  })

  it('flags an unreachable source but still lists its agents', () => {
    const rows = build({
      roster: roster({
        sources: [
          { connectionId: 'local', kind: 'local', label: 'This device', reachable: true },
          {
            connectionId: 'hermes-dev',
            error: 'connect ECONNREFUSED',
            kind: 'ssh',
            label: 'hermes-dev-env',
            reachable: false
          }
        ]
      } as Partial<DesktopAgentRoster>)
    })

    // Listed, not dropped: a dead box should be visible and diagnosable, and an
    // SSH source is connect-on-demand rather than broken.
    expect(rows).toHaveLength(2)
    expect(rows.find(row => !row.isLocal)?.isUnreachable).toBe(true)
    expect(rows.find(row => row.isLocal)?.isUnreachable).toBe(false)
  })

  it('carries the @name-device handle through as a search keyword', () => {
    expect(build().find(row => !row.isLocal)?.handle).toBe('@default-hermes-dev-env')
  })
})
