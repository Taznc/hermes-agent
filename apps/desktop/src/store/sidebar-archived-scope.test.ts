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

import { $pinnedSessionIds, $sidebarGrouping, $sidebarShowArchived, setSidebarGrouping, setSidebarShowArchived } from './layout'
import { $projectTree } from './projects'
import { $cronSessions, $messagingSessions, $sessions, mergeSessionPage, setCronSessions, setMessagingSessions } from './session'
import { $archivedSessions } from './sidebar-archive'
import {
  $sidebarMessagingGroups,
  $sidebarPinnedSessions,
  $sidebarProjectModel,
  $sidebarScopedSessions,
  $sidebarUnpinnedAgentSessions,
  $sidebarVisibleCronSessions,
  $sidebarVisibleMessagingSessions,
  $sidebarWorktreeGroupingActive
} from './sidebar-model'

vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key
}))

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

describe('sidebar archived-row scoping', () => {
  beforeEach(() => {
    $sessions.set([])
    $archivedSessions.set([])
    $projectTree.set([])
    $sidebarShowArchived.set(false)
    $pinnedSessionIds.set([])
    $cronSessions.set([])
    $messagingSessions.set([])
    setSidebarGrouping('date')
  })

  afterEach(() => {
    $sessions.set([])
    $archivedSessions.set([])
    $projectTree.set([])
    $sidebarShowArchived.set(false)
    $pinnedSessionIds.set([])
    $cronSessions.set([])
    $messagingSessions.set([])
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

  it('drops archived rows from a stale backend project-tree snapshot', () => {
    // The web spike can run a newer renderer against an older managed backend.
    // Do not trust an old `projects.tree` response to honor archived=exclude:
    // every snapshot row still has to satisfy the same live-view invariant.
    const live = row('live-1')
    const archived = row('archived-elsewhere', { archived: true })

    $projectTree.set([
      {
        id: 'project-1',
        label: 'Project',
        path: '/repo',
        repos: [
          {
            groups: [{ id: 'main', isMain: true, label: 'main', path: '/repo', sessions: [live, archived] }],
            id: '/repo',
            label: 'repo',
            path: '/repo',
            sessionCount: 2
          }
        ],
        sessionCount: 2
      }
    ])

    const model = $sidebarProjectModel.get()

    expect(model[0].repos[0].groups[0].sessions.map(session => session.id)).toEqual(['live-1'])
    expect(model[0].sessionCount).toBe(1)
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

  // Regression (t_7feb9e6d): external `hermes sessions archive --ids` updates
  // the durable DB, but the desktop's own `$sessions` cache was populated
  // BEFORE that mutation. The next normal-mode page excludes the now-archived
  // row, yet `mergeSessionPage` deliberately RETAINS `keep`-protected rows
  // (pinned / working / open tiles / active) that the server page dropped —
  // so a stale `{archived: false}` snapshot of that exact row survives the
  // merge and re-enters the normal sidebar even though the independent
  // archived-only query already reports it `archived: true`. The centralized
  // membership boundary this card owns must reject that stale row from every
  // normal-mode surface (flat + grouped) regardless of why mergeSessionPage
  // kept it, and it must still surface once Archived is toggled on.
  describe('external CLI archive of an open/pinned tile (stale mergeSessionPage survivor)', () => {
    for (const grouping of ['project', 'date'] as const) {
      it(`does not include a kept stale archived=false row in normal ${grouping} sidebar`, () => {
        setSidebarGrouping(grouping)
        const x = row('external-archive-X', { archived: false })
        // External archive: the fresh normal-mode page no longer carries X.
        const incoming: SessionInfo[] = []
        const retained = mergeSessionPage([x], incoming, [x.id])
        $sessions.set(retained)
        $archivedSessions.set([row(x.id, { archived: true })])

        expect(retained).toContainEqual(x) // proves the stale cache survives the merge
        expect($sidebarScopedSessions.get().map(session => session.id)).not.toContain(x.id)
        expect($sidebarUnpinnedAgentSessions.get().map(session => session.id)).not.toContain(x.id)

        // A stale pin is another normal-sidebar surface, independent of rows
        // rendered by the server-owned project tree.
        $pinnedSessionIds.set([x.id])
        expect($sidebarPinnedSessions.get().map(session => session.id)).not.toContain(x.id)
      })
    }

    it('shows the canonical archived row once Archived is on', () => {
      const x = row('external-archive-X', { archived: false })

      $sessions.set(mergeSessionPage([x], [], [x.id]))
      $archivedSessions.set([row(x.id, { archived: true })])
      setSidebarShowArchived(true)

      expect($sidebarScopedSessions.get().map(session => session.id)).toContain(x.id)
    })
  })

  // Regression (round-2 review, t_d0a6300e finding 2): the Cron and Messaging
  // sidebar sections, and everything downstream of them (Pinned via
  // `$sidebarSessionByAnyId`, the per-platform Messaging groups), read the raw
  // `$cronSessions`/`$messagingSessions` stores directly and had no archive
  // guard of their own — only the flat Recents / project-tree path did. A
  // cron run or messaging thread archived by another surface, while this
  // client's cache still holds a stale `archived: false` copy (same class of
  // leak as the mergeSessionPage-survivor case above), stayed visible in
  // those sections even with the Archived filter off.
  describe('cron and messaging sections honor the same centralized policy', () => {
    it('drops a stale archived=false cron row from the Cron section', () => {
      const x = row('cron-external-archive-X', { archived: false, source: 'cron' })

      setCronSessions([x])
      $archivedSessions.set([row(x.id, { archived: true })])

      expect($sidebarVisibleCronSessions.get().map(session => session.id)).not.toContain(x.id)
    })

    it('drops a stale archived=false messaging row from the Messaging section and its groups', () => {
      const x = row('msg-external-archive-X', { archived: false, source: 'telegram' })

      setMessagingSessions([x])
      $archivedSessions.set([row(x.id, { archived: true })])

      expect($sidebarVisibleMessagingSessions.get().map(session => session.id)).not.toContain(x.id)
      expect($sidebarMessagingGroups.get().flatMap(group => group.sessions.map(session => session.id))).not.toContain(
        x.id
      )
    })

    it('a stale archived cron/messaging row cannot resurface via Pinned', () => {
      const cronRow = row('cron-external-archive-X', { archived: false, source: 'cron' })
      const messagingRow = row('msg-external-archive-X', { archived: false, source: 'telegram' })

      setCronSessions([cronRow])
      setMessagingSessions([messagingRow])
      $archivedSessions.set([row(cronRow.id, { archived: true }), row(messagingRow.id, { archived: true })])
      $pinnedSessionIds.set([cronRow.id, messagingRow.id])

      const pinnedIds = $sidebarPinnedSessions.get().map(session => session.id)

      expect(pinnedIds).not.toContain(cronRow.id)
      expect(pinnedIds).not.toContain(messagingRow.id)
    })

    it('still shows cron/messaging rows once Archived is on and they are the canonical archived copy', () => {
      const cronRow = row('cron-external-archive-X', { archived: false, source: 'cron' })

      setCronSessions([cronRow])
      $archivedSessions.set([row(cronRow.id, { archived: true })])
      setSidebarShowArchived(true)

      // Archived mode reads the archived-only store directly (not the
      // cron/messaging sections), so the canonical archived copy must still
      // be reachable there — the policy above must not also hide it in
      // Archived mode.
      expect($archivedSessions.get().map(session => session.id)).toContain(cronRow.id)
    })
  })
})
