/**
 * All Boards: one merged view of every live board, served by the fork's
 * backend plugin (`GET /api/plugins/kanban-fork/board/all`). Selecting it sets
 * upstream's `$boardSlug` to the `ALL_BOARDS` sentinel; upstream's kanban then
 * reaches this module through two anchored calls in its `api.ts`:
 *
 *  - `fetchBoard` → `fetchAllBoards` serves the merged payload.
 *  - `withBoard` → `boardFor` pins every per-task call (drawer, move, delete,
 *    comments) to the board that card actually lives on, since `?board=*` means
 *    nothing to upstream's routes.
 *
 * Upstream's events socket only follows ONE board, so while All Boards is on,
 * `useAllBoardsLive` opens one socket per board and refreshes the merged view
 * on any event.
 */

import { pluginRest, pluginSocket } from '@/api/plugins'
import { queryClient } from '@/lib/query-client'
import { $activeConnectionId } from '@/store/connections'

import type { KanbanBoard, KanbanTask } from './types'

export const ALL_BOARDS = '*'

export const FORK_PLUGIN_ID = 'kanban-fork'

export interface AllBoardsInfo {
  color?: string
  name: string
  slug: string
  task_count: number
}

export interface AllBoardsPayload extends KanbanBoard {
  boards?: AllBoardsInfo[]
  cursors?: Record<string, number>
  errors?: Array<{ board: string; detail: string }>
}

/** task id → the board it lives on, from the latest merged payload. Ids are
 *  random per board; a clash across boards is astronomically unlikely, and
 *  on one the FIRST board wins (the ambiguity is logged, never silent). */
const boardOfTask = new Map<string, string>()

function indexTasks(payload: AllBoardsPayload): void {
  boardOfTask.clear()
  const clashes = new Set<string>()

  for (const column of payload.columns ?? []) {
    for (const task of column.tasks as KanbanTask[]) {
      if (!task.board) {
        continue
      }

      if (boardOfTask.has(task.id) && boardOfTask.get(task.id) !== task.board) {
        clashes.add(task.id)

        continue
      }

      boardOfTask.set(task.id, task.board)
    }
  }

  if (clashes.size > 0) {
    console.warn('[kanban-fork] task ids shared across boards; actions route to the first board:', [...clashes])
  }
}

export async function fetchAllBoards<T>(archived: boolean): Promise<T> {
  const payload = await pluginRest<AllBoardsPayload>(
    FORK_PLUGIN_ID,
    `/board/all${archived ? '?include_archived=true' : ''}`
  )

  indexTasks(payload)

  return payload as unknown as T
}

/** Anchor for upstream's `fetchBoard`: the merged payload while All Boards is
 *  selected, else `null` so upstream's own fetch runs untouched. */
export function fetchBoardFor<T>(slug: string, archived: boolean): Promise<T> | null {
  return slug === ALL_BOARDS ? fetchAllBoards<T>(archived) : null
}

const TASK_PATH = /^\/tasks\/([^/?]+)/

/** Anchor for upstream's `withBoard`: the board slug a request should carry.
 *  Outside All Boards it is the selection, unchanged. Inside it, a per-task
 *  path goes to that task's board; anything else ('' → the server's current
 *  board) — creating a task, nudging the dispatcher. */
export function boardFor(path: string, slug: string): string {
  if (slug !== ALL_BOARDS) {
    return slug
  }

  const id = TASK_PATH.exec(path)?.[1]

  return (id && id !== 'bulk' && boardOfTask.get(decodeURIComponent(id))) || ''
}

const scope = () => $activeConnectionId.get() ?? 'local'

/** Upstream's board query key (see `boardKey` in plugins/kanban/api.ts). */
const allBoardsKey = (archived: boolean) => ['kanban', 'board', scope(), ALL_BOARDS, archived] as const

/**
 * Switch the selection to All Boards, seeding the merged payload first.
 * Upstream's socket binder snapshots the new selection with its own
 * single-board fetch, which 400s on the sentinel; with data already cached the
 * board keeps rendering through that one failed snapshot instead of flashing
 * an error. A failed seed still switches; the page's own query retries.
 */
export async function enterAllBoards(slug: { set: (value: string) => void }): Promise<void> {
  await queryClient
    .fetchQuery({ queryFn: () => fetchAllBoards<AllBoardsPayload>(false), queryKey: allBoardsKey(false) })
    .catch(() => undefined)
  slug.set(ALL_BOARDS)
}

/** One upstream events socket per board while All Boards is on; any event
 *  refreshes the merged view (and the switcher counts). Returns a disposer. */
export function watchAllBoards(cursors: Record<string, number>): () => void {
  const refresh = (data: unknown) => {
    const events = (data as { events?: unknown[] } | null)?.events

    if (events?.length) {
      void queryClient.invalidateQueries({ queryKey: ['kanban', 'board', scope(), ALL_BOARDS] })
      void queryClient.invalidateQueries({ queryKey: ['kanban', 'boards', scope()] })
    }
  }

  const closers = Object.entries(cursors).map(([board, since]) =>
    pluginSocket('kanban', `/events?board=${encodeURIComponent(board)}&since=${since}`, refresh)
  )

  return () => closers.forEach(close => close())
}
