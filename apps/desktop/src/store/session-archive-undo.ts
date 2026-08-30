import { atom } from 'nanostores'

import { setSessionArchived } from '@/hermes'
import type { SessionInfo } from '@/types/hermes'

import { $pinnedSessionIds } from './layout'
import { tombstoneSessions, untombstoneSessions } from './projects'
import {
  $selectedStoredSessionId,
  $sessions,
  sessionMatchesStoredId,
  sessionPinId,
  setSelectedStoredSessionId,
  setSessions
} from './session'

// ---------------------------------------------------------------------------
// Archive-with-undo state layer.
//
// The row-level archive icon (no confirmation dialog) needs to: archive
// instantly, keep enough of the prior row around to put it back exactly
// where it was, and let that reversal happen for a bounded window. This
// module owns that bookkeeping. It reuses the SAME backend mutation every
// other archive/unarchive path in the app already uses (`setSessionArchived`,
// from api/sessions.ts) — there is only ever one way to flip the archived
// flag, this just adds a client-side grace period on top of it.
//
// Scope: this is the STATE layer only. It manipulates the shared session
// list (`$sessions`), the pin list, and the optimistic-eviction tombstone set
// the same way the existing `archiveSession`/`unarchive` call sites do, so a
// row archived through here behaves identically to one archived through the
// tile context menu once the 10s window closes. It does not touch session
// TILES (open split panes) — that cleanup is owned by the existing
// `archiveSession` action in use-session-actions and is unrelated to whether
// undo is offered, so this path intentionally leaves it alone.
// ---------------------------------------------------------------------------

export const ARCHIVE_UNDO_WINDOW_MS = 10_000

export interface PendingArchiveUndo {
  /** The id the caller archived with (may be a lineage tip). */
  storedSessionId: string
  /** Full row snapshot as it looked immediately before archiving. */
  session: SessionInfo
  /** Index in `$sessions` the row lived at, for position-preserving restore. */
  index: number
  /** Whether the row was pinned at archive time. */
  wasPinned: boolean
  archivedAt: number
  expiresAt: number
}

/** Pending undo entries, keyed by the id passed to `archiveSessionWithUndo`.
 *  Observable so the UI (toast/snackbar) can render "N seconds left" or a
 *  stack of undoable archives without polling. */
export const $pendingArchiveUndos = atom<Record<string, PendingArchiveUndo>>({})

const timers = new Map<string, ReturnType<typeof setTimeout>>()

function clearPendingTimer(storedSessionId: string): void {
  const timer = timers.get(storedSessionId)

  if (timer !== undefined) {
    clearTimeout(timer)
    timers.delete(storedSessionId)
  }
}

function dropPendingEntry(storedSessionId: string): void {
  clearPendingTimer(storedSessionId)
  const current = $pendingArchiveUndos.get()

  if (!(storedSessionId in current)) {
    return
  }

  const { [storedSessionId]: _dropped, ...rest } = current

  $pendingArchiveUndos.set(rest)
}

function insertAt<T>(list: readonly T[], index: number, item: T): T[] {
  const clamped = Math.max(0, Math.min(index, list.length))
  const next = list.slice()

  next.splice(clamped, 0, item)

  return next
}

function setPinned(storedSessionId: string, pinId: string, pinned: boolean): void {
  const current = $pinnedSessionIds.get()
  const isPinned = current.includes(storedSessionId) || current.includes(pinId)

  if (pinned === isPinned) {
    return
  }

  $pinnedSessionIds.set(
    pinned ? [...current, pinId] : current.filter(id => id !== storedSessionId && id !== pinId)
  )
}

/** True while `storedSessionId` has a live undo window. Useful for the UI to
 *  decide whether to show an archive icon vs. an already-pending state. */
export function isArchiveUndoPending(storedSessionId: string): boolean {
  return storedSessionId in $pendingArchiveUndos.get()
}

/** Archive a session immediately — no confirmation — and open a 10s window
 *  during which `undoArchive` can fully reverse it. Independent per session:
 *  archiving several sessions in quick succession gives each its own timer
 *  and pending entry, and undoing one never touches another's.
 *
 *  Resolves once the backend mutation lands. On rejection, the optimistic
 *  removal is rolled back and the pending entry is dropped (there is nothing
 *  to undo — the archive never actually happened), then the error is
 *  rethrown so the caller can surface it. */
export async function archiveSessionWithUndo(storedSessionId: string): Promise<void> {
  const sessions = $sessions.get()
  const index = sessions.findIndex(session => sessionMatchesStoredId(session, storedSessionId))
  const session = index === -1 ? undefined : sessions[index]

  if (!session) {
    return
  }

  const pinId = sessionPinId(session)
  const wasPinned = $pinnedSessionIds.get().includes(storedSessionId) || $pinnedSessionIds.get().includes(pinId)
  const archivedIds = [storedSessionId, session.id, session._lineage_root_id]
  const wasSelected = $selectedStoredSessionId.get() === storedSessionId

  // Instant, dialog-free removal from the active list.
  setSessions(prev => prev.filter(s => !sessionMatchesStoredId(s, storedSessionId)))
  tombstoneSessions(archivedIds)
  setPinned(storedSessionId, pinId, false)

  // Sane default for "archiving the open session": deselect rather than
  // leave the primary view pointed at a row that just vanished from the
  // list. We don't drive navigation from here (that's a UI/routing concern
  // for the wiring layer) — just clear the stale selection.
  if (wasSelected) {
    setSelectedStoredSessionId(null)
  }

  clearPendingTimer(storedSessionId)

  const now = Date.now()

  const entry: PendingArchiveUndo = {
    archivedAt: now,
    expiresAt: now + ARCHIVE_UNDO_WINDOW_MS,
    index,
    session,
    storedSessionId,
    wasPinned
  }

  $pendingArchiveUndos.set({ ...$pendingArchiveUndos.get(), [storedSessionId]: entry })

  timers.set(
    storedSessionId,
    setTimeout(() => {
      // Window closed: the archive is now permanent, just forget the entry.
      // No leaked timer — this callback is the timer's only job.
      dropPendingEntry(storedSessionId)
    }, ARCHIVE_UNDO_WINDOW_MS)
  )

  try {
    await setSessionArchived(storedSessionId, true, session.profile)
  } catch (err) {
    // The mutation never took — there is nothing pending to undo.
    dropPendingEntry(storedSessionId)
    untombstoneSessions(archivedIds)
    setPinned(storedSessionId, pinId, wasPinned)

    if (wasSelected) {
      setSelectedStoredSessionId(storedSessionId)
    }

    setSessions(prev =>
      prev.some(s => sessionMatchesStoredId(s, storedSessionId)) ? prev : insertAt(prev, index, session)
    )

    throw err
  }
}

/** Undo an archive within its 10s window: restores the row to its recorded
 *  list position and pin state, and reverses the backend flag. Safe to call
 *  blind — a call after the window has expired, for an id that was never
 *  archived through this path, or a second call for one already undone, is
 *  a no-op (never throws, never double-restores). Two pending archives are
 *  independent: undoing one only ever reads/clears that id's own entry. */
export async function undoArchive(storedSessionId: string): Promise<void> {
  const entry = $pendingArchiveUndos.get()[storedSessionId]

  if (!entry) {
    return
  }

  // Drop the entry (and its timer) up front so a second concurrent call —
  // or the timer firing while this await is in flight — sees nothing left
  // to act on.
  dropPendingEntry(storedSessionId)

  const archivedIds = [entry.storedSessionId, entry.session.id, entry.session._lineage_root_id]
  const pinId = sessionPinId(entry.session)

  untombstoneSessions(archivedIds)
  setSessions(prev =>
    prev.some(s => sessionMatchesStoredId(s, storedSessionId)) ? prev : insertAt(prev, entry.index, entry.session)
  )
  setPinned(storedSessionId, pinId, entry.wasPinned)

  try {
    await setSessionArchived(storedSessionId, false, entry.session.profile)
  } catch (err) {
    // Backend still thinks it's archived — roll the optimistic restore back
    // rather than leave the UI showing a session the server disagrees on.
    setSessions(prev => prev.filter(s => !sessionMatchesStoredId(s, storedSessionId)))
    tombstoneSessions(archivedIds)
    setPinned(storedSessionId, pinId, false)
    throw err
  }
}

/** Test/reset hook: clears every pending entry and its timer without
 *  touching `$sessions`/pins. Also useful as a hard reset on profile/gateway
 *  switch, where in-flight undo windows from the previous context should not
 *  linger (not wired up yet — no caller needs it outside tests today). */
export function resetArchiveUndos(): void {
  for (const timer of timers.values()) {
    clearTimeout(timer)
  }

  timers.clear()
  $pendingArchiveUndos.set({})
}
