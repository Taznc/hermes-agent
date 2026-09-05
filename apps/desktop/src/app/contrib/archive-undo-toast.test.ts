// Regression for review t_548d0d33, blocking issue 4, round 2: the two
// generic suites (`notifications.test.ts`'s "5th toast evicts + fires
// onEvict" and `session-archive-undo.test.ts`'s "commitPendingArchive drops
// the pending entry") each prove their own half works, but neither proves
// the two are actually WIRED together through wiring.tsx's real toast
// input. A broken `onEvict: () => commitPendingArchive(storedSessionId)`
// binding — wrong id, typo, or accidentally dropped — would leave both of
// those suites green while a real 5th archive left an orphaned, invisible,
// still-running undo timer. This test drives `buildArchiveUndoToastInput`
// (the actual seam wiring.tsx calls `notify()` with) against the real
// `$notifications` and `$pendingArchiveUndos` stores for five real pending
// archives, so the eviction path is proven end-to-end.
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patchArchived = vi.fn<(id: string, archived: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(
  () => Promise.resolve({ ok: true })
)

vi.mock('@/hermes', () => ({
  setApiRequestProfile: () => {},
  setSessionArchived: (id: string, archived: boolean, profile?: null | string) => patchArchived(id, archived, profile)
}))

import { $pinnedSessionIds } from '@/store/layout'
import { $notifications, clearNotifications, notify } from '@/store/notifications'
import { $removedSessionIds } from '@/store/session-removal'
import { $sessions, setSessions } from '@/store/session'
import {
  $pendingArchiveUndos,
  captureArchiveNeighbors,
  isArchiveUndoPending,
  registerPendingArchiveUndo,
  resetArchiveUndos
} from '@/store/session-archive-undo'

import { archiveUndoToastId, buildArchiveUndoToastInput } from './archive-undo-toast'

const row = (id: string): SessionInfo => ({ id, message_count: 1, source: 'cli', started_at: 0, title: id }) as SessionInfo

/** Mirrors the relevant slice of `archiveSessionViaSidebar` (wiring.tsx) +
 *  `archiveSession({ withUndo: true })` (use-session-actions/index.ts):
 *  capture neighbors, optimistically remove, register the pending undo, and
 *  show the REAL toast input this suite is verifying. */
function archiveWithToast(storedSessionId: string): void {
  const session = $sessions.get().find(s => s.id === storedSessionId)

  if (!session) {
    throw new Error(`test setup: no such session ${storedSessionId}`)
  }

  const { nextPinId, prevPinId } = captureArchiveNeighbors(storedSessionId)

  setSessions(prev => prev.filter(s => s.id !== storedSessionId))

  const writePromise = patchArchived(storedSessionId, true, undefined)

  registerPendingArchiveUndo({ nextPinId, prevPinId, session, storedSessionId, wasPinned: false, writePromise })

  notify(
    buildArchiveUndoToastInput({
      message: `Archived ${storedSessionId}`,
      onUndoFailed: () => {},
      storedSessionId,
      undoLabel: 'Undo'
    })
  )
}

beforeEach(() => {
  setSessions([])
  $pinnedSessionIds.set([])
  $removedSessionIds.set(new Set())
  resetArchiveUndos()
  clearNotifications()
  patchArchived.mockReset()
  patchArchived.mockResolvedValue({ ok: true })
})

describe('archive-undo toast <-> notification-cap eviction wiring', () => {
  it('evicting a 5th rapid archive commits its pending undo (cancels the timer) while the 4 visible ones stay pending and bound', () => {
    setSessions([row('a'), row('b'), row('c'), row('d'), row('e')])

    for (const id of ['a', 'b', 'c', 'd', 'e']) {
      archiveWithToast(id)
    }

    // All 5 archives are real, independent pending-undo entries — this is
    // the ">4 rapid archives" case the review explicitly called out.
    expect(Object.keys($pendingArchiveUndos.get()).sort()).toEqual(['b', 'c', 'd', 'e'])

    // 'a' was archived first, so its toast is the oldest and the one the
    // 4-item cap evicts once the 5th (toast for 'e') is added.
    expect(isArchiveUndoPending('a')).toBe(false)
    expect($notifications.get().map(n => n.id)).not.toContain(archiveUndoToastId('a'))

    // The 4 most recent archives are still fully live: pending undo entry
    // AND a visible, correctly-bound toast for each.
    for (const id of ['b', 'c', 'd', 'e']) {
      expect(isArchiveUndoPending(id)).toBe(true)
      expect($notifications.get().map(n => n.id)).toContain(archiveUndoToastId(id))
    }

    expect($notifications.get()).toHaveLength(4)
  })

  it("a visible archive toast's Undo action still restores its own session after other archives evicted an older one", async () => {
    setSessions([row('a'), row('b'), row('c'), row('d'), row('e')])

    for (const id of ['a', 'b', 'c', 'd', 'e']) {
      archiveWithToast(id)
    }

    const toastForE = $notifications.get().find(n => n.id === archiveUndoToastId('e'))

    expect(toastForE?.action).toBeDefined()

    toastForE?.action?.onClick()

    // onClick is a fire-and-forget `() => void` wrapper around the async
    // undoArchive() call (matching the real NotificationAction contract) —
    // flush microtasks so its internal awaits (serialization behind the
    // archive write, then the inverse PATCH) actually run before asserting.
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(patchArchived).toHaveBeenCalledWith('e', false, undefined)
    expect(isArchiveUndoPending('e')).toBe(false)
  })
})
