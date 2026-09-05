import type { NotificationInput } from '@/store/notifications'
/**
 * The "Session archived — Undo" toast's notification-input shape, factored
 * out of wiring.tsx as its own pure function so it can be exercised directly
 * against the REAL notification stack + archive-undo store in a test,
 * without dragging in the whole React controller. This is the seam that
 * connects the two halves of review t_548d0d33's blocking issue 4: the
 * notification stack's 4-item cap (`store/notifications.ts`) evicting a
 * toast, and the archive-undo store's `commitPendingArchive` (which cancels
 * the timer and drops the pending entry so there is never an invisible
 * orphaned undo window). A test that only calls `notify()` with a generic
 * `onEvict` spy, or only calls `commitPendingArchive` directly, proves each
 * half works but not that they are actually WIRED to each other — a broken
 * `onEvict: () => commitPendingArchive(storedSessionId)` binding here would
 * still leave both of those narrower suites green.
 */
import { ARCHIVE_UNDO_WINDOW_MS, commitPendingArchive, undoArchive } from '@/store/session-archive-undo'

/** Stable id so archiving the SAME session again (e.g. after a failed then
 *  retried archive) replaces its own toast instead of stacking a duplicate. */
export function archiveUndoToastId(storedSessionId: string): string {
  return `archive-undo:${storedSessionId}`
}

export function buildArchiveUndoToastInput(params: {
  message: string
  onUndoFailed: (err: unknown) => void
  storedSessionId: string
  undoLabel: string
}): NotificationInput {
  const { message, onUndoFailed, storedSessionId, undoLabel } = params

  return {
    action: {
      label: undoLabel,
      onClick: () => void undoArchive(storedSessionId).catch(onUndoFailed)
    },
    durationMs: ARCHIVE_UNDO_WINDOW_MS,
    id: archiveUndoToastId(storedSessionId),
    kind: 'success',
    message,
    // If the 4-item notification cap silently drops THIS toast (a 5th
    // archive within the window — #548d0d33, issue 4), there is no longer
    // any UI affordance for undoing it: commit the archive immediately
    // instead of leaving its timer running behind a toast the user can no
    // longer see or click.
    onEvict: () => commitPendingArchive(storedSessionId)
  }
}
