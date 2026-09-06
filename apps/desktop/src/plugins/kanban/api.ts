/**
 * Kanban data layer. Everything goes through `ctx.rest` — the plugin's own
 * `/api/plugins/kanban/*` FastAPI router (`plugins/kanban/dashboard/plugin_api.py`),
 * reused as-is via the desktop's namespace-scoped REST door. No new backend.
 *
 * Fetching, caching, polling, dedupe, and invalidation are React Query's job
 * (the app's standard, via the SDK). This module owns the query keys, the REST
 * calls, and the selected-board atom — every call passes `?board=<slug>` so the
 * desktop's selection never flips the server-wide current-board pointer.
 */

import {
  atom,
  type PluginOs,
  type PluginRestOptions,
  type PluginStorage,
  type PluginTranslate,
  queryClient
} from '@hermes/plugin-sdk'

// Native completion notification.
import { bindCompletionNotify, type CompletionEvent, onKanbanEventsFrame } from './completion-notify'
import type {
  BoardExportResult,
  BoardImportResult,
  BoardMeta,
  BoardsResponse,
  ChoiceResponse,
  KanbanBoard,
  KanbanProfile,
  KanbanProject,
  KanbanTask,
  KanbanTaskDetail,
  OrchestrationSettings,
  StagedAttachment,
  TaskEstimate,
  WorkerLog
} from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>
type Socket = (path: string, onMessage: (data: unknown) => void) => () => void

let rest: null | Rest = null
let os: null | PluginOs = null

/** Selected board slug ('' = the server's current board). Persisted. */
export const $boardSlug = atom<string>('')

/** Sentinel `$boardSlug` value for the consolidated "All Boards" view — never
 *  a real board slug (board slugs are filesystem-safe identifiers that never
 *  contain `*`), so it can't collide with an on-disk board. Every call site
 *  that reaches the server MUST route around this value explicitly (see
 *  `fetchAllBoards`, `withExplicitBoard`) rather than send it as `?board=*`. */
export const ALL_BOARDS = '*'

/** Whether the "how this board works" intro was dismissed. Persisted. */
export const $introDismissed = atom<boolean>(false)

/** Sub-group the Running lane by assignee (the dashboard's "lanes by
 *  profile"). Persisted. */
export const $lanesByProfile = atom<boolean>(false)

/** Per-lane collapse OVERRIDES (true=collapsed, false=expanded). Absence means
 *  auto: empty lanes collapse to a rail, occupied lanes expand. Persisted. */
export const $collapsedLanes = atom<Record<string, boolean>>({})

/** Board VISIBILITY overrides for the All Boards filter chip row (true =
 *  hidden). Absence means visible — a newly appearing board defaults to
 *  shown. Persisted, same shape/pattern as `$collapsedLanes`. Client-side
 *  only: the server always returns every board's cards, this just filters
 *  what's rendered. */
export const $hiddenBoards = atom<Record<string, boolean>>({})

const BOARD_SLUG_KEY = 'boardSlug'
const INTRO_KEY = 'introDismissed'
const LANES_KEY = 'lanesByProfile'
const COLLAPSED_KEY = 'collapsedLanes'
const HIDDEN_BOARDS_KEY = 'hiddenBoards'

/** One live `task_events` frame → precise cache invalidation: the board, plus
 *  each touched task's detail. The polls (8s board / 4s drawer) stay as the
 *  fallback — the socket just makes the board feel instant. */
function onEventsFrame(slug: string, data: unknown): void {
  const events = (data as { events?: CompletionEvent[] })?.events

  if (!events?.length) {
    return
  }

  void queryClient.invalidateQueries({ queryKey: ['kanban', 'board'] })
  // Any event can change a board's card count — keep the switcher badge honest.
  void queryClient.invalidateQueries({ queryKey: BOARDS_KEY })

  for (const taskId of new Set(events.map(event => event.task_id).filter(Boolean))) {
    void queryClient.invalidateQueries({ queryKey: taskKey(slug, taskId!) })
  }

  // Completion notification (after invalidation so notify failure
  // never interferes with cache invalidation).
  void onKanbanEventsFrame(slug, events).catch(() => undefined)
}

// A persisted, subscribable atom (the structural slice we need — avoids
// importing nanostore's type just to describe one).
interface Persisted<T> {
  get(): T
  set(value: T): void
  listen(cb: (value: T) => void): () => void
}

/** Bind the plugin's doors at register time and return a disposer the host
 *  runs on unload/disable — so nothing (store sync, socket) survives a toggle
 *  or duplicates on re-enable. The events socket is pinned to a board at
 *  handshake, so a board switch closes + reopens it. The All Boards sentinel
 *  has no live-events fan-out yet (a named follow-on card) — the socket
 *  simply stays closed while it's selected; the board still polls. */
export function bindApi(
  r: Rest,
  storage: PluginStorage,
  socket: Socket,
  notifyDoors?: { os?: PluginOs; t?: PluginTranslate }
): () => void {
  rest = r
  os = notifyDoors?.os ?? null
  bindCompletionNotify(r, notifyDoors?.t, notifyDoors?.os)
  const unsubs: Array<() => void> = []

  // Hydrate an atom from storage and keep storage in sync with it.
  const persist = <T>(atom: Persisted<T>, key: string, fallback: T) => {
    atom.set(storage.get(key, fallback))
    unsubs.push(atom.listen(value => storage.set(key, value)))
  }

  persist($boardSlug, BOARD_SLUG_KEY, '')
  persist($introDismissed, INTRO_KEY, false)
  persist($lanesByProfile, LANES_KEY, false)
  persist($collapsedLanes, COLLAPSED_KEY, {})
  persist($hiddenBoards, HIDDEN_BOARDS_KEY, {})

  let close: (() => void) | null = null

  const open = (slug: string) => {
    close?.()
    close =
      slug === ALL_BOARDS
        ? null
        : socket(slug ? `/events?board=${encodeURIComponent(slug)}` : '/events', data => onEventsFrame(slug, data))
  }

  open($boardSlug.get())
  unsubs.push($boardSlug.listen(open))

  return () => {
    unsubs.forEach(unsub => unsub())
    close?.()
    rest = null
    os = null
  }
}

/** The plugin's OS door, for components too deep to be handed `ctx`. Null
 *  before `bindApi` and after unload. */
export const pluginOs = (): null | PluginOs => os

function call<T>(path: string, opts?: PluginRestOptions): Promise<T> {
  return rest ? rest<T>(path, opts) : Promise.reject(new Error('kanban api not ready'))
}

/** Append the selected board (and other params) to a path. Never emits the
 *  All Boards sentinel as a literal `board=*` — the backend has no such
 *  board, so that would 400/404 on every mutation fired while the sentinel is
 *  selected. Falling through to "no board param" resolves server-side to the
 *  active board, which is a safe default for any call site not yet migrated
 *  to pass an explicit board (see `withExplicitBoard`). */
function withBoard(path: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(params)
  const slug = $boardSlug.get()

  if (slug && slug !== ALL_BOARDS) {
    search.set('board', slug)
  }

  const qs = search.toString()

  return qs ? `${path}?${qs}` : path
}

/** Like `withBoard`, but the board comes from the CALLER, never the
 *  `$boardSlug` atom — the explicit-board escape hatch every mutation the
 *  consolidated All Boards view can reach must use, so a write always lands
 *  on the card's own board, never the sentinel. Empty string means "no board
 *  param" (server falls back to its active board), matching `withBoard`. */
function withExplicitBoard(path: string, slug: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(params)

  if (slug) {
    search.set('board', slug)
  }

  const qs = search.toString()

  return qs ? `${path}?${qs}` : path
}

/** Route a board-scoped path: an explicit `board` (even '') pins the request
 *  to that board; `undefined` (the default on every existing call site) keeps
 *  today's behavior of reading `$boardSlug`. This is the seam every mutation
 *  helper below uses so single-board call sites are byte-for-byte unchanged
 *  while all-boards call sites can pass a card's own board explicitly. */
function boardPath(path: string, board: string | undefined, params?: Record<string, string>): string {
  return board === undefined ? withBoard(path, params) : withExplicitBoard(path, board, params)
}

// ── query keys (all board-scoped so switching boards is a clean cache miss) ──

export const boardKey = (slug: string, archived: boolean) => ['kanban', 'board', slug, archived] as const
export const taskKey = (slug: string, id: string) => ['kanban', 'task', slug, id] as const
export const logKey = (slug: string, id: string, tailBytes: number) => ['kanban', 'log', slug, id, tailBytes] as const
export const BOARDS_KEY = ['kanban', 'boards'] as const
export const PROFILES_KEY = ['kanban', 'profiles'] as const
export const PROJECTS_KEY = ['kanban', 'projects'] as const
export const ORCHESTRATION_KEY = ['kanban', 'orchestration'] as const

// ── reads ─────────────────────────────────────────────────────────────────────

export const fetchBoard = (archived: boolean) =>
  call<KanbanBoard>(withBoard('/board', archived ? { include_archived: 'true' } : {}))

/** The consolidated All Boards view — merges every board's cards into the
 *  standard status columns, each task tagged `board`/`board_name`. Deliberately
 *  bypasses `withBoard`/`$boardSlug`: `GET /board/all` has no `board` query
 *  param (it takes `boards=<csv>` to RESTRICT the set, which this always-fetch-
 *  everything call never sends). */
export const fetchAllBoards = (archived: boolean) =>
  call<KanbanBoard>(`/board/all${archived ? '?include_archived=true' : ''}`)

export const fetchTask = (id: string, board?: string) => call<KanbanTaskDetail>(boardPath(`/tasks/${id}`, board))

/** Worker stdout/stderr tail (16 KiB by default; the drawer's "show more"
 *  affordance requests a larger tail instead of leaving truncation
 *  unexplained). */
export const fetchLog = (id: string, tailBytes = 16384, board?: string) =>
  call<WorkerLog>(boardPath(`/tasks/${id}/log`, board, { tail: String(tailBytes) }))

export const fetchBoards = () => call<BoardsResponse>('/boards')

export const fetchProfiles = () => call<{ profiles: KanbanProfile[] }>('/profiles')

/** First-class Hermes projects, for scoping a board's default workspace. */
export const fetchProjects = () => call<{ projects: KanbanProject[] }>('/projects')

export const fetchOrchestration = () => call<OrchestrationSettings>('/orchestration')

// ── writes ────────────────────────────────────────────────────────────────────

// Every board edit nudges the dispatcher (debounced, fire-and-forget) so the
// change takes effect NOW instead of on the next 60s tick — create a ready
// task and the worker spawns immediately, no manual "nudge" ritual. The tick
// is lock-guarded and ~1ms when there's nothing to do, so over-nudging is
// free; failures are non-events (the periodic tick still exists).
let nudgeTimer: null | ReturnType<typeof setTimeout> = null
// The set of explicit boards (plus a `true` marker for "use $boardSlug") that
// have a write pending since the last nudge fired — so a debounced burst of
// all-boards writes across several real boards nudges every one of them,
// never just the last board that happened to settle the timer.
const pendingNudgeBoards = new Set<string | true>()

function autoNudge(board?: string): void {
  pendingNudgeBoards.add(board ?? true)

  if (nudgeTimer != null) {
    clearTimeout(nudgeTimer)
  }

  nudgeTimer = setTimeout(() => {
    nudgeTimer = null
    const boards = [...pendingNudgeBoards]
    pendingNudgeBoards.clear()

    for (const board of boards) {
      nudgeDispatcher(board === true ? undefined : board).catch(() => undefined)
    }
  }, 400)
}

/** Resolve the write, then kick the dispatcher. Rejections pass through. */
function nudged<T>(write: Promise<T>, board?: string): Promise<T> {
  return write.then(value => {
    autoNudge(board)

    return value
  })
}

export const patchTask = (id: string, patch: Record<string, unknown>, board?: string) =>
  nudged(call(boardPath(`/tasks/${id}`, board), { method: 'PATCH', body: patch }), board)

/** `board` pins the new task to a specific board. Required from the
 *  consolidated All Boards view: without it `withBoard` drops the sentinel and
 *  the server silently creates the card on whatever board happens to be
 *  ACTIVE, with nothing in the UI saying which. */
export const createTask = (body: Record<string, unknown>, board?: string) =>
  nudged(
    call<{ task: KanbanTask | null; warning?: string }>(boardPath('/tasks', board), { method: 'POST', body }),
    board
  )

// Deleting can unblock dependants (a gone parent no longer gates), so it
// nudges too.
export const deleteTask = (id: string, board?: string) =>
  nudged(call(boardPath(`/tasks/${id}`, board), { method: 'DELETE' }), board)

/** One patch, many ids — independent per-id application; returns per-id
 *  outcomes so the UI can toast partial failures. `board` pins every id in
 *  ONE call to the same board; a selection spanning multiple boards (only
 *  possible in the All Boards view) must be grouped by board and called once
 *  per group by the caller — the backend endpoint is single-board. */
export const bulkTasks = (ids: string[], patch: Record<string, unknown>, board?: string) =>
  nudged(
    call<{ results: Array<{ id: string; ok: boolean; error?: string }> }>(boardPath('/tasks/bulk', board), {
      method: 'POST',
      body: { ids, ...patch }
    }),
    board
  )

/** `choice`, when present, is the clicked multiple-choice option — see
 *  docs/design/blocked-callout-multiple-choice-spec.md. Optional so every
 *  free-text reply keeps sending exactly the payload it always has. */
export const addComment = (id: string, body: string, choice?: ChoiceResponse, board?: string) =>
  call(boardPath(`/tasks/${id}/comments`, board), {
    method: 'POST',
    body: { author: 'desktop', body, choice: choice ?? null }
  })

export const reassignTask = (id: string, profile: string, board?: string) =>
  nudged(
    call(boardPath(`/tasks/${id}/reassign`, board), { method: 'POST', body: { profile, reclaim_first: true } }),
    board
  )

export const reclaimTask = (id: string, board?: string) =>
  nudged(call(boardPath(`/tasks/${id}/reclaim`, board), { method: 'POST', body: {} }), board)

/** Create a dependency edge: `parentId` BLOCKS `childId`. Nudges, because a
 *  new gate can change what the dispatcher is allowed to spawn. `board`
 *  should be the CHILD's board (the task the drawer is open on) — a link only
 *  makes sense between tasks the backend can see from one board's DB. */
export const linkTasks = (parentId: string, childId: string, board?: string) =>
  nudged(call(boardPath('/links', board), { method: 'POST', body: { parent_id: parentId, child_id: childId } }), board)

/** Cut a dependency edge. Nudges: removing the last gate on a todo task can
 *  promote it to ready immediately. */
export const unlinkTasks = (parentId: string, childId: string, board?: string) =>
  nudged(
    call(boardPath('/links', board, { parent_id: parentId, child_id: childId }), {
      method: 'DELETE'
    }),
    board
  )

export const uploadAttachment = (
  id: string,
  upload: { filename: string; contentType?: string; bytes: ArrayBuffer },
  board?: string
) => call(boardPath(`/tasks/${id}/attachments`, board), { method: 'POST', upload })

/** Fetch an attachment's bytes as a base64 data URL — the desktop plugin
 *  host has no authenticated `<img src>` door of its own (REST goes over
 *  the Electron IPC bridge, JSON only), so rendering a pasted image inline
 *  in the drawer needs the bytes delivered as a data URL rather than a URL
 *  to point an `<img>` at. */
export const fetchAttachmentDataUrl = (id: number | string, board?: string) =>
  call<{ data_url: string; content_type: string; size: number }>(boardPath(`/attachments/${id}/data-url`, board))

/** Upload a pasted image before the task exists (new-task dialog paste flow).
 *  Returns a `token` that travels in `pending_attachment_tokens` on
 *  `createTask` and is promoted into a real attachment server-side. Staged
 *  blobs live in the TARGET board's own staging DB, so `board` must match the
 *  board the task will be created on or the token won't resolve at promotion. */
export const stageAttachment = (
  upload: { filename: string; contentType?: string; bytes: ArrayBuffer },
  board?: string
) => call<{ attachment: StagedAttachment }>(boardPath('/attachments/staged', board), { method: 'POST', upload })

/** Remove a staged (pre-submit) image — used by the remove (×) button and by
 *  best-effort cleanup when the new-task dialog closes without submitting.
 *  `board` must be the board the token was staged against. */
export const deleteStagedAttachment = (token: string, board?: string) =>
  call(boardPath(`/attachments/staged/${encodeURIComponent(token)}`, board), { method: 'DELETE' })

export const createBoard = (slug: string, name: string, projectId?: string) =>
  call<{ board: { slug: string } }>('/boards', {
    method: 'POST',
    body: { slug, name, ...(projectId ? { project_id: projectId } : {}) }
  })

/** Rough auxiliary-model estimate for a task (tokens + complexity). Makes a
 *  model call — gate behind an explicit user action + disclaimer. */
export const estimateTask = (id: string, board?: string) =>
  call<TaskEstimate>(boardPath(`/tasks/${id}/estimate`, board), { method: 'POST', body: {} })

/** Estimate from typed title/body before a task exists (create dialog). */
export const estimateNew = (title: string, body: string) =>
  call<TaskEstimate>('/estimate', { method: 'POST', body: { title, body: body || undefined } })

/** Edit a board's display metadata + default project directory. Pass
 *  `default_workdir: ''` to clear it. Slug is immutable. */
export const updateBoard = (slug: string, patch: Record<string, unknown>) =>
  call<{ board: BoardMeta }>(`/boards/${encodeURIComponent(slug)}`, { method: 'PATCH', body: patch })

/** Archive a board to `boards/_archived/` — recoverable, and the backend
 *  refuses to touch `default`. (`?delete=true` hard-deletes; no caller yet.) */
export const deleteBoard = (slug: string) =>
  call<{ result: { action: string; new_path: string }; current: string }>(`/boards/${encodeURIComponent(slug)}`, {
    method: 'DELETE'
  })

// Board transfer exchanges filesystem paths, not bytes — the picker runs on
// the machine hosting the backend, so the backend reads and writes the file.

export const exportBoard = (slug: string, output: string) =>
  call<BoardExportResult>(`/boards/${encodeURIComponent(slug)}/export`, { method: 'POST', body: { output } })

export const importBoard = (archive: string) =>
  call<BoardImportResult>('/boards/import', { method: 'POST', body: { archive } })

export const nudgeDispatcher = (board?: string) =>
  call<{ spawned?: unknown[] }>(boardPath('/dispatch', board), { method: 'POST', body: {} })

/** Append a free-typed idea to the board's roadmap `## Ideas` inbox
 *  (Phase 2.15). Never rejects on a roadmap-unavailable outcome — the
 *  backend is fail-open by contract — so callers branch on `ok`/`reason`
 *  rather than a thrown error, matching `estimateNew`'s shape. */
export const addRoadmapIdea = (text: string, sourceId?: string, board?: string) =>
  call<{ ok: boolean; reason?: null | string }>(boardPath('/roadmap/idea', board), {
    method: 'POST',
    body: { text, ...(sourceId ? { source_id: sourceId } : {}) }
  })

export const saveOrchestration = (patch: Record<string, unknown>) =>
  call<OrchestrationSettings>('/orchestration', { method: 'PUT', body: patch })

export const saveProfileDescription = (name: string, description: string) =>
  call(`/profiles/${encodeURIComponent(name)}`, { method: 'PATCH', body: { description } })

export const autoDescribeProfile = (name: string) =>
  call<{ ok: boolean; reason?: null | string; description?: null | string }>(
    `/profiles/${encodeURIComponent(name)}/describe-auto`,
    { method: 'POST', body: { overwrite: true } }
  )
