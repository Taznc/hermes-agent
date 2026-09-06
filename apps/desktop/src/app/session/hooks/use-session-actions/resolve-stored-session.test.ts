import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as HermesModule from '@/hermes'
import { getSession } from '@/hermes'
import { __resetMissingProfiles } from '@/lib/profile-liveness'
import { $pinnedSessionIds } from '@/store/layout'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $prBranchBySession, $prScannedSessions } from '@/store/pull-requests'
import { $projectTree } from '@/store/projects'
import { $sessionSeenCounts, $unreadFinishedMarkers } from '@/store/session-unread'
import { $cronSessions, $messagingSessions, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { __resetSessionProbeCache, cachedSessionRow, resolveSessionProfile, resolveStoredSession } from './utils'

vi.mock('@/hermes', async importActual => ({
  ...(await importActual<typeof HermesModule>()),
  getSession: vi.fn()
}))

const mockGetSession = vi.mocked(getSession)

const session = (over: Partial<SessionInfo>): SessionInfo => over as SessionInfo

const profiles = (...names: string[]) => names.map(name => ({ name }) as never)

describe('resolveStoredSession profile ownership', () => {
  beforeEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $projectTree.set([])
    $pinnedSessionIds.set([])
    $prBranchBySession.set({})
    $prScannedSessions.set([])
    $sessionSeenCounts.set({})
    $unreadFinishedMarkers.set({})
    $profiles.set(profiles('default', 'meta'))
    $activeGatewayProfile.set('meta')
    mockGetSession.mockReset()
    // Dead-profile memory is module state shared across tests.
    __resetMissingProfiles()
    // So is the negative/in-flight probe cache: a miss recorded by one test
    // would otherwise short-circuit the next test's lookup of the same id.
    __resetSessionProbeCache()
  })

  afterEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $projectTree.set([])
    $pinnedSessionIds.set([])
    $prBranchBySession.set({})
    $prScannedSessions.set([])
    $sessionSeenCounts.set({})
    $unreadFinishedMarkers.set({})
    $profiles.set([])
    $activeGatewayProfile.set('default')
  })

  it('returns a cached row that carries an owning profile', async () => {
    $sessions.set([session({ id: 's1', profile: 'default' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it.each([
    ['cron', $cronSessions],
    ['messaging', $messagingSessions]
  ])('resolves a %s sidebar row without duplicating it into regular sessions', async (_source, store) => {
    store.set([session({ id: 's1', profile: 'default' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    expect(mockGetSession).not.toHaveBeenCalled()
    expect($sessions.get()).toEqual([])
  })

  it('treats a profile-less cache hit as unresolved when multiple profiles exist', async () => {
    $sessions.set([session({ id: 's1' })])
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // rung 2 (active profile) then rung 3 (stamped cross-profile probe)
    expect(mockGetSession).toHaveBeenNthCalledWith(1, 's1', 'meta')
    expect(mockGetSession).toHaveBeenNthCalledWith(2, 's1', 'default')
  })

  it('scopes the first by-id lookup so a miss does not skip the active profile', async () => {
    $activeGatewayProfile.set('brain')
    $profiles.set(profiles('default', 'brain'))
    mockGetSession.mockImplementation(async (id, profile) => {
      if (profile === 'brain') {
        return session({ id, profile: 'brain' })
      }

      throw new Error('404: Session not found')
    })

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('brain')
    expect(mockGetSession).toHaveBeenCalledWith('s1', 'brain')
    expect(mockGetSession).not.toHaveBeenCalledWith('s1')
    expect(mockGetSession).not.toHaveBeenCalledWith('s1', 'default')
  })

  it('accepts a profile-less cache hit for single-profile users', async () => {
    $profiles.set(profiles('default'))
    $sessions.set([session({ id: 's1' })])

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.id).toBe('s1')
    expect(mockGetSession).not.toHaveBeenCalled()
  })

  it('stamps the active profile on a bare by-id hit from an older backend', async () => {
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect(mockGetSession).toHaveBeenCalledWith('s1', 'meta')
    // the upserted cache row is owned too, so the next hit short-circuits
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
  })

  it('probed desktop profile overrides a remote backend answering as its own "default"', async () => {
    // Per-profile remote override: Electron strips the desktop alias before
    // forwarding, so the standalone backend stamps its backend-local root.
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))
    $activeGatewayProfile.set('default')
    $profiles.set(profiles('default', 'meta'))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('meta')
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('meta')
  })

  it('stamps the probed profile on a scoped hit from an older backend that omits it', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1' }))

    const resolved = await resolveStoredSession('s1')

    expect(resolved?.profile).toBe('default')
    // the cached row is owned too — no unowned row is ever re-cached
    expect($sessions.get().find(s => s.id === 's1')?.profile).toBe('default')
  })

  it('resolveSessionProfile routes a default-profile session from a non-default gateway', async () => {
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's1', profile: 'default' }))

    await expect(resolveSessionProfile('s1')).resolves.toBe('default')
  })

  // Regression: the cross-profile probe's catch treated every failure alike, so
  // a profile the Electron spawn guard had already declared permanently gone
  // ("no longer exists") was re-probed on every single lookup — the repeating
  // `?profile=<dead>` bursts in the dev console. A dead profile must drop out
  // of the fan-out after the first rejection.
  it('stops probing a profile once the spawn guard reports it gone', async () => {
    $profiles.set(profiles('default', 'meta', 'ghost'))
    $activeGatewayProfile.set('meta')

    // First lookup: active backend misses, `default` misses, `ghost` is gone.
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockRejectedValueOnce(new Error('Profile "ghost" no longer exists.'))

    await expect(resolveStoredSession('s1')).resolves.toBeUndefined()
    expect(mockGetSession).toHaveBeenCalledWith('s1', 'ghost')

    // Second lookup: `ghost` must not be probed again.
    mockGetSession.mockReset()
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    await expect(resolveStoredSession('s2')).resolves.toBeUndefined()

    const probed = mockGetSession.mock.calls.map(call => call[1])

    expect(probed).not.toContain('ghost')
  })

  // The other half of the contract: a plain 404 is a legitimate miss, not a
  // dead profile. Blacklisting on a 404 would skip the profile that actually
  // owns a later session and make it unresolvable.
  it('keeps probing a profile that merely 404s for one session id', async () => {
    $profiles.set(profiles('default', 'meta'))
    $activeGatewayProfile.set('meta')

    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))

    await expect(resolveStoredSession('missing')).resolves.toBeUndefined()

    // A later id that DOES live on `default` still resolves.
    mockGetSession.mockReset()
    mockGetSession.mockRejectedValueOnce(new Error('404: Session not found'))
    mockGetSession.mockResolvedValueOnce(session({ id: 's9', profile: 'default' }))

    await expect(resolveStoredSession('s9')).resolves.toMatchObject({ profile: 'default' })
  })

  // The negative cache is what keeps a stuck id (dead deep link, orphaned Bot
  // tile) from re-running the N-profile fan-out on every 1.5s backstop poll.
  it('serves a repeat miss from the negative cache instead of re-probing', async () => {
    mockGetSession.mockRejectedValue(new Error('404: Session not found'))

    await expect(resolveStoredSession('stuck')).resolves.toBeUndefined()

    const afterFirst = mockGetSession.mock.calls.length

    expect(afterFirst).toBeGreaterThan(0)

    await expect(resolveStoredSession('stuck')).resolves.toBeUndefined()

    expect(mockGetSession).toHaveBeenCalledTimes(afterFirst)
  })

  it('prunes client-only caches only after every profile rejects the stored id', async () => {
    $sessionSeenCounts.set({ default: { keep: 1, stuck: 2 }, meta: { stuck: 3 } })
    $unreadFinishedMarkers.set({ default: ['keep', 'stuck'], meta: ['stuck'] })
    $prScannedSessions.set(['keep', 'stuck'])
    $prBranchBySession.set({ keep: 'repo\nkeep', stuck: 'repo\nstuck' })
    // Pins reconcile against their backend row and are not a resolver cache.
    $pinnedSessionIds.set(['stuck'])
    mockGetSession.mockRejectedValue(new Error('404: Session not found'))

    await expect(resolveStoredSession('stuck')).resolves.toBeUndefined()

    expect($sessionSeenCounts.get()).toEqual({ default: { keep: 1 } })
    expect($unreadFinishedMarkers.get()).toEqual({ default: ['keep'] })
    expect($prScannedSessions.get()).toEqual(['keep'])
    expect($prBranchBySession.get()).toEqual({ keep: 'repo\nkeep' })
    expect($pinnedSessionIds.get()).toEqual(['stuck'])
  })

  it('does not prune client caches when a profile resolves the stored id', async () => {
    const seen = { default: { live: 2 } }
    const markers = { default: ['live'] }
    const scanned = ['live']
    const branches = { live: 'repo\nlive' }
    $sessionSeenCounts.set(seen)
    $unreadFinishedMarkers.set(markers)
    $prScannedSessions.set(scanned)
    $prBranchBySession.set(branches)
    mockGetSession.mockResolvedValueOnce(session({ id: 'live', profile: 'meta' }))

    await expect(resolveStoredSession('live')).resolves.toMatchObject({ id: 'live' })

    expect($sessionSeenCounts.get()).toEqual(seen)
    expect($unreadFinishedMarkers.get()).toEqual(markers)
    expect($prScannedSessions.get()).toEqual(scanned)
    expect($prBranchBySession.get()).toEqual(branches)
  })

  // It is a TTL, not a blacklist: once the window lapses the id is probed
  // again, so a session that appears moments later still resolves.
  it('re-probes after the negative TTL lapses', async () => {
    vi.useFakeTimers()

    try {
      mockGetSession.mockRejectedValue(new Error('404: Session not found'))

      await expect(resolveStoredSession('later')).resolves.toBeUndefined()

      mockGetSession.mockReset()
      vi.advanceTimersByTime(20_000)

      mockGetSession.mockResolvedValueOnce(session({ id: 'later', profile: 'default' }))

      await expect(resolveStoredSession('later')).resolves.toMatchObject({ profile: 'default' })
    } finally {
      vi.useRealTimers()
    }
  })

  // Concurrent callers (wiring.tsx probes + the backstop poll) must share one
  // fan-out, not each start their own.
  it('de-dups concurrent lookups of the same id into one probe', async () => {
    mockGetSession.mockRejectedValue(new Error('404: Session not found'))

    // Baseline: what one lookup costs in backend calls.
    await resolveStoredSession('same')

    const singleProbeCalls = mockGetSession.mock.calls.length

    expect(singleProbeCalls).toBeGreaterThan(0)

    __resetSessionProbeCache()
    mockGetSession.mockClear()

    // Three simultaneous callers must not cost three fan-outs.
    const results = await Promise.all([
      resolveStoredSession('same'),
      resolveStoredSession('same'),
      resolveStoredSession('same')
    ])

    expect(results).toEqual([undefined, undefined, undefined])
    expect(mockGetSession.mock.calls.length).toBe(singleProbeCalls)
  })
})

describe('cachedSessionRow owner preference', () => {
  const projectNode = (sessions: SessionInfo[], preview: SessionInfo[] = []) =>
    ({
      previewSessions: preview,
      repos: [{ groups: [{ sessions }] }]
    }) as never

  beforeEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $projectTree.set([])
    mockGetSession.mockReset()
  })

  afterEach(() => {
    $cronSessions.set([])
    $messagingSessions.set([])
    $sessions.set([])
    $projectTree.set([])
  })

  it('prefers a self-describing project-tree row over an ownerless Recents duplicate', () => {
    // The same conversation, listed twice: a legacy Recents row with no owner
    // and the profile-scoped project-tree row the gateway stamped. Picking the
    // Recents copy throws away the only routing information there is, and the
    // branch then creates its child on whichever backend is active.
    $sessions.set([session({ cwd: '/wrong', id: 's1' })])
    $projectTree.set([projectNode([session({ connection_id: 'pandora', cwd: '/right', id: 's1', profile: 'work' })])])

    expect(cachedSessionRow('s1')).toMatchObject({ connection_id: 'pandora', cwd: '/right', profile: 'work' })
  })

  it('finds a project-tree preview row when the session is in no other list', () => {
    $projectTree.set([projectNode([], [session({ connection_id: 'rigremote', id: 's1', profile: 'default' })])])

    expect(cachedSessionRow('s1')).toMatchObject({ connection_id: 'rigremote' })
  })

  it('keeps the plain Recents row when nothing carries an owner', () => {
    $sessions.set([session({ cwd: '/only', id: 's1' })])

    expect(cachedSessionRow('s1')).toMatchObject({ cwd: '/only' })
    expect(cachedSessionRow('missing')).toBeUndefined()
  })
})
