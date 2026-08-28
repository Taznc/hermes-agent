// Reference-stability + isolation contract for the shared sidebar derivation
// chain (store/sidebar-model.ts). This is what makes the section-component
// split in sidebar/index.tsx actually pay off: a section subscribing to one
// of these `computed()` stores only re-renders when THAT store's reference
// changes, and nanostores' `computed` already skips a re-notify when none of
// its own direct dependencies changed reference (see computed/index.js).
//
// These tests assert the property at the STORE layer (cheap, deterministic,
// no DOM) rather than re-deriving it via React render counts: a computed
// store keeping its reference IS the reason a subscribing component's render
// bails out, so proving the former proves the latter for any correctly
// written `useStore($leafStore)` consumer.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import type { SessionInfo } from '@/types/hermes'

import { $cronJobs } from './cron'
import { $pinnedSessionIds, $sidebarStatusFilter } from './layout'
import { $messagingSessions, $sessions, setSessions } from './session'
import { clearAllSessionStates, publishSessionState } from './session-states'
import {
  $sidebarIsPinnedSession,
  $sidebarMessagingGroups,
  $sidebarPinnedSessions,
  $sidebarProjectModel,
  $sidebarScopedSessions,
  $sidebarSortedSessions,
  $sidebarUnpinnedAgentSessions,
  $sidebarVisibleSessions
} from './sidebar-model'

vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key
}))

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

describe('sidebar-model reference stability', () => {
  beforeEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $messagingSessions.set([])
    $pinnedSessionIds.set([])
    $sidebarStatusFilter.set([])
    $cronJobs.set([])
  })

  afterEach(() => {
    clearAllSessionStates()
    $sessions.set([])
    $messagingSessions.set([])
    $pinnedSessionIds.set([])
    $sidebarStatusFilter.set([])
    $cronJobs.set([])
  })

  it('$sidebarScopedSessions keeps its reference across an UNRELATED dot-state tick', () => {
    setSessions([row('s1'), row('s2')])

    const first = $sidebarScopedSessions.get()

    // A dot-state-adjacent publish that doesn't touch $sessions at all.
    publishSessionState('rt-unrelated', { ...createClientSessionState('s1'), busy: true })

    expect($sidebarScopedSessions.get()).toBe(first)
  })

  it('$sidebarVisibleSessions recomputes (new reference) when $sessions itself changes', () => {
    setSessions([row('s1'), row('s2')])
    const first = $sidebarVisibleSessions.get()

    setSessions([row('s1'), row('s2'), row('s3')])

    expect($sidebarVisibleSessions.get()).not.toBe(first)
  })

  it('$sidebarSortedSessions and $sidebarUnpinnedAgentSessions stay stable when only cron jobs tick', () => {
    setSessions([row('s1'), row('s2')])

    const sorted = $sidebarSortedSessions.get()
    const unpinned = $sidebarUnpinnedAgentSessions.get()

    // A cron-only tick: $sidebarMessagingGroups' sibling store, $cronJobs, has
    // NOTHING in this chain's dependency list, so a cron tick must not
    // recompute session-derived stores at all.
    $cronJobs.set([{ id: 'job-1' } as never])

    expect($sidebarSortedSessions.get()).toBe(sorted)
    expect($sidebarUnpinnedAgentSessions.get()).toBe(unpinned)
  })

  it('$sidebarPinnedSessions and $sidebarIsPinnedSession stay stable when only cron JOBS tick', () => {
    setSessions([row('s1')])
    $pinnedSessionIds.set(['s1'])

    const pinned = $sidebarPinnedSessions.get()
    const isPinned = $sidebarIsPinnedSession.get()

    // $cronJobs (the job LIST, not cron run sessions) is not in the pinned
    // chain's dependency list at all — this is the sidebar's actual Cron
    // section data, decoupled on purpose so a cron tick can't cascade into
    // session-derived stores it has no bearing on.
    $cronJobs.set([{ id: 'job-1' } as never])

    expect($sidebarPinnedSessions.get()).toBe(pinned)
    expect($sidebarIsPinnedSession.get()).toBe(isPinned)
  })

  it('$sidebarMessagingGroups stays stable when only $sessions (recents) ticks', () => {
    $messagingSessions.set([row('m1', { source: 'telegram' })])

    const first = $sidebarMessagingGroups.get()

    setSessions([row('s1')])

    expect($sidebarMessagingGroups.get()).toBe(first)
  })

  it('$sidebarProjectModel stays stable when only messaging ticks (no project-tree input changed)', () => {
    const first = $sidebarProjectModel.get()

    $messagingSessions.set([row('m1', { source: 'telegram' })])

    expect($sidebarProjectModel.get()).toBe(first)
  })
})
