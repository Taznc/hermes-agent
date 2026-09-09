// Regression: archived sessions leaked into the sidebar's live views —
// most visibly the grouped-by-project view, which showed rows that had been
// archived while the Archived filter was OFF.
//
// `$sessions` is a CACHE of the backend's archived-excluded page, not a
// re-derivation of it. Two documented paths put an `archived: true` row in it:
// a session archived by another surface (CLI, another client, a bulk sweep)
// sits there until a refresh evicts it, and `mergeSessionPage` deliberately
// RETAINS `keep`-protected rows (pinned / working / open tiles / active) that
// the server page already dropped. The project overlays each carried their own
// `isLiveArchived` guard against this; the shared root did not, so every
// consumer that wasn't an overlay (flat Recents, profile groups) still showed
// them.
//
// These assert the relationship — "the live scope excludes archived, the
// archived scope contains exactly the archived set" — not a row snapshot.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { $sidebarGrouping, $sidebarShowArchived, setSidebarGrouping, setSidebarShowArchived } from './layout'
import { $sessions } from './session'
import { $archivedSessions } from './sidebar-archive'
import { $sidebarScopedSessions, $sidebarUnpinnedAgentSessions, $sidebarWorktreeGroupingActive } from './sidebar-model'

vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key
}))

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

describe('sidebar archived-row scoping', () => {
  beforeEach(() => {
    $sessions.set([])
    $archivedSessions.set([])
    $sidebarShowArchived.set(false)
    setSidebarGrouping('date')
  })

  afterEach(() => {
    $sessions.set([])
    $archivedSessions.set([])
    $sidebarShowArchived.set(false)
    setSidebarGrouping('date')
  })

  it('drops archived rows the live session cache is still holding', () => {
    $sessions.set([row('live-1'), row('archived-elsewhere', { archived: true })])

    expect($sidebarScopedSessions.get().map(session => session.id)).toEqual(['live-1'])
  })

  it('keeps them out of the derived list the project/profile groupings consume', () => {
    // $sidebarUnpinnedAgentSessions is what feeds overlayLivePreviews and the
    // grouped views, so the leak has to be gone at this end of the chain too.
    $sessions.set([row('live-1'), row('archived-elsewhere', { archived: true })])

    expect($sidebarUnpinnedAgentSessions.get().map(session => session.id)).toEqual(['live-1'])
  })

  it('shows the archived set — and only it — when the Archived filter is on', () => {
    $sessions.set([row('live-1')])
    $archivedSessions.set([row('archived-1', { archived: true })])

    setSidebarShowArchived(true)

    expect($sidebarScopedSessions.get().map(session => session.id)).toEqual(['archived-1'])
  })

  it('leaves the persisted grouping choice alone when the filter is toggled', () => {
    // The Archived view has no room for the workspace tree, so the grouping is
    // SUPPRESSED while it is on — but the user's choice must survive, so that
    // turning the filter back off restores the grouped view instead of
    // silently resetting them to flat.
    setSidebarGrouping('project')

    setSidebarShowArchived(true)

    expect($sidebarGrouping.get()).toBe('project')
    expect($sidebarWorktreeGroupingActive.get()).toBe(false)

    setSidebarShowArchived(false)

    expect($sidebarGrouping.get()).toBe('project')
    expect($sidebarWorktreeGroupingActive.get()).toBe(true)
  })
})
