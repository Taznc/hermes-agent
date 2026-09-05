import { atom } from 'nanostores'

import { setSessionArchived } from '@/hermes'
import type { SessionInfo } from '@/types/hermes'

import { $pinnedSessionIds } from './layout'
import { beginSessionMutation, endSessionMutation, tombstoneSessions, untombstoneSessions } from './session-removal'
import { $sessions, sessionMatchesStoredId, sessionPinId, setSessions } from './session'

// ---------------------------------------------------------------------------
// Archive-with-undo bookkeeping.
//
// This module owns ONLY the undo-window bookkeeping: which sessions have a
// live 10s undo, where each one goes back on undo, and serializing a
// session's undo write behind its own in-flight archive write. It does NOT
// own archive semantics — those live in the ONE canonical `archiveSession`
// action (app/session/hooks/use-session-actions/index.ts), which wraps
// itself with `{ silent: true }` (as `archiveSessionWithUndo`) and calls
// into `registerPendingArchiveUndo` below. Mutation fencing, unread cleanup,
// tile closure, and runtime cleanup all still happen there exactly as they
// do for every other archive path — this module never re-implements them
// (previously it did, which is why review t_548d0d33 rejected the feature).
// ---------------------------------------------------------------------------

export const ARCHIVE_UNDO_WINDOW_MS = 10_000

export interface PendingArchiveUndo {
  /** The id the caller archived with (may be a lineage tip). */
  storedSessionId: string
  /** Full row snapshot as it looked immediately before archiving. */
  session: SessionInfo
  /** Durable (pin) id of the neighbor that should sit immediately BEFORE
   *  this row once restored, or null when it was first / unknown. Recorded
   *  instead of an absolute list index so two overlapping undo windows
   *  restore in the correct RELATIVE order no matter which one is undone
   *  first — an absolute index goes stale the moment a second archive
   *  happens (review t_548d0d33, blocking issue 1). */
  prevPinId: null | string
  /** Durable (pin) id of the neighbor that should sit immediately AFTER
   *  this row once restored, or null when it was last / unknown. Used when
   *  `prevPinId` is not (yet) back in the list — e.g. undoing the earlier of
   *  two concurrent archives while the later one is still pending. */
  nextPinId: null | string
  wasPinned: boolean
  archivedAt: number
  expiresAt: number
}

/** Pending undo entries, keyed by the id passed to `registerPendingArchiveUndo`.
 *  Observable so the UI (toast/snackbar) can render a stack of undoable
 *  archives without polling. */
export const $pendingArchiveUndos = atom<Record<string, PendingArchiveUndo>>({})

const timers = new Map<string, ReturnType<typeof setTimeout>>()
// The archive PATCH for a session, tracked only while it may still be in
// flight. `undoArchive` awaits this before issuing its own inverse PATCH so
// the two writes can never apply out of order (review t_548d0d33, blocking
// issue 3). Stored WITHOUT swallowing its rejection: `undoArchive` needs to
// see whether the archive itself failed so it can abort a queued undo
// instead of proceeding as if the archive had succeeded (review t_548d0d33
// round 2 — a swallowed rejection here let a queued Undo fire its inverse
// PATCH, and roll back the canonical archive's own rollback, after an
// archive that never actually landed). The promise is safe to store
// unhandled: the caller (`archiveSession`) already `await`s this exact same
// promise inside its own try/catch, so a rejection is always handled there
// even when nothing here ever reads it (e.g. the undo window simply
// expires).
const pendingWrites = new Map<string, Promise<unknown>>()

function clearPendingTimer(storedSessionId: string): void {
  const timer = timers.get(storedSessionId)

  if (timer !== undefined) {
    clearTimeout(timer)
    timers.delete(storedSessionId)
  }
}

/** Drops a pending entry (and its timer/write tracking) without touching
 *  `$sessions`/pins — the archive itself already landed, so "committing" it
 *  here just means forgetting the undo window, not another backend write.
 *  Used by: the window's own timer on natural expiry, an archive that turned
 *  out to have failed (nothing to undo), AND a notification-stack eviction
 *  (`store/notifications.ts`'s 4-item cap), which must commit immediately
 *  rather than leave an invisible undo timer running behind a toast the user
 *  can no longer see (review t_548d0d33, blocking issue 4). Safe to call for
 *  an id with no pending entry. */
export function commitPendingArchive(storedSessionId: string): void {
  clearPendingTimer(storedSessionId)
  pendingWrites.delete(storedSessionId)
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

/** Where every row this module currently knows about sits — the live
 *  `$sessions` list with any OTHER pending archive re-threaded back into its
 *  own recorded position. Reconstructing in `archivedAt` order lets each
 *  entry's neighbors resolve against an already-correct partial
 *  reconstruction, so overlapping archives compose correctly no matter how
 *  many are in flight at once. */
function reconstructedOrder(): SessionInfo[] {
  const pending = Object.values($pendingArchiveUndos.get()).sort((a, b) => a.archivedAt - b.archivedAt)
  let order: SessionInfo[] = $sessions.get().slice()

  for (const entry of pending) {
    if (order.some(s => sessionMatchesStoredId(s, entry.storedSessionId))) {
      continue
    }

    order = insertAt(order, resolveNeighborIndex(order, entry.prevPinId, entry.nextPinId), entry.session)
  }

  return order
}

/** Resolves an insertion index from recorded neighbor ids against a LIVE
 *  list — tries the preceding neighbor first (insert right after it), then
 *  the following neighbor (insert right before it), and falls back to the
 *  end when neither is present (both already gone, e.g. deleted). */
function resolveNeighborIndex(order: readonly SessionInfo[], prevPinId: null | string, nextPinId: null | string): number {
  if (prevPinId) {
    const idx = order.findIndex(s => sessionPinId(s) === prevPinId)

    if (idx !== -1) {
      return idx + 1
    }
  }

  if (nextPinId) {
    const idx = order.findIndex(s => sessionPinId(s) === nextPinId)

    if (idx !== -1) {
      return idx
    }
  }

  return order.length
}

/** Captures `storedSessionId`'s neighbors from the full reconstructed order
 *  (live sessions + any already-pending archives) — call this BEFORE the
 *  optimistic removal, while the row is still present to locate. */
export function captureArchiveNeighbors(storedSessionId: string): { nextPinId: null | string; prevPinId: null | string } {
  const order = reconstructedOrder()
  const idx = order.findIndex(s => sessionMatchesStoredId(s, storedSessionId))

  if (idx === -1) {
    return { nextPinId: null, prevPinId: null }
  }

  return {
    nextPinId: idx < order.length - 1 ? sessionPinId(order[idx + 1]) : null,
    prevPinId: idx > 0 ? sessionPinId(order[idx - 1]) : null
  }
}

/** True while `storedSessionId` has a live undo window. Useful for the UI to
 *  decide whether to show an archive icon vs. an already-pending state. */
export function isArchiveUndoPending(storedSessionId: string): boolean {
  return storedSessionId in $pendingArchiveUndos.get()
}

/** Opens a 10s undo window for a session the CALLER has already archived
 *  through the canonical archive action — this never archives anything
 *  itself. Call synchronously right after the optimistic removal (so a
 *  second concurrent archive's `captureArchiveNeighbors` sees this one),
 *  passing the in-flight archive write so `undoArchive` can serialize behind
 *  it instead of racing it. */
export function registerPendingArchiveUndo(params: {
  nextPinId: null | string
  prevPinId: null | string
  session: SessionInfo
  storedSessionId: string
  wasPinned: boolean
  writePromise: Promise<unknown>
}): void {
  const { nextPinId, prevPinId, session, storedSessionId, wasPinned, writePromise } = params

  clearPendingTimer(storedSessionId)

  const now = Date.now()

  const entry: PendingArchiveUndo = {
    archivedAt: now,
    expiresAt: now + ARCHIVE_UNDO_WINDOW_MS,
    nextPinId,
    prevPinId,
    session,
    storedSessionId,
    wasPinned
  }

  $pendingArchiveUndos.set({ ...$pendingArchiveUndos.get(), [storedSessionId]: entry })
  pendingWrites.set(storedSessionId, writePromise)

  timers.set(
    storedSessionId,
    setTimeout(() => {
      // Window closed: the archive is now permanent, just forget the entry.
      // No leaked timer — this callback is the timer's only job.
      commitPendingArchive(storedSessionId)
    }, ARCHIVE_UNDO_WINDOW_MS)
  )
}

/** Drops a pending entry without restoring anything — used when the archive
 *  it was tracking turned out to have failed (there is nothing to undo). */
export function discardPendingArchiveUndo(storedSessionId: string): void {
  commitPendingArchive(storedSessionId)
}

/** Undo an archive within its 10s window: restores the row to the position
 *  implied by its recorded neighbors (order-independent — see
 *  `PendingArchiveUndo.prevPinId`/`nextPinId`) and pin state, and reverses
 *  the backend flag. Waits for the archive's own write to settle first, so
 *  the two PATCHes can never apply out of order — and if that write itself
 *  rejected, this is a no-op (nothing to undo; the canonical archive action
 *  already rolled its own optimistic removal back, and this must NOT issue
 *  the inverse PATCH or disturb that rollback — review t_548d0d33 round 2).
 *  Safe to call blind — a call after the window has expired (checked
 *  against `expiresAt` at invocation time, not just trusting
 *  timer-callback ordering), for an id that was never archived through
 *  this path, or a second call for one already undone, is a no-op (never
 *  throws, never double-restores). Two pending archives are independent:
 *  undoing one only ever reads/clears that id's own entry. */
export async function undoArchive(storedSessionId: string): Promise<void> {
  const entry = $pendingArchiveUndos.get()[storedSessionId]

  if (!entry || Date.now() > entry.expiresAt) {
    return
  }

  // Drop the entry (and its timer) up front so a second concurrent call —
  // or the timer firing while this await is in flight — sees nothing left
  // to act on.
  const write = pendingWrites.get(storedSessionId) ?? Promise.resolve()
  commitPendingArchive(storedSessionId)

  // Queue behind the archive PATCH: it may still be in flight, and undoing
  // before it lands risks the server applying the two writes out of order.
  try {
    await write
  } catch {
    // The archive write itself failed — there is nothing to undo. The
    // canonical `archiveSession` catch block (use-session-actions/index.ts)
    // already restored the row/pin and untombstoned it, and rethrows for
    // its OWN caller to surface the failure. Proceeding past this point
    // would optimistically "restore" an already-restored row and then fire
    // an inverse PATCH to unarchive a session the backend never archived —
    // and if THAT inverse PATCH also failed, the old rollback would
    // re-remove/re-tombstone the row the canonical catch just put back,
    // leaving the UI archived while the backend was never archived at all
    // (review t_548d0d33 round 2, the deferred-archive-rejection case).
    // Stop here; the archive failure is already being surfaced elsewhere.
    return
  }

  const archivedIds = [entry.storedSessionId, entry.session.id, entry.session._lineage_root_id]
  const pinId = sessionPinId(entry.session)

  beginSessionMutation(archivedIds)
  untombstoneSessions(archivedIds)
  setSessions(prev =>
    prev.some(s => sessionMatchesStoredId(s, storedSessionId))
      ? prev
      : insertAt(prev, resolveNeighborIndex(prev, entry.prevPinId, entry.nextPinId), entry.session)
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
  } finally {
    endSessionMutation(archivedIds)
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
  pendingWrites.clear()
  $pendingArchiveUndos.set({})
}
