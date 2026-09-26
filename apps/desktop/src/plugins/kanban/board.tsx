/**
 * The Kanban board page — mounted at `/kanban` (a ROUTES_AREA contribution) in
 * the workspace pane. The desktop port of the dashboard board: one compact
 * header row (count, filter kebab, search, settings, new task — the board
 * SWITCHER lives in the titlebar, see board-switcher.tsx), columns in
 * BOARD_COLUMNS order, drag-to-move (optimistic, workflow-checked),
 * primary-modifier-click multi-select with a floating bulk bar, right-click
 * actions, and the detail drawer. Dispatch nudges ride every write (see api.ts).
 */

import {
  Button,
  cn,
  Codicon,
  ConfirmDialog,
  Contribute,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  host,
  Loader,
  SearchField,
  Tip,
  TITLEBAR_AREAS,
  useGrabScroll,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useRef, useState } from 'react'

import { FocusAnswerBar } from './answer-bar'
import {
  $boardSlug,
  $collapsedLanes,
  $hiddenBoards,
  $introDismissed,
  $lanesByProfile,
  $roadmapHidden,
  ALL_BOARDS,
  boardKey,
  deleteTask,
  fetchAllBoards,
  fetchBoard,
  patchTask,
  primeAllBoardsSocket
} from './api'
import { ArchiveDoneControl } from './archive-done-control'
import { $hotEdge, BoardDependencyArrows, type FocusDepth } from './board-arrows-layer'
import { BoardSwitcher } from './board-switcher'
import { BoardInfoContext, Column, EMPTY_BOARD_INFO, LANE_GAP_ATTR } from './card'
import { DependencyContext, type DependencyView, EMPTY_IDS } from './dependency-view'
import { buildGraph, cardKey, chainSets, focusSets, indexBoard, parseCardKey, taskCardKey } from './deps'
import { TaskDrawer } from './drawer'
import { IdeaCaptureDialog, NewTaskDialog } from './new-task-dialog'
import { OrchestrationPanel } from './orchestration'
import { SelectionBar } from './selection-bar'
import { needsBlockLoopAck } from './status-guidance'
import {
  type BoardAllInfo,
  isRoadmapLane,
  type KanbanBoard,
  type KanbanTask,
  laneDropAllowed,
  orderLanes
} from './types'
import { $newTaskLane, Avatar, columnLabel, errText, isLockedTarget, lockedReason, useKanban } from './ui'

// ── optimistic board edits (reconciled by the follow-up refresh) ─────────────

/** Read a plugin deep link from the hash without taking a router dependency.
 *  Kanban's page is also mounted directly in focused component tests and in
 *  embedders, where a React Router context is deliberately absent. */
function notificationRouteSearch(): string {
  const hash = window.location.hash
  const query = hash.indexOf('?')

  return query === -1 ? '' : hash.slice(query)
}

function moveCard(board: KanbanBoard, key: string, toStatus: string): KanbanBoard {
  let moved: KanbanTask | undefined

  const columns = board.columns.map(col => ({
    ...col,
    tasks: col.tasks.filter(task => {
      if (taskCardKey(task) !== key) {
        return true
      }

      moved = { ...task, status: toStatus }

      return false
    })
  }))

  if (!moved) {
    return board
  }

  return {
    ...board,
    columns: columns.map(col => (col.name === toStatus ? { ...col, tasks: [moved!, ...col.tasks] } : col))
  }
}

function removeCard(board: KanbanBoard, key: string): KanbanBoard {
  return {
    ...board,
    columns: board.columns.map(col => ({ ...col, tasks: col.tasks.filter(t => taskCardKey(t) !== key) }))
  }
}

function setPriorityCard(board: KanbanBoard, key: string, priority: number): KanbanBoard {
  return {
    ...board,
    columns: board.columns.map(col => ({
      ...col,
      tasks: col.tasks.map(task => (taskCardKey(task) === key ? { ...task, priority } : task))
    }))
  }
}

// ── intro ────────────────────────────────────────────────────────────────────

// One-time explainer for the board's core gotcha: this is a dispatcher queue,
// not a todo list. Dismissal persists via plugin storage.
function Intro() {
  const k = useKanban()
  const dismissed = useValue($introDismissed)

  if (dismissed) {
    return null
  }

  return (
    <div
      className="mx-4 mb-2 flex flex-col items-start gap-1.5 rounded-lg bg-(--ui-bg-quinary) px-3 py-2.5 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)"
      data-selectable-text="true"
    >
      <p className="min-w-0">{k.introBody}</p>
      <Button onClick={() => $introDismissed.set(true)} size="inline" variant="textStrong">
        {k.introGotIt}
      </Button>
    </div>
  )
}

const UNASSIGNED_LANE = 'unassigned'

// ── filter kebab ─────────────────────────────────────────────────────────────

function FilterMenu({
  archived,
  assignee,
  board,
  onArchived,
  onAssignee,
  onTenant,
  tenant
}: {
  archived: boolean
  assignee: string
  board: KanbanBoard
  onArchived: (v: boolean) => void
  onAssignee: (v: string) => void
  onTenant: (v: string) => void
  tenant: string
}) {
  const k = useKanban()
  const active = Boolean(assignee || tenant || archived)
  const lanesByProfile = useValue($lanesByProfile)

  const check = (on: boolean) => (on ? <Codicon className="ml-auto" name="check" size="0.8rem" /> : null)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={k.filters}
          className={cn(active && 'bg-(--ui-control-active-background) text-foreground')}
          size="icon-xs"
          variant="ghost"
        >
          <Codicon name="filter" size="0.85rem" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuItem onSelect={() => onAssignee('')}>
          {k.allProfiles}
          {check(!assignee)}
        </DropdownMenuItem>
        {board.assignees.map(name => (
          <DropdownMenuItem key={name} onSelect={() => onAssignee(name)}>
            <Avatar name={name} size="0.875rem" />
            {name}
            {check(assignee === name)}
          </DropdownMenuItem>
        ))}
        {board.tenants.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onTenant('')}>
              {k.allTenants}
              {check(!tenant)}
            </DropdownMenuItem>
            {board.tenants.map(name => (
              <DropdownMenuItem key={name} onSelect={() => onTenant(name)}>
                {name}
                {check(tenant === name)}
              </DropdownMenuItem>
            ))}
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onArchived(!archived)}>
          {k.showArchived}
          {check(archived)}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => $lanesByProfile.set(!lanesByProfile)}>
          {k.groupRunning}
          {check(lanesByProfile)}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// ── All Boards chrome (filter chips + degraded-state notice) ─────────────────

/** Chip row toggling each contributing board on/off client-side (all on by
 *  default). Rendered only in All Boards mode — board-specific affordances
 *  stay confined to this component rather than sprinkled through the header. */
export function BoardFilterChips({
  boards,
  hidden,
  onToggle
}: {
  boards: readonly BoardAllInfo[]
  hidden: Record<string, boolean>
  onToggle: (slug: string) => void
}) {
  const k = useKanban()

  if (boards.length === 0) {
    return null
  }

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1.5 px-4 pb-2">
      {boards.map(info => {
        const isHidden = Boolean(hidden[info.slug])

        return (
          <button
            aria-label={k.toggleBoard(info.name)}
            aria-pressed={!isHidden}
            className={cn(
              'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[0.6875rem] font-medium transition-colors',
              isHidden ? 'border-(--ui-stroke-tertiary) text-(--ui-text-quaternary) opacity-50' : 'border-transparent'
            )}
            key={info.slug}
            onClick={() => onToggle(info.slug)}
            style={
              isHidden
                ? undefined
                : {
                    backgroundColor: `color-mix(in srgb, ${info.color || 'var(--ui-text-tertiary)'} 14%, transparent)`,
                    color: info.color || 'var(--ui-text-secondary)'
                  }
            }
            type="button"
          >
            {info.icon && <Codicon name={info.icon} size="0.7rem" />}
            {info.name}
            <span className="tabular-nums opacity-70">{info.task_count}</span>
          </button>
        )
      })}
    </div>
  )
}

/** Degraded-state banner: a board that failed to load in the consolidated
 *  fetch must not blank the whole view — name it and move on. Renders
 *  nothing when there are no errors, so the caller can mount it
 *  unconditionally in All Boards mode. */
export function BoardsErrorNotice({ errors }: { errors?: Array<{ board: string; detail: string }> }) {
  const k = useKanban()

  if (!errors || errors.length === 0) {
    return null
  }

  return (
    <div className="mx-4 mb-2 flex shrink-0 items-center gap-2 rounded-lg bg-(--ui-bg-quinary) px-3 py-1.5 text-[0.6875rem] text-amber-500">
      <Codicon className="shrink-0" name="warning" size="0.8rem" />
      <span className="min-w-0 truncate">{k.boardsFailedNotice(errors.map(e => e.board).join(', '))}</span>
    </div>
  )
}

// ── page ─────────────────────────────────────────────────────────────────────

export function KanbanBoardPage() {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const [routeSearch, setRouteSearch] = useState(notificationRouteSearch)
  const isAllBoards = slug === ALL_BOARDS
  const [archived, setArchived] = useState(false)

  // Live updates ride the events socket (bindApi) in single-board mode; the
  // consolidated view rides the multi-board `boards=*` socket instead (primed below, once
  // per All-Boards selection, from THIS query's own `cursors` map — no gap, no replay).
  // Either way this interval is the fallback for a dropped/reconnecting socket.
  const { data: board, error } = useQuery({
    queryFn: () => (isAllBoards ? fetchAllBoards(archived) : fetchBoard(archived)),
    queryKey: boardKey(slug, archived),
    refetchInterval: 60_000
  })

  // Prime the multi-board socket from this fetch's cursors the first time All Boards mode
  // loads data — `primeAllBoardsSocket` no-ops on every call after the first (per selection),
  // so this is safe to run on every render/refetch.
  useEffect(() => {
    if (isAllBoards && board?.cursors) {
      primeAllBoardsSocket(board.cursors)
    }
  }, [isAllBoards, board?.cursors])

  // Per-board display chrome for the consolidated view — badge tint/icon and
  // the filter chip row. Empty outside All Boards mode (board?.boards is only
  // ever populated by fetchAllBoards).
  const boardInfoList = board?.boards ?? EMPTY_BOARD_INFO
  const boardInfoMap = useMemo(() => new Map(boardInfoList.map(info => [info.slug, info])), [boardInfoList])

  // Client-side board visibility toggle (all on by default; persisted
  // alongside $collapsedLanes). Boards the payload didn't return (renamed,
  // deleted) fall out naturally since they never render a chip or a card.
  const hiddenBoards = useValue($hiddenBoards)

  // Wishlist-lane visibility for THIS board. Keyed by slug (the '' server-
  // default and the All Boards sentinel are ordinary keys), absent = shown, so
  // a board nobody has touched still shows its full structure.
  const roadmapHiddenMap = useValue($roadmapHidden)
  const roadmapHidden = Boolean(roadmapHiddenMap[slug])

  const toggleRoadmapHidden = () => {
    const next = { ...roadmapHiddenMap }
    const hiding = !next[slug]

    if (next[slug]) {
      delete next[slug]
    } else {
      next[slug] = true
    }

    $roadmapHidden.set(next)

    // Hiding the lanes must not leave an invisible card selected and
    // bulk-actionable — the floating SelectionBar renders purely off
    // `selected.size` and has no idea the cards it would act on just left
    // the visible board. Prune wishlist cards out of the selection at the
    // moment they disappear, the same way the board-membership effect below
    // prunes cards that left entirely.
    if (hiding && board) {
      const laneKeys = new Set(
        board.columns.filter(col => isRoadmapLane(col.name)).flatMap(col => col.tasks.map(taskCardKey))
      )

      if (laneKeys.size > 0) {
        setSelected(prev => {
          const kept = [...prev].filter(key => !laneKeys.has(key))

          return kept.length === prev.size ? prev : new Set(kept)
        })
      }
    }
  }

  const toggleBoardVisible = (slugToToggle: string) => {
    const next = { ...hiddenBoards }

    if (next[slugToToggle]) {
      delete next[slugToToggle]
    } else {
      next[slugToToggle] = true
    }

    $hiddenBoards.set(next)
  }

  // The open drawer's card, as a `cardKey` (board + id in All Boards mode).
  const [openKey, setOpenKey] = useState<null | string>(null)
  const [addStatus, setAddStatus] = useState<null | string>(null)
  // The roadmap card awaiting the "skip auto-decompose?" confirm, as a cardKey.
  const [spawnReadyKey, setSpawnReadyKey] = useState<null | string>(null)
  const [ideaOpen, setIdeaOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [tenant, setTenant] = useState('')
  const [assignee, setAssignee] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  // Dependency-chain focus. Pure per-window presentation — deliberately local
  // state, never a persisted store: a reload should not resurrect a trace the
  // user started three sessions ago.
  const [focused, setFocused] = useState<null | string>(null)
  // One hop by default (see focusSets); 'chain' is the opt-in transitive view.
  const [focusDepth, setFocusDepth] = useState<FocusDepth>('direct')
  // The card the graph overlay is centred on; null = closed.

  useEffect(() => {
    const onHashChange = () => setRouteSearch(notificationRouteSearch())

    window.addEventListener('hashchange', onHashChange)

    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  // Terminal-event notifications link directly to a card. The event belongs
  // to a specific board even if the user has since switched views, so adopt
  // that board before opening the existing detail drawer rather than landing
  // them on a generic Kanban page that makes them hunt for the id.
  useEffect(() => {
    const params = new URLSearchParams(routeSearch)
    const targetBoard = params.get('board')?.trim()
    const targetTask = params.get('task')?.trim()

    if (targetBoard && targetBoard !== ALL_BOARDS && targetBoard !== $boardSlug.get()) {
      $boardSlug.set(targetBoard)
    }

    if (targetTask) {
      setOpenKey(targetTask)
    }
  }, [routeSearch])

  // A new-task request raised from outside the page (⌘⌥N, the palette row).
  // The command navigates here and parks the lane; the page picks it up on
  // arrival — whether it was already mounted or is mounting for the first
  // time — then clears it so a later remount can't reopen the dialog.
  const requestedLane = useValue($newTaskLane)

  useEffect(() => {
    if (requestedLane === null) {
      return
    }

    setAddStatus(requestedLane)
    $newTaskLane.set(null)
  }, [requestedLane])

  const toggleSelect = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev)

      if (!next.delete(id)) {
        next.add(id)
      }

      return next
    })
  }

  // Prune ids that left the board (completed elsewhere, deleted, filtered by
  // a board switch) so the bar's count never lies about what a bulk op hits.
  useEffect(() => {
    if (!board) {
      return
    }

    const alive = new Set(board.columns.flatMap(col => col.tasks.map(taskCardKey)))

    setSelected(prev => {
      const kept = [...prev].filter(id => alive.has(id))

      return kept.length === prev.size ? prev : new Set(kept)
    })
  }, [board])

  // Graph + index built exactly ONCE per board payload, then shared by every
  // card through context. Per-card construction would be O(cards × edges) on
  // every render. Keyed off the raw `board`, not the filtered view: a search
  // filter hides cards but must not rewrite what depends on what.
  const graph = useMemo(() => buildGraph(board), [board])
  const index = useMemo(() => indexBoard(board), [board])
  const hasEdges = Boolean(board?.link_edges && board.link_edges.length > 0)

  // One hop by default (see focusSets) — a transitive closure lights up most
  // of a busy board and defeats the dimming — unless the user asked for it.
  const chain = useMemo(
    () =>
      focused
        ? (focusDepth === 'chain' ? chainSets : focusSets)(graph, focused)
        : { downstream: EMPTY_IDS, upstream: EMPTY_IDS },
    [graph, focused, focusDepth]
  )

  // A focused card that left the board (deleted, archived, filtered away by a
  // board switch) would strand every other card dimmed with nothing lit.
  useEffect(() => {
    if (focused && board && !index.has(focused)) {
      setFocused(null)
    }
  }, [board, focused, index])

  const dependencies = useMemo<DependencyView>(
    () => ({
      downstream: chain.downstream,
      focused,
      graph,
      hasEdges,
      index,
      // Toggle: re-triggering the focused card clears it, so the same
      // affordance both starts and ends a trace. Defined inline because
      // `setFocused` is stable — a handler declared in the component body
      // would be a fresh function every render and rebuild this object (and
      // thus re-render every card) for nothing.
      onFocus: (id: string) => setFocused(prev => (prev === id ? null : id)),
      upstream: chain.upstream
    }),
    [chain, focused, graph, hasEdges, index]
  )

  useEffect(() => {
    if (selected.size === 0) {
      return
    }

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelected(new Set())
      }
    }

    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [selected.size])

  // Esc clears the focus, but only when it is the topmost dismissable thing.
  // The drawer owns Esc while it is open (it has no backdrop to click off, see
  // drawer.tsx), the dialogs own it while *they* are open, and the selection
  // handler above owns it while cards are selected. Gating on all four keeps
  // this listener unregistered in exactly those cases, so nothing races over a
  // single keypress and Esc always dismisses one layer at a time, innermost
  // first. Same shape as the selection handler.
  useEffect(() => {
    if (!focused || openKey || addStatus || selected.size > 0) {
      return
    }

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setFocused(null)
      }
    }

    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [focused, openKey, addStatus, selected.size])

  const columnNames = board?.columns.map(col => col.name) ?? []

  const parentOptions = useMemo(
    () =>
      board?.columns
        .flatMap(col => col.tasks)
        .map(task => ({ board: task.board ?? undefined, id: task.id, title: task.title })) ?? [],
    [board]
  )

  // Client-side filters, mirroring the dashboard (search over title/body/id).
  // In All Boards mode, a hidden-board chip also drops that board's cards —
  // client-side only, since the server always returns every board.
  //
  // Two lane-specific transforms ride along, both applied HERE rather than at
  // render time so everything downstream (the header total, the lane-phase
  // signature, `boardHasWork`) sees one consistent view:
  //  - the wishlist lanes are reordered leftmost, ahead of `triage`, since the
  //    backend appends them to BOARD_COLUMNS instead;
  //  - when hidden for this board they are dropped ENTIRELY (not collapsed to
  //    a rail), so their cards leave the counts with them.
  const filtered = useMemo(() => {
    if (!board) {
      return null
    }

    const q = search.trim().toLowerCase()

    const keep = (task: KanbanTask) =>
      (!q || `${task.title} ${task.body ?? ''} ${task.id}`.toLowerCase().includes(q)) &&
      (!tenant || task.tenant === tenant) &&
      (!assignee || task.assignee === assignee) &&
      !(isAllBoards && task.board && hiddenBoards[task.board])

    const columns = orderLanes(board.columns)
      .filter(col => !(roadmapHidden && isRoadmapLane(col.name)))
      .map(col => ({ ...col, tasks: col.tasks.filter(keep) }))

    return { ...board, columns }
  }, [board, search, tenant, assignee, isAllBoards, hiddenBoards, roadmapHidden])

  const total = filtered?.columns.reduce((sum, col) => sum + col.tasks.length, 0) ?? 0

  // Card count behind the hidden-lanes pill — raw board, not `filtered` (which
  // has already dropped them), so the pill can say how much is parked there.
  const roadmapCount = useMemo(
    () => board?.columns.reduce((sum, col) => (isRoadmapLane(col.name) ? sum + col.tasks.length : sum), 0) ?? 0,
    [board]
  )

  // Every mutation below takes the card's `cardKey` (`key`) for the optimistic
  // cache edit and the bare `id` + `board` for the wire, so a same-id card on
  // another board can never be patched, deleted, or re-prioritized by mistake.
  const moveMut = useMutation({
    mutationFn: ({
      id,
      status,
      board: taskBoard,
      acknowledgeBlockLoop
    }: {
      key: string
      id: string
      status: string
      board?: string
      acknowledgeBlockLoop?: boolean
    }) => patchTask(id, { status, ...(acknowledgeBlockLoop ? { acknowledge_block_loop: true } : {}) }, taskBoard),
    onMutate: async ({ key, status }) => {
      await qc.cancelQueries({ queryKey: boardKey(slug, archived) })
      const previous = qc.getQueryData<KanbanBoard>(boardKey(slug, archived))

      if (previous) {
        qc.setQueryData(boardKey(slug, archived), moveCard(previous, key, status))
      }

      return { previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(boardKey(slug, archived), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: (_data, _err, vars) => {
      void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      void qc.invalidateQueries({ queryKey: ['kanban', 'task', slug, vars.id] })
    }
  })

  const deleteMut = useMutation({
    mutationFn: ({ id, board: taskBoard }: { key: string; id: string; board?: string }) => deleteTask(id, taskBoard),
    onMutate: async ({ key }) => {
      await qc.cancelQueries({ queryKey: boardKey(slug, archived) })
      const previous = qc.getQueryData<KanbanBoard>(boardKey(slug, archived))

      if (previous) {
        qc.setQueryData(boardKey(slug, archived), removeCard(previous, key))
      }

      return { previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(boardKey(slug, archived), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
  })

  // Toggling the star only ever touches `priority` — never status, title, or
  // any other field — so a rejected write can't be mistaken for a bigger
  // failure, and the optimistic patch below is safe to apply in isolation.
  const priorityMut = useMutation({
    mutationFn: ({ id, priority, board: taskBoard }: { key: string; id: string; priority: number; board?: string }) =>
      patchTask(id, { priority }, taskBoard),
    onMutate: async ({ key, priority }) => {
      await qc.cancelQueries({ queryKey: boardKey(slug, archived) })
      const previous = qc.getQueryData<KanbanBoard>(boardKey(slug, archived))

      if (previous) {
        qc.setQueryData(boardKey(slug, archived), setPriorityCard(previous, key, priority))
      }

      return { previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(boardKey(slug, archived), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: (_data, _err, vars) => {
      void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      void qc.invalidateQueries({ queryKey: ['kanban', 'task', slug, vars.id] })
    }
  })

  // Handlers take the card's `cardKey` (what `Card` hands back) and resolve
  // the (board, id) pair from it — never a bare id against the merged index.
  const onSetPriority = (key: string, priority: number) => {
    const task = index.get(key)

    if (!task) {
      return
    }

    priorityMut.mutate({ board: task.board ?? undefined, id: task.id, key, priority })
  }

  // A card the unblock-loop breaker parked in `triage` needs a deliberate
  // confirmation before it re-enters the work queue: the backend refuses the
  // bare drag with a 409, and this dialog is what tells the human WHY rather
  // than surfacing that refusal as a bare error toast. Holds the pending move
  // (never the mutation) so cancelling leaves the board exactly as it was.
  const [pendingLoopMove, setPendingLoopMove] = useState<null | {
    key: string
    id: string
    status: string
    board?: string
    title: string
  }>(null)

  const onMove = (key: string, status: string) => {
    const task = index.get(key)

    if (!task || task.status === status) {
      return
    }

    if (isLockedTarget(status)) {
      host.notify({ kind: 'info', message: lockedReason(k, status) })

      return
    }

    // Wishlist-lane rules, checked BEFORE the optimistic edit: the backend
    // refuses these with a 400, and painting the move first would flash a
    // phantom card into a lane it can never reach. `laneDropAllowed` is the
    // same predicate the menus filter on, so the two can't drift.
    if (!laneDropAllowed(task.status, status)) {
      host.notify({
        kind: 'warning',
        message: k.laneDropRefused(columnLabel(k, task.status), columnLabel(k, status))
      })

      return
    }

    // Spawning straight to Ready skips auto-decompose, which is the standing
    // default for a roadmap item — so it is the one lane move that asks first.
    if (task.status === 'roadmap' && status === 'ready') {
      setSpawnReadyKey(key)

      return
    }

    // Dragging a loop-broken card out of triage re-arms exactly the loop the
    // breaker parked it to stop, so the backend refuses the bare PATCH (409).
    // Disjoint from the roadmap branches above: this one only fires from
    // `triage`, those only from a wishlist lane.
    if (needsBlockLoopAck(task, status)) {
      setPendingLoopMove({ board: task.board ?? undefined, id: task.id, key, status, title: task.title })

      return
    }

    moveMut.mutate({ board: task.board ?? undefined, id: task.id, key, status })
  }

  const errorMessage = error ? errText(error) : null

  // The board the open drawer's card belongs to. Read from the index while the
  // card is present, falling back to the key itself so a refresh that drops
  // the row mid-view can't strand the drawer's writes on the wrong board.
  const openBoard = openKey ? (index.get(openKey)?.board ?? parseCardKey(openKey).board) : undefined

  // Grab-to-scrub the lane strip (shared primitive, same as the dashboard's pan).
  const lanesRef = useRef<HTMLDivElement>(null)
  const { grabbing, onMouseDown } = useGrabScroll(lanesRef)

  // Lane collapse: auto (empty → rail) unless the user overrode it. The map
  // stores only deviations from auto, so it stays tiny and self-heals. On a
  // board with no work at all, auto is disabled — a wall of rails teaches
  // nothing, so a fresh board shows its full structure instead.
  const laneOverrides = useValue($collapsedLanes)
  const boardHasWork = (board?.columns.reduce((sum, col) => sum + col.tasks.length, 0) ?? 0) > 0

  // An override only lives for the lane's current empty/non-empty phase: when
  // emptiness flips (last card dragged out, first card dropped in) the stale
  // override is dropped and auto takes over — so a drained lane collapses even
  // if it was manually expanded ages ago, while expanding an empty lane still
  // sticks for as long as it stays empty.
  //
  // The phase is a string signature held in state, not a ref: React bails out
  // when it's unchanged, so the common case (a poll where no lane's emptiness
  // moved) costs no extra render, and nothing lags a render behind the value
  // it mirrors.
  const lanePhase = filtered
    ? filtered.columns.map(col => `${col.name}:${col.tasks.length === 0 ? 'empty' : 'full'}`).join('|')
    : null

  const [prevLanePhase, setPrevLanePhase] = useState<null | string>(null)

  useEffect(() => {
    if (lanePhase === null || lanePhase === prevLanePhase) {
      return
    }

    setPrevLanePhase(lanePhase)

    if (prevLanePhase === null) {
      return
    }

    const before = new Map(prevLanePhase.split('|').map(entry => entry.split(':') as [string, string]))
    const overrides = { ...$collapsedLanes.get() }
    let changed = false

    for (const entry of lanePhase.split('|')) {
      const [name, phase] = entry.split(':')
      const was = before.get(name)

      if (was !== undefined && was !== phase && name in overrides) {
        delete overrides[name]
        changed = true
      }
    }

    if (changed) {
      $collapsedLanes.set(overrides)
    }
  }, [lanePhase, prevLanePhase])

  const toggleLane = (name: string, auto: boolean) => {
    const overrides = { ...laneOverrides }
    const next = !(overrides[name] ?? auto)

    if (next === auto) {
      delete overrides[name]
    } else {
      overrides[name] = next
    }

    $collapsedLanes.set(overrides)
  }

  return (
    <DependencyContext.Provider value={dependencies}>
      <BoardInfoContext.Provider value={isAllBoards ? boardInfoMap : null}>
        <div className="relative flex h-full flex-col overflow-hidden bg-(--ui-surface-background)">
          {/* Page-owned titlebar chrome: exists exactly while this page is mounted. */}
          <Contribute area={TITLEBAR_AREAS.center} id="kanban:board-switcher">
            <BoardSwitcher />
          </Contribute>

          <header className="flex shrink-0 flex-wrap items-center gap-2 px-4 py-2">
            <h1 className="text-sm font-semibold text-foreground">{k.title}</h1>
            <span className="rounded-full bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">
              {total}
            </span>
            {board && (
              <FilterMenu
                archived={archived}
                assignee={assignee}
                board={board}
                onArchived={setArchived}
                onAssignee={setAssignee}
                onTenant={setTenant}
                tenant={tenant}
              />
            )}
            <SearchField aria-label={k.filterCards} onChange={setSearch} placeholder={k.filterCards} value={search} />
            <div className="ml-auto flex items-center gap-1">
              {board && !archived && <ArchiveDoneControl />}
              {/* Wishlist-lane visibility. Hidden collapses to a compact pill
                  carrying the parked count, so the lanes stay one click away
                  without costing a lane's width when you don't want them. */}
              {board &&
                (roadmapHidden ? (
                  <Button
                    aria-label={k.roadmapShowLanes}
                    className="h-6 gap-1 rounded-full px-2 text-[0.625rem] tabular-nums text-(--ui-text-tertiary)"
                    onClick={toggleRoadmapHidden}
                    size="xs"
                    variant="ghost"
                  >
                    <Codicon name="map" size="0.7rem" />
                    {k.roadmapPill(roadmapCount)}
                  </Button>
                ) : (
                  <Tip label={k.roadmapHideLanes}>
                    <Button
                      aria-label={k.roadmapHideLanes}
                      onClick={toggleRoadmapHidden}
                      size="icon-xs"
                      variant="ghost"
                    >
                      <Codicon name="map" size="0.85rem" />
                    </Button>
                  </Tip>
                ))}
              <Tip label={k.ideaTitle}>
                <Button aria-label={k.ideaTitle} onClick={() => setIdeaOpen(true)} size="icon-xs" variant="ghost">
                  <Codicon name="lightbulb" size="0.85rem" />
                </Button>
              </Tip>
              <Tip label={k.orchestrationSettings}>
                <Button
                  aria-label={k.orchestrationSettings}
                  className={cn(settingsOpen && 'bg-(--ui-control-active-background) text-foreground')}
                  onClick={() => setSettingsOpen(!settingsOpen)}
                  size="icon-xs"
                  variant="ghost"
                >
                  <Codicon name="organization" size="0.85rem" />
                </Button>
              </Tip>
              <Button onClick={() => setAddStatus('triage')} size="sm">
                <Codicon name="add" size="0.8rem" />
                {k.newTask}
              </Button>
            </div>
          </header>

          {settingsOpen && <OrchestrationPanel />}

          {board && <Intro />}

          {isAllBoards && (
            <BoardFilterChips boards={boardInfoList} hidden={hiddenBoards} onToggle={toggleBoardVisible} />
          )}

          {isAllBoards && <BoardsErrorNotice errors={board?.errors} />}

          {/* The answer bar. Only while a trace is live, so the board chrome
          is unchanged in the common case. Its own row rather than an overlay:
          the board is dimmed underneath and an overlay would compete with the
          selection bar for the same corner. Clicking a row moves the focus
          (never toggles it off — the row is a different card). */}
          {focused && (
            <FocusAnswerBar
              depth={focusDepth}
              focused={focused}
              graph={graph}
              index={index}
              onClear={() => setFocused(null)}
              onDepth={setFocusDepth}
              onFocus={setFocused}
            />
          )}

          {errorMessage && !board ? (
            <div className="grid flex-1 place-items-center">
              <ErrorState title={errorMessage} />
            </div>
          ) : !filtered ? (
            <div className="grid flex-1 place-items-center">
              <Loader type="lemniscate-bloom" />
            </div>
          ) : total === 0 ? (
            <div className="grid flex-1 place-items-center px-4 text-center">
              <div className="flex flex-col items-center gap-2">
                <Codicon className="text-(--ui-text-quaternary)" name="project" size="1.25rem" />
                <p className="text-xs text-(--ui-text-tertiary)">
                  {search || tenant || assignee ? k.noMatch : k.noTasks}
                </p>
                <Button className="mt-0.5" onClick={() => setAddStatus('triage')} size="sm" variant="outline">
                  <Codicon name="add" size="0.75rem" />
                  {k.newTask}
                </Button>
              </div>
            </div>
          ) : (
            <div
              // This is the board's sole vertical flex child. `min-h-0` lets it
              // yield space to the page chrome (including the status bar)
              // instead of extending underneath it on a short viewport.
              // `relative`: the dependency-arrow layer is positioned against the
              // strip's scroll content, so it pans with the lanes for free.
              // While a trace is live the right gutter grows to fit the widest
              // same-lane bracket (BRACKET_MAX + casing), so the last lane's
              // loop is never clipped by the strip's scroll edge.
              className={cn(
                'relative flex min-h-0 flex-1 gap-2 overflow-x-auto px-4 pt-1 pb-3',
                focused && 'pr-16',
                grabbing && 'cursor-grabbing'
              )}
              // Clicking the board background clears the trace — the gaps between
              // lanes, a lane's padding, a lane header, empty column space. Keyed
              // off "the click did not land on a card" rather than a strict
              // `currentTarget` check, which would only catch the thin gutters.
              // Cards are the draggable nodes (same vocabulary useGrabScroll uses),
              // so a click on a card — including its own trace button — is left to
              // the card's own handler. A click on a dependency line is not a
              // click on the background either: lines are hover targets. The
              // line layer is pointer-transparent (so it never swallows a
              // card click), so "on a line" means "a line is hovered".
              onClickCapture={event => {
                const target = event.target as Element

                if (
                  focused &&
                  !$hotEdge.get() &&
                  !target.closest(`[draggable="true"], [data-board-arrows], [${LANE_GAP_ATTR}]`)
                ) {
                  setFocused(null)
                }
              }}
              onMouseDown={onMouseDown}
              ref={lanesRef}
            >
              {filtered.columns.map(col => {
                const auto = boardHasWork && col.tasks.length === 0

                return (
                  <Column
                    collapsed={laneOverrides[col.name] ?? auto}
                    column={col}
                    columns={columnNames}
                    key={col.name}
                    onAdd={setAddStatus}
                    onDelete={key => {
                      const task = index.get(key)

                      if (task) {
                        deleteMut.mutate({ board: task.board ?? undefined, id: task.id, key })
                      }
                    }}
                    onDropTask={onMove}
                    onMove={onMove}
                    onOpen={setOpenKey}
                    onSetPriority={onSetPriority}
                    onToggle={() => toggleLane(col.name, auto)}
                    onToggleSelect={toggleSelect}
                    selected={selected}
                  />
                )
              })}
              {/* Arrows between the real cards while a trace is live. Keyed off
                  the same `chain` sets that light the cards, so an arrow never
                  lands on a dimmed card. */}
              <BoardDependencyArrows
                depth={focusDepth}
                downstream={chain.downstream}
                focused={focused}
                graph={graph}
                index={index}
                stripRef={lanesRef}
                upstream={chain.upstream}
              />
            </div>
          )}

          {selected.size > 0 && (
            <SelectionBar
              columns={columnNames}
              index={index}
              onClear={() => setSelected(new Set())}
              onDone={failed => setSelected(new Set(failed))}
              selected={selected}
            />
          )}

          <NewTaskDialog onClose={() => setAddStatus(null)} parents={parentOptions} target={addStatus} />
          <IdeaCaptureDialog onClose={() => setIdeaOpen(false)} open={ideaOpen} />
          {/* Roadmap → Ready is the one spawn that bypasses auto-decompose, so
              it confirms; Roadmap → Triage (the default) never asks. The
              dialog owns its own pending/done/error beat (ConfirmDialog
              contract): `onConfirm` returns the mutation's own promise so a
              server-side rejection surfaces inline and keeps the dialog open
              instead of closing on a failed spawn. */}
          <ConfirmDialog
            confirmLabel={k.spawnReadyConfirm}
            description={k.spawnReadyBody}
            onClose={() => setSpawnReadyKey(null)}
            onConfirm={async () => {
              const task = spawnReadyKey ? index.get(spawnReadyKey) : undefined

              if (!task) {
                return
              }

              await moveMut.mutateAsync({
                board: task.board ?? undefined,
                id: task.id,
                key: spawnReadyKey!,
                status: 'ready'
              })
            }}
            open={spawnReadyKey !== null}
            title={k.spawnReadyTitle}
          />
          {/* Dragging a loop-broken card back into the work queue is refused by
              the backend (409) without an explicit acknowledgment; the dialog
              is what tells the human WHY, and its confirm re-sends the same
              move carrying the ack. */}
          <ConfirmDialog
            cancelLabel={k.cancel}
            confirmLabel={k.blockLoopConfirmAction}
            description={k.blockLoopConfirmBody(
              pendingLoopMove?.title ?? '',
              columnLabel(k, pendingLoopMove?.status ?? '')
            )}
            onClose={() => setPendingLoopMove(null)}
            onConfirm={async () => {
              if (pendingLoopMove) {
                await moveMut.mutateAsync({ ...pendingLoopMove, acknowledgeBlockLoop: true })
              }
            }}
            open={Boolean(pendingLoopMove)}
            title={k.blockLoopConfirmTitle}
          />
          {/* The drawer speaks bare task ids (its detail payload's `links` are
              plain ids on ONE board), so translate at this boundary: the open
              card's board comes from the index, and a navigation out of a
              dependency row re-keys onto that same board — links never cross
              boards, so the target is always a sibling. */}
          <TaskDrawer
            board={openBoard}
            columns={columnNames}
            id={openKey ? (index.get(openKey)?.id ?? parseCardKey(openKey).id) : null}
            onClose={() => setOpenKey(null)}
            onOpen={id => setOpenKey(cardKey(id, openBoard))}
          />
        </div>
      </BoardInfoContext.Provider>
    </DependencyContext.Provider>
  )
}
