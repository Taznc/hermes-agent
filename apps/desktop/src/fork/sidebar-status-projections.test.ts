import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $sidebarStatusFilter } from '@/store/layout'
import { $sessions, $unreadFinishedSessionIds } from '@/store/session'
import { $sessionDotStateById, hasLiveTurn, sessionStatusBucket } from '@/store/session-dot-state'
import { $sessionStates, clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

import { $sidebarFilterDotStateById as buckets, $sidebarLiveDotStateById as live } from './sidebar-status-projections'

function reset() {
  clearAllSessionStates()
  $sessions.set([])
  $unreadFinishedSessionIds.set([])
  $sidebarStatusFilter.set([])
}

beforeEach(reset)
afterEach(reset)

describe('authoritative sidebar projections', () => {
  it('preserves lineage aliases, unloaded ids and direct raw-state resets', () => {
    $sidebarStatusFilter.set(['working'])
    $sessions.set([makeSessionInfo({ id: 'tip', _lineage_root_id: 'root', profile: 'work' })])
    publishSessionState('runtime', { ...createClientSessionState('tip'), busy: true })
    publishSessionState('off-page-runtime', { ...createClientSessionState('off-page'), busy: true })

    for (const id of ['tip', 'root', 'off-page']) {
      expect(sessionStatusBucket(buckets.get()[id])).toBe(sessionStatusBucket($sessionDotStateById.get()[id]))
      expect(hasLiveTurn(live.get()[id] ?? 'idle')).toBe(true)
    }

    $sessionStates.set({})
    expect(live.get()).toEqual({})
    expect(sessionStatusBucket(buckets.get()['off-page'])).toBe('idle')
  })

  it('does not invent unknown status or keep stale membership after profile/catalog replacement', () => {
    $sidebarStatusFilter.set(['needs-input', 'working'])
    publishSessionState('runtime', { ...createClientSessionState('off-page'), busy: true })
    expect(sessionStatusBucket(buckets.get().unknown)).toBe('idle')
    expect(hasLiveTurn(live.get().unknown ?? 'idle')).toBe(false)
    clearAllSessionStates()
    $sessions.set([makeSessionInfo({ id: 'other-profile', profile: 'other', unread: true })])
    expect(live.get()).toEqual({})
    expect(sessionStatusBucket(buckets.get()['other-profile'])).toBe('unread')
    expect(buckets.get()['off-page']).toBeUndefined()
  })
})
