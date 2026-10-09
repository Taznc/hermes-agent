/**
 * All Boards: one merged view of every live board, served by the fork's
 * backend plugin (`GET /api/plugins/fork-kanban/board/all`). Selecting it sets
 * upstream's `$boardSlug` to the `ALL_BOARDS` sentinel; upstream's kanban then
 * reaches this module through ONE anchored call in its `api.ts` REST funnel
 * (`call` → `routeCall`):
 *
 *  - `GET /board` is served by the merged payload;
 *  - every per-task call (drawer, move, delete, comments) is pinned to the
 *    board that card actually lives on, since `?board=*` means nothing to
 *    upstream's routes;
 *  - `POST /tasks/bulk` fans out once per board.
 *
 * Upstream's events socket only follows ONE board, so while All Boards is on,
 * `watchAllBoards` opens one socket per board and refreshes the merged view
 * on any event.
 */

import type { QueryClient } from '@tanstack/react-query'
import type { WritableAtom } from 'nanostores'

import { pluginRest, type PluginRestOptions, pluginSocket } from '@/api/plugins'
import type { KanbanBoard, KanbanTask } from '@/fork/kanban/types'
import { queryClient } from '@/lib/query-client'
import { $activeConnectionId } from '@/store/connections'

/** The plugin's REST door (`ctx.rest`) — namespace-scoped to `kanban`. */
export type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>

export const ALL_BOARDS = '*'

export const FORK_PLUGIN_ID = 'fork-kanban'

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

/** Per-board event cursors from the latest merged payload (socket resume). */
let lastCursors: Record<string, number> = {}

function indexTasks(payload: AllBoardsPayload): void {
  boardOfTask.clear()
  lastCursors = { ...(payload.cursors ?? {}) }
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
    console.warn('[fork-kanban] task ids shared across boards; actions route to the first board:', [...clashes])
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

const TASK_PATH = /^\/tasks\/([^/?]+)/

/** The board slug a request should carry. Outside All Boards it is the
 *  selection, unchanged. Inside it, a per-task path goes to that task's
 *  board; anything else ('' → the server's current board) — creating a task,
 *  nudging the dispatcher. */
export function boardFor(path: string, slug: string): string {
  if (slug !== ALL_BOARDS) {
    return slug
  }

  const id = TASK_PATH.exec(path)?.[1]

  return (id && id !== 'bulk' && boardOfTask.get(decodeURIComponent(id))) || ''
}

type Send<T> = (path: string, opts?: PluginRestOptions) => Promise<T>

const withQuery = (pathname: string, params: URLSearchParams) => {
  const qs = params.toString()

  return qs ? `${pathname}?${qs}` : pathname
}

/** A 404 from the fork backend (plugin not mounted). Over Electron IPC the
 *  status only survives inside the message ("…Error: 404: {…}"). */
const isNotFound = (err: unknown) => {
  const e = err as { message?: unknown; statusCode?: unknown } | null

  return e?.statusCode === 404 || (typeof e?.message === 'string' && /(^|[\s:])404\b/.test(e.message))
}

/** `POST /tasks/bulk` under All Boards: one request per board holding the
 *  selected ids, results merged — upstream's toast logic sees one reply. */
async function bulkAcrossBoards<T>(params: URLSearchParams, send: Send<T>, opts?: PluginRestOptions): Promise<T> {
  const body = (opts?.body ?? {}) as { ids?: string[] } & Record<string, unknown>
  const groups = new Map<string, string[]>()

  for (const id of body.ids ?? []) {
    const board = boardOfTask.get(id) ?? ''
    groups.set(board, [...(groups.get(board) ?? []), id])
  }

  const replies = await Promise.all(
    [...groups].map(([board, ids]) => {
      const scoped = new URLSearchParams(params)

      if (board) {
        scoped.set('board', board)
      } else {
        scoped.delete('board')
      }

      return send(withQuery('/tasks/bulk', scoped), { ...opts, body: { ...body, ids } })
    })
  )

  const results = replies.flatMap(reply => (reply as { results?: unknown[] } | null)?.results ?? [])

  return { results } as T
}

/**
 * The anchor in upstream's REST funnel (`call` in plugins/kanban/api.ts).
 * Always returns the request to make: outside All Boards that is exactly
 * `send(path, opts)`, upstream's own call. `slug` is upstream's selection at
 * call time, read by the caller.
 */
export function routeCall<T>(path: string, slug: string, send: Send<T>, opts?: PluginRestOptions): Promise<T> {
  if (slug !== ALL_BOARDS) {
    return send(path, opts)
  }

  const [pathname, query = ''] = path.split('?', 2) as [string, string?]
  const params = new URLSearchParams(query)

  if (params.get('board') !== ALL_BOARDS) {
    return send(path, opts)
  }

  if (pathname === '/board') {
    params.delete('board')

    // Selection persisted from a session where the fork backend existed:
    // without it, quietly show the server's current board instead of an error.
    return fetchAllBoards<T>(params.get('include_archived') === 'true').catch(err =>
      isNotFound(err) ? send(withQuery(pathname, params), opts) : Promise.reject(err)
    )
  }

  if (pathname === '/tasks/bulk') {
    return bulkAcrossBoards(params, send, opts)
  }

  const board = boardFor(pathname, slug)

  if (board) {
    params.set('board', board)
  } else {
    params.delete('board')
  }

  return send(withQuery(pathname, params), opts)
}

const scope = () => $activeConnectionId.get() ?? 'local'

/** Upstream's selected-board atom, handed over by the `api.ts` anchor at bind
 *  time — the fork's only handle on the selection (plugins can't be imported
 *  from here). Null before the plugin binds. */
let slugAtom: null | WritableAtom<string> = null

export const boardSlugAtom = (): null | WritableAtom<string> => slugAtom

/**
 * The `api.ts` anchor: wrap the plugin's REST door once, at bind time, so
 * EVERY upstream request — the page's queries, the drawer, mutations, the
 * socket binder's snapshot, completion-notify's baseline — goes through
 * `routeCall`. Outside All Boards the wrapper is a straight pass-through.
 */
export function routeRest(r: Rest, slug: WritableAtom<string>): Rest {
  slugAtom = slug

  return <T>(path: string, opts?: PluginRestOptions) => routeCall<T>(path, slug.get(), r as Send<T>, opts)
}

/** Select All Boards (the switcher's item). Upstream's socket binder reacts
 *  to the atom and snapshots `/board?board=*` through the routed door. */
export function enterAllBoards(): void {
  slugAtom?.set(ALL_BOARDS)
}

/** Leave All Boards for the server's current board (fork backend gone). */
export function leaveAllBoards(): void {
  if (slugAtom?.get() === ALL_BOARDS) {
    slugAtom.set('')
  }
}

/**
 * Is the fork's backend plugin mounted on this connection? One cheap GET of
 * the current board's edges; any failure (404 when the plugin is absent, a
 * network error, an old backend) reads as "no". Never retried, never toasted.
 */
export async function probeForkBackend(): Promise<boolean> {
  try {
    await pluginRest<unknown>(FORK_PLUGIN_ID, '/link-edges')

    return true
  } catch {
    return false
  }
}

/** Dependency edges for one board (`''` = the server's current board), or
 *  null when the fork backend can't serve them. Never throws. */
export async function fetchLinkEdges(slug: string): Promise<Array<[string, string]> | null> {
  try {
    const res = await pluginRest<{ edges?: Array<[string, string]> }>(
      FORK_PLUGIN_ID,
      `/link-edges${slug ? `?board=${encodeURIComponent(slug)}` : ''}`
    )

    return Array.isArray(res?.edges) ? res.edges : null
  } catch {
    return null
  }
}

/** One upstream events socket per board while All Boards is on; any event
 *  refreshes the merged view (and the switcher counts). Resumes from the
 *  latest merged payload's cursors. Returns a disposer. */
export function watchAllBoards(client: QueryClient = queryClient): () => void {
  const refresh = (data: unknown) => {
    const events = (data as { events?: unknown[] } | null)?.events

    if (events?.length) {
      void client.invalidateQueries({ queryKey: ['kanban', 'board', scope(), ALL_BOARDS] })
      void client.invalidateQueries({ queryKey: ['kanban', 'boards', scope()] })
    }
  }

  const closers = Object.entries(lastCursors).map(([board, since]) =>
    pluginSocket('kanban', `/events?board=${encodeURIComponent(board)}&since=${since}`, refresh)
  )

  return () => closers.forEach(close => close())
}
