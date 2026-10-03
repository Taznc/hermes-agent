/**
 * Dispatch pause/resume for one board or every board (dev 95bea51183 parity),
 * served by the `fork-kanban` backend's `/dispatch/*` routes (T0) over the
 * fork's T1 `hermes_fork.kanban.dispatch_pause`.
 *
 *  - `DispatchControl`: the orchestration panel's control, mounted by one
 *    anchor in upstream's `orchestration.tsx` (`host.fork.kanban.dispatchControl`).
 *    Single board: pause/resume that board, with the drain count. All Boards:
 *    pause/resume every board at once, with "N of M boards paused".
 *  - `DispatchPausedNotice`: a one-line banner above the lanes, rendered by the
 *    existing `boardOverlay` anchor, so a paused board never looks merely idle
 *    even with the settings panel closed.
 *
 * Both read one shared query. Upstream's global emergency stop (`hermes pause`)
 * is shown read-only: it halts every board whatever the per-board switch says.
 * Without the fork backend both render nothing.
 */

import { useStore as useValue } from '@nanostores/react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { atom } from 'nanostores'

import { pluginRest } from '@/api/plugins'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ALL_BOARDS, boardSlugAtom, FORK_PLUGIN_ID } from '@/fork/kanban/all-boards'
import { useForkBackend } from '@/fork/kanban/backend'
import { type KanbanText, useKanban } from '@/fork/kanban/text'
import { $activeConnectionId } from '@/store/connections'
import { notify, notifyError } from '@/store/notifications'

export interface DispatchBoardStatus {
  board: string
  name: string
  paused: boolean
  running_count: number
  state: null | { note?: string; paused_at?: number; paused_by?: string; reason?: string }
}

export interface DispatchStatus {
  board_count: number
  boards: DispatchBoardStatus[]
  errors: Array<{ board: string; detail: string }>
  estop: null | { engaged_at?: null | string; reason?: null | string }
  paused: boolean
  paused_count: number
  /** `null` when any targeted board could not be read: the drain count is unknown. */
  running_count: null | number
  scope: 'all' | 'board'
}

interface PauseReply {
  busy: string[]
  failures: Array<{ board: string; detail: string }>
  paused: boolean
}

interface ResumeReply {
  failures: Array<{ board: string; detail: string }>
  resumed: boolean
}

const POLL_MS = 8_000
const NO_SLUG = atom('')

/** `?board=` for a selection: `''` (server's current board) sends none. */
export const dispatchPath = (route: string, slug: string) =>
  slug ? `/dispatch/${route}?board=${encodeURIComponent(slug)}` : `/dispatch/${route}`

export const dispatchKey = (scope: string, slug: string) => ['fork-kanban', 'dispatch', scope, slug] as const

const fetchStatus = (slug: string) => pluginRest<DispatchStatus>(FORK_PLUGIN_ID, dispatchPath('status', slug))

/** Upstream's selected board, reactive; `''` before the plugin binds. */
function useSlug(): string {
  return useValue(boardSlugAtom() ?? NO_SLUG)
}

function useDispatchStatus(slug: string, enabled: boolean) {
  const scope = useValue($activeConnectionId) ?? 'local'

  return useQuery({
    enabled,
    queryFn: () => fetchStatus(slug),
    queryKey: dispatchKey(scope, slug),
    refetchInterval: POLL_MS,
    retry: false
  })
}

const failedBoards = (failures: Array<{ board: string }>) => failures.map(f => f.board).join(', ')

/**
 * The drain line. Restart clearance ("safe to restart") needs a complete read
 * (no unreadable board, a known count), every targeted board paused, and 0
 * running. A partial pause shows the count only: an unpaused board can start
 * work at any moment. An incomplete read names the boards and clears nothing.
 * `stale`: the latest refresh failed and `status` is React Query's cached last
 * success, so nothing about the drain is known now and nothing is cleared.
 */
export function drainText(k: KanbanText, status: DispatchStatus, stale = false): string {
  const running = status.running_count

  if (stale) {
    return k.statusStale
  }

  if (status.errors.length || running === null) {
    return k.statusUnknown(failedBoards(status.errors))
  }

  if (!status.paused) {
    return k.runningCount(running)
  }

  return running === 0 ? k.safeToRestart : k.draining(running)
}

export function DispatchControl() {
  const k = useKanban()
  const qc = useQueryClient()
  const backend = useForkBackend()
  const slug = useSlug()
  const isAll = slug === ALL_BOARDS
  const { data: status, error } = useDispatchStatus(slug, backend === true)
  const refresh = () => void qc.invalidateQueries({ queryKey: ['fork-kanban', 'dispatch'] })

  const pause = useMutation({
    mutationFn: () => pluginRest<PauseReply>(FORK_PLUGIN_ID, dispatchPath('pause', slug), { body: {}, method: 'POST' }),
    onError: err => notifyError(err, k.pauseDispatch),
    onSettled: refresh,
    onSuccess: reply => {
      if (reply.busy.length) {
        notify({ kind: 'warning', message: k.pauseBusy })
      }

      if (reply.failures.length) {
        notify({ kind: 'error', message: `${k.pauseDispatch}: ${failedBoards(reply.failures)}` })
      }
    }
  })

  const resume = useMutation({
    mutationFn: () => pluginRest<ResumeReply>(FORK_PLUGIN_ID, dispatchPath('resume', slug), { body: {}, method: 'POST' }),
    onError: err => notifyError(err, k.resumeDispatch),
    onSettled: refresh,
    onSuccess: reply => {
      if (reply.failures.length) {
        notify({ kind: 'error', message: `${k.resumeDispatch}: ${failedBoards(reply.failures)}` })
      }
    }
  })

  if (backend !== true) {
    return null
  }

  const busy = pause.isPending || resume.isPending
  const label = <span className={FIELD_LABEL}>{k.dispatchControl}</span>

  if (!status) {
    // Never swallow a failed status read: a missing control looks like a bug
    // in the panel, an error line names the backend.
    return error ? (
      <div className="flex flex-col gap-1.5" data-dispatch-control>
        {label}
        <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{String((error as Error).message ?? error)}</p>
      </div>
    ) : null
  }

  // React Query keeps the last good `data` when a refresh rejects: show it as
  // last-known state, never as a live drain signal.
  const stale = Boolean(error)
  const drain = drainText(k, status, stale)
  const note = !isAll ? status.boards[0]?.state?.note : undefined

  return (
    <div
      className="flex flex-col gap-1.5"
      data-dispatch-control
      data-paused={status.paused ? 'true' : 'false'}
      data-stale={stale ? 'true' : undefined}
    >
      {label}
      <div className="flex flex-wrap items-center gap-2">
        {isAll ? (
          <>
            <Button
              disabled={busy || status.paused || status.board_count === 0}
              onClick={() => pause.mutate()}
              size="xs"
              variant="outline"
            >
              <Codicon name="debug-pause" size="0.8rem" />
              {k.pauseAllBoards}
            </Button>
            <Button disabled={busy || status.paused_count === 0} onClick={() => resume.mutate()} size="xs" variant="outline">
              <Codicon name="debug-start" size="0.8rem" />
              {k.resumeAllBoards}
            </Button>
          </>
        ) : status.paused ? (
          <Button disabled={busy} onClick={() => resume.mutate()} size="xs" variant="outline">
            <Codicon name="debug-start" size="0.8rem" />
            {k.resumeDispatch}
          </Button>
        ) : (
          <Button disabled={busy} onClick={() => pause.mutate()} size="xs" variant="outline">
            <Codicon name="debug-pause" size="0.8rem" />
            {k.pauseDispatch}
          </Button>
        )}
        <span className="text-[0.75rem] text-(--ui-text-secondary)" data-dispatch-summary>
          {isAll
            ? status.paused_count > 0 || status.errors.length || stale
              ? `${k.boardsPaused(status.paused_count, status.board_count)} · ${drain}`
              : k.dispatchRunning
            : status.paused || stale
              ? drain
              : k.dispatchRunning}
        </span>
      </div>
      {stale && <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{String((error as Error).message ?? error)}</p>}
      {status.estop && <EstopLine reason={status.estop.reason ?? ''} />}
      <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{note || k.pauseHint}</p>
    </div>
  )
}

function EstopLine({ reason }: { reason: string }) {
  const k = useKanban()

  return (
    <p className="flex items-center gap-1.5 text-[0.6875rem] text-amber-600 dark:text-amber-400" data-estop>
      <Codicon name="warning" size="0.75rem" />
      {k.estopEngaged(reason)}
    </p>
  )
}

/** Above the lanes (boardOverlay anchor): only when something is paused. */
export function DispatchPausedNotice({ slug }: { slug: string }) {
  const k = useKanban()
  const backend = useForkBackend()
  const { data: status, error } = useDispatchStatus(slug, backend === true)

  if (backend !== true || !status || (status.paused_count === 0 && !status.estop)) {
    return null
  }

  const isAll = slug === ALL_BOARDS
  const drain = drainText(k, status, Boolean(error))

  const text = status.estop
    ? k.estopEngaged(status.estop.reason ?? '')
    : `${isAll ? k.boardsPaused(status.paused_count, status.board_count) : k.dispatchPaused} · ${drain}`

  return (
    <div
      className="mx-4 mb-2 flex shrink-0 items-center gap-1.5 rounded-md bg-amber-500/10 px-2.5 py-1 text-[0.6875rem] text-amber-600 dark:text-amber-400"
      data-dispatch-paused
      role="status"
    >
      <Codicon name="debug-pause" size="0.75rem" />
      {text}
    </div>
  )
}

/** Same look as upstream's kanban FIELD_LABEL (plugins can't be imported here). */
const FIELD_LABEL = 'text-[0.62rem] font-semibold uppercase tracking-[0.14em] text-(--ui-text-quaternary)'

/** The `orchestration.tsx` anchor. */
export const dispatchControl = () => <DispatchControl />
