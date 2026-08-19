import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sessionsLimit, resetSessionsLimit, SIDEBAR_SESSIONS_PAGE_SIZE } from '@/store/layout'
import {
  $cronSessions,
  $freshDraftReady,
  $messagingSessions,
  $sessionProfilesTruncated,
  $sessions,
  $sessionsLoading,
  setCronSessions,
  setFreshDraftReady,
  setMessagingSessions,
  setSessionProfilesTruncated,
  setSessions,
  setSessionsLoading
} from '@/store/session'
import { $stalledSessionIds } from '@/store/session-states'

import { $gatewaySwitching, wipeSessionListsForGatewaySwitch } from './gateway-switch'

vi.mock('@/lib/query-client', () => ({
  invalidateProfileScopedQueries: vi.fn()
}))

vi.mock(import('@/store/profile'), async importOriginal => {
  const actual = await importOriginal()

  return {
    ...actual,
    invalidateProfileListFetches: vi.fn()
  }
})

const { invalidateProfileListFetches } = await import('@/store/profile')

describe('wipeSessionListsForGatewaySwitch', () => {
  beforeEach(() => {
    $gatewaySwitching.set(false)
    setSessions([{ id: 's1', title: 'old', profile: 'default' } as never])
    setSessionProfilesTruncated({ default: true })
    setCronSessions([{ id: 'c1', title: 'cron', profile: 'default' } as never])
    setMessagingSessions([{ id: 'm1', title: 'tg', profile: 'default' } as never])
    $stalledSessionIds.set(['s1'])
    setSessionsLoading(false)
    setFreshDraftReady(false)
    $sessionsLimit.set(SIDEBAR_SESSIONS_PAGE_SIZE * 3)
  })

  afterEach(() => {
    resetSessionsLimit()
    setSessions([])
    setCronSessions([])
    setMessagingSessions([])
    $stalledSessionIds.set([])
    setSessionsLoading(true)
    $gatewaySwitching.set(false)
  })

  it('clears lists and arms loading so sidebar skeletons retrigger', () => {
    wipeSessionListsForGatewaySwitch()

    expect($sessions.get()).toEqual([])
    expect($sessionProfilesTruncated.get()).toEqual({})
    expect($cronSessions.get()).toEqual([])
    expect($messagingSessions.get()).toEqual([])
    expect($stalledSessionIds.get()).toEqual([])
    expect($sessionsLoading.get()).toBe(true)
    expect($sessionsLimit.get()).toBe(SIDEBAR_SESSIONS_PAGE_SIZE)
    expect($freshDraftReady.get()).toBe(true)
  })

  it('strands in-flight profile-list fetches so the old backend cannot repaint the rail (#85731)', () => {
    // The soft re-home moves /api/profiles routing to the NEW backend; a
    // response still in flight from the previous one must be invalidated
    // here, in the same wipe every connection/mode apply funnels through.
    wipeSessionListsForGatewaySwitch()

    expect(invalidateProfileListFetches).toHaveBeenCalled()
  })
})

describe('wipeSessionListsForGatewaySwitch → projects', () => {
  it('drops the previous backend\'s projects, tree and drilled-in scope', async () => {
    const { $activeProjectId, $projects, $projectScope, $projectsRpcAvailable, $projectTree, ALL_PROJECTS } =
      await import('@/store/projects')

    // State as it looks after browsing the LOCAL machine: repo names from
    // /Users/..., a drilled-in project, and a probed capability verdict.
    $projects.set([{ id: 'p_local', label: 'hermes-agent', path: '/Users/me/Projects/hermes' } as never])
    $projectTree.set([{ id: 'p_local', label: 'hermes-agent' } as never])
    $activeProjectId.set('p_local')
    $projectScope.set('p_local')
    $projectsRpcAvailable.set(true)

    wipeSessionListsForGatewaySwitch()

    // Nothing from the previous machine may survive into the next one — this
    // is what left local repo names in the sidebar while the chat ran remote.
    expect($projects.get()).toEqual([])
    expect($projectTree.get()).toEqual([])
    expect($activeProjectId.get()).toBeNull()
    expect($projectsRpcAvailable.get()).toBeNull()
    // The persisted scope is backend-specific: keeping it would scope the new
    // sidebar to a project id the new backend has never issued.
    expect($projectScope.get()).toBe(ALL_PROJECTS)
  })
})
