/** Board-scoped completed-card cleanup: the header's "Archive Done" button
 *  plus its confirmation. The backend remains authoritative for the
 *  candidate set — the preflight only enables the affordance and gives the
 *  confirmation its honest count, while the mutation re-checks `done` per
 *  card. */

import { Button, Codicon, ConfirmDialog, host, useMutation, useQuery, useQueryClient } from '@hermes/plugin-sdk'
import { useState } from 'react'

import { $boardSlug, archiveDone, BOARDS_KEY, fetchArchiveDonePreflight } from './api'
import { useKanban } from './ui'

/** A fetch abort is not an archive failure. The REST layer aborts with no
 * reason, so the browser's DOMException carries the spec text "signal is
 * aborted without reason" — meaningless to a user, and misleading besides: the
 * backend request is still running and will finish. Say that instead. */
function isAbortLike(error: unknown): boolean {
  if (error instanceof DOMException) {
    return error.name === 'AbortError'
  }

  if (error instanceof Error) {
    return error.name === 'AbortError' || /abort/i.test(error.message)
  }

  return false
}

export function ArchiveDoneControl() {
  const k = useKanban()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)

  const { data: preflight } = useQuery({
    queryFn: fetchArchiveDonePreflight,
    queryKey: ['kanban', 'archive-done', $boardSlug.get()]
  })

  const archive = useMutation({
    mutationFn: archiveDone,
    // The confirmation is gone by the time this settles (see onConfirm below),
    // so a failure has to reach the user as a notification or not at all.
    onError: (error: unknown) => {
      host.notify({
        kind: 'error',
        message: isAbortLike(error)
          ? k.archiveDoneBackground
          : k.archiveDoneFailed(error instanceof Error ? error.message : String(error))
      })
    },
    onSuccess: result => {
      // Archive events will also invalidate through the socket, but reconcile
      // immediately rather than waiting for that asynchronous delivery.
      void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      void qc.invalidateQueries({ queryKey: BOARDS_KEY })
      void qc.invalidateQueries({ queryKey: ['kanban', 'archive-done'] })

      if (result.failures.length > 0 || result.skipped_count > 0) {
        host.notify({
          kind: 'warning',
          message: k.archiveDonePartial(result.archived_count, result.failures.length, result.skipped_count)
        })
      } else {
        host.notify({ kind: 'success', message: k.archiveDoneSuccess(result.archived_count) })
      }
    }
  })

  const doneCount = preflight?.done_count ?? 0
  const disabled = !preflight || doneCount === 0 || archive.isPending

  return (
    <>
      <Button aria-label={k.archiveDone} disabled={disabled} onClick={() => setOpen(true)} size="xs" variant="ghost">
        <Codicon name="archive" size="0.8rem" />
        {k.archiveDone}
      </Button>
      <ConfirmDialog
        cancelLabel={k.cancel}
        confirmLabel={k.archiveDone}
        description={k.archiveDoneConfirm(doneCount, preflight?.scope.label ?? '')}
        onClose={() => setOpen(false)}
        // Fire-and-forget on purpose: a bulk archive across every board takes
        // as long as it takes, and holding a modal open (busy, undismissable)
        // for its whole duration is the bug. The mutation's own handlers own
        // the outcome — success reconciles and toasts, failure toasts.
        onConfirm={() => {
          archive.mutate()
        }}
        open={open}
        title={k.archiveDone}
      />
    </>
  )
}
