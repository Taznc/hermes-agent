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
  compactNumber,
  ConfirmDialog,
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
  Contribute,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  formatModifierToken,
  host,
  Input,
  Loader,
  SearchField,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
  Tip,
  TITLEBAR_AREAS,
  useGrabScroll,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import {
  createContext,
  type CSSProperties,
  type ClipboardEvent as ReactClipboardEvent,
  type DragEvent as ReactDragEvent,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react'

import {
  $boardSlug,
  $collapsedLanes,
  $hiddenBoards,
  $introDismissed,
  $lanesByProfile,
  $roadmapHidden,
  addRoadmapIdea,
  ALL_BOARDS,
  archiveDone,
  boardKey,
  BOARDS_KEY,
  bulkTasks,
  createTask,
  deleteStagedAttachment,
  deleteTask,
  estimateNew,
  fetchAllBoards,
  fetchArchiveDonePreflight,
  fetchAttachmentDataUrl,
  fetchBoard,
  fetchBoards,
  fetchProfiles,
  patchTask,
  primeAllBoardsSocket,
  PROFILES_KEY,
  stageAttachment
} from './api'
import { BoardSwitcher } from './board-switcher'
import {
  blockerStand,
  buildGraph,
  cardKey,
  type DependencyGraph,
  downstreamOf,
  focusSets,
  indexBoard,
  parseCardKey,
  taskCardKey,
  upstreamOf
} from './deps'
import { TaskDrawer } from './drawer'
import { EMPTY_OVERRIDE, ModelOverrideField, overrideCreateFields, type TaskModelOverride } from './model-override'
import { OrchestrationPanel } from './orchestration'
import { PriorityPicker } from './priority-picker'
import { needsBlockLoopAck } from './status-guidance'
import {
  type BoardAllInfo,
  columnMeta,
  isRoadmapLane,
  type KanbanBoard,
  type KanbanTask,
  laneDropAllowed,
  orderLanes,
  type TaskEstimate
} from './types'
import {
  $newTaskLane,
  ago,
  type ArcState,
  arcState,
  Avatar,
  columnHelp,
  columnLabel,
  errText,
  FIELD_LABEL,
  IdChip,
  isLockedTarget,
  lockedReason,
  RunClock,
  useDefaultAssignee,
  useKanban,
  useOrchestration
} from './ui'

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

// ── dependency view (graph + focus), shared by every card ────────────────────

/**
 * The board's dependency adjacency, its cardKey→task index, and the current
 * focus, handed to cards through context rather than threaded as props:
 * `Column` already carries a dozen callbacks, and every card needs the same
 * three objects. The graph and index are built ONCE per board payload up in
 * `KanbanBoardPage` — rebuilding them per card would be O(cards × edges) on
 * every render.
 *
 * Every id-shaped value here is a `cardKey` (board + id in All Boards mode,
 * bare id in single-board mode), because task ids are only unique per board.
 *
 * `hasEdges` is the capability probe for an older backend that sends
 * `link_counts` but not `link_edges`: without edges we can still show honest
 * counts, but we cannot know which blockers are still gating.
 */
interface DependencyView {
  downstream: ReadonlySet<string>
  focused: null | string
  graph: DependencyGraph
  hasEdges: boolean
  index: Map<string, KanbanTask>
  onFocus: (key: string) => void
  upstream: ReadonlySet<string>
}

const EMPTY_IDS: ReadonlySet<string> = new Set<string>()
const EMPTY_BOARD_INFO: readonly BoardAllInfo[] = []

// Module-level constant so the context default keeps a stable identity across
// renders (a fresh object here would re-render every consumer for nothing).
const NO_DEPENDENCIES: DependencyView = {
  downstream: EMPTY_IDS,
  focused: null,
  graph: { blockedBy: new Map(), blocking: new Map() },
  hasEdges: false,
  index: new Map(),
  onFocus: () => {},
  upstream: EMPTY_IDS
}

const DependencyContext = createContext<DependencyView>(NO_DEPENDENCIES)

const useDependencies = () => useContext(DependencyContext)

// ── board attribution (All Boards mode only) ──────────────────────────────────

/** Per-board display chrome (name/color/icon), keyed by slug — populated only
 *  in the consolidated All Boards view so `Card` can render a board badge.
 *  `null` in single-board mode: cards never need attribution against
 *  themselves, and `Card` skips the badge entirely when this is null.
 *  Exported so `BoardBadge` is testable by wrapping it in a provider without
 *  mounting the whole page. */
export const BoardInfoContext = createContext<Map<string, BoardAllInfo> | null>(null)

const useBoardInfo = () => useContext(BoardInfoContext)

type FocusRole = 'downstream' | 'focused' | 'upstream'

/** Where a card sits relative to the focused one, by `cardKey`. `null` while
 *  nothing is focused AND for unrelated cards — callers tell them apart via
 *  `focused`. */
function focusRole(deps: DependencyView, key: string): FocusRole | null {
  if (!deps.focused) {
    return null
  }

  if (deps.focused === key) {
    return 'focused'
  }

  return deps.upstream.has(key) ? 'upstream' : deps.downstream.has(key) ? 'downstream' : null
}

/** Does this card have any link at all — i.e. is focusing it meaningful? */
function hasDependencies(deps: DependencyView, task: KanbanTask): boolean {
  if (deps.hasEdges) {
    const key = taskCardKey(task)

    return upstreamOf(deps.graph, key).length > 0 || downstreamOf(deps.graph, key).length > 0
  }

  return Boolean(task.link_counts && (task.link_counts.parents > 0 || task.link_counts.children > 0))
}

/**
 * Statuses where "every blocker is done" is ACTIONABLE news worth a green chip.
 *
 * Deliberately narrow, and please don't "simplify" this gate away. On a real
 * board the overwhelmingly common shape of `parents > 0 && gating === 0` is a
 * card that is ITSELF already done — it finished long after its blockers did.
 * Measured on the reference board: of 32 tasks with all blockers satisfied, 30
 * were `done` and 2 were `running`. Dropping the status gate paints 30 done
 * cards green and drowns the handful that actually need a human to move them.
 * Only a card still parked in a waiting lane can act on the news.
 */
const PROMOTABLE_STATUSES: ReadonlySet<string> = new Set(['on_hold', 'scheduled', 'todo', 'triage'])

// ── card ─────────────────────────────────────────────────────────────────────

function Meta({ children, className, icon }: { children: ReactNode; className?: string; icon: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1', className)}>
      <Codicon name={icon} size="0.7rem" />
      {children}
    </span>
  )
}

/** Small thumbnail indicator on cards that have at least one image
 *  attachment (#cae4c2ba). Lazily fetches the first image's bytes as a data
 *  URL only once mounted (cards off-screen never pay the fetch); a fetch or
 *  decode failure quietly hides the thumbnail rather than showing a broken
 *  image icon on a card. */
function CardThumb({ attachmentId, board }: { attachmentId: number | string; board?: string }) {
  const [broken, setBroken] = useState(false)

  const { data } = useQuery({
    queryFn: () => fetchAttachmentDataUrl(attachmentId, board),
    queryKey: ['kanban', 'attachment-data-url', attachmentId],
    retry: false,
    staleTime: Infinity
  })

  if (!data?.data_url || broken) {
    return null
  }

  return (
    <img
      alt=""
      aria-hidden
      className="size-8 shrink-0 rounded object-cover"
      onError={() => setBroken(true)}
      src={data.data_url}
    />
  )
}

/**
 * The dependency chips. Replaces an anonymous `parents + children` total that
 * told the reader nothing: the two directions mean opposite things, so they
 * get separate chips.
 *
 *  - blocked-by, red while at least one blocker still gates;
 *  - "blockers clear" in green, but only on a card that can act on it
 *    (see `PROMOTABLE_STATUSES`);
 *  - blocks-n, in a cool tone — informational, never alarming.
 *
 * Without `link_edges` (older backend) we can't resolve blocker statuses, so
 * we fall back to the raw `link_counts` numbers and drop the gating/clear
 * distinction entirely rather than guessing at it.
 */
function DependencyChips({ task }: { task: KanbanTask }) {
  const k = useKanban()
  const deps = useDependencies()
  const key = taskCardKey(task)
  const stand = blockerStand(deps.graph, deps.index, key)

  const blockedBy = deps.hasEdges ? stand.total : (task.link_counts?.parents ?? 0)
  const blocks = deps.hasEdges ? downstreamOf(deps.graph, key).length : (task.link_counts?.children ?? 0)
  const clear = deps.hasEdges && blockedBy > 0 && stand.gating === 0 && PROMOTABLE_STATUSES.has(task.status)

  return (
    <>
      {clear ? (
        <Tip label={k.depClearTip}>
          <span className="inline-flex shrink-0 cursor-help items-center gap-1 font-medium text-emerald-500">
            <Codicon name="pass-filled" size="0.7rem" />
            {k.depClear}
          </span>
        </Tip>
      ) : blockedBy > 0 ? (
        <Meta className={cn('shrink-0', stand.gating > 0 && 'text-destructive')} icon="circle-slash">
          {k.depBlockedByCount(blockedBy)}
        </Meta>
      ) : null}
      {blocks > 0 && (
        <Meta className="shrink-0 text-sky-400/80" icon="references">
          {k.depBlocksCount(blocks)}
        </Meta>
      )}
    </>
  )
}

/**
 * The upstream/downstream flag on a card caught in the focused chain. Lives in
 * the footer's left group rather than as its own row so lighting a chain never
 * changes a card's HEIGHT — only colours and this one inline chip appear, and
 * the flex row absorbs the width. Nothing below it in the column moves.
 */
function FocusFlag({ task }: { task: KanbanTask }) {
  const k = useKanban()
  const deps = useDependencies()
  const role = focusRole(deps, taskCardKey(task))

  if (role !== 'downstream' && role !== 'upstream') {
    return null
  }

  return (
    <span
      className={cn(
        'shrink-0 font-semibold uppercase tracking-[0.08em]',
        role === 'upstream' ? 'text-amber-500' : 'text-sky-400'
      )}
    >
      {role === 'upstream' ? k.depFocusUpstream : k.depFocusDownstream}
    </span>
  )
}

function CardFooter({
  arc,
  onSetPriority,
  task
}: {
  arc: ArcState | null
  onSetPriority: (priority: number) => void
  task: KanbanTask
}) {
  const k = useKanban()
  const created = ago(task.created_at)
  const fallback = useDefaultAssignee()
  const orchestrator = useOrchestration()?.resolved_orchestrator_profile ?? ''
  // Ready + no assignee: with a configured default assignee the dispatcher
  // auto-assigns on its next tick (#27145) — say THAT, not "won't run". Only
  // a board with no fallback has the genuine silent failure.
  const unassignedReady = task.status === 'ready' && !task.assignee

  // The agent on the hook for a queued card: the explicit assignee, else the
  // auto-default (ready), else the specifier that rewrites triage cards.
  const attached = task.assignee || (task.status === 'ready' ? fallback : task.status === 'triage' ? orchestrator : '')

  const meta = columnMeta(task.status)

  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1 text-[0.625rem] text-(--ui-text-tertiary)">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        {arc === 'queued' && attached ? (
          // WHO is coming for the card. The arc only animates once the agent is
          // actually working; while queued, the named chip carries "attached".
          <Tip
            label={
              task.status === 'review'
                ? k.reviewChecking
                : task.assignee
                  ? k.attachedTip(attached)
                  : task.status === 'triage'
                    ? k.orchestratorTip(attached)
                    : k.autoAssignTip(attached)
            }
          >
            <span
              className="inline-flex min-w-0 max-w-full cursor-help items-center gap-1 font-medium"
              style={{ color: meta.tone }}
            >
              <Avatar name={attached} size="1.125rem" />
              <span className="truncate">
                {!task.assignee && '→ '}
                {attached}
              </span>
            </span>
          </Tip>
        ) : task.assignee ? (
          <Avatar name={task.assignee} size="1.125rem" />
        ) : null}
        {arc === 'running' && (
          <Tip label={k.arcRunning}>
            <span className="shrink-0 cursor-help">
              <RunClock task={task} />
            </span>
          </Tip>
        )}
        {arc === 'stale' && (
          <Tip label={k.arcStale}>
            <span className="shrink-0 cursor-help font-medium text-amber-500">{k.noHeartbeat}</span>
          </Tip>
        )}
        {unassignedReady && !fallback && (
          <Tip label={k.wontRunTip}>
            <span className="inline-flex shrink-0 cursor-help items-center gap-1 text-amber-500">
              <Codicon name="debug-disconnect" size="0.7rem" />
              {k.wontRun}
            </span>
          </Tip>
        )}
        <FocusFlag task={task} />
      </div>
      <span
        onClick={event => event.stopPropagation()}
        onMouseDown={event => event.stopPropagation()}
        onPointerDown={event => event.stopPropagation()}
      >
        <PriorityPicker onChange={onSetPriority} priority={task.priority} />
      </span>
      <div className="col-span-2 flex min-w-0 flex-wrap items-center justify-end gap-x-2 gap-y-1">
        {task.progress && task.progress.total > 0 && (
          <Meta icon="checklist">
            {task.progress.done}/{task.progress.total}
          </Meta>
        )}
        {Boolean(task.comment_count) && <Meta icon="comment">{task.comment_count}</Meta>}
        <DependencyChips task={task} />
        {task.warnings && task.warnings.count > 0 && (
          <span className="inline-flex items-center gap-0.5 text-destructive">
            <Codicon name="warning" size="0.7rem" />
            {task.warnings.count}
          </span>
        )}
        {created && !task.assignee && !unassignedReady ? (
          <span className="text-(--ui-text-quaternary)">{created}</span>
        ) : null}
        <IdChip className="min-w-0 text-[0.6rem]" id={task.id} />
      </div>
    </div>
  )
}

/**
 * The wishlist-lane card footer: id, priority, and the parent link only.
 *
 * A lane card is not work — nothing is assigned to it, no run has ever
 * touched it, and it cannot be late — so every affordance that narrates
 * machine activity is deliberately absent here (assignee/model badge, run
 * clock, age, heartbeat warning, run/comment/warning counts). Rendering the
 * full footer would make a wishlist read like a stalled backlog, which is
 * exactly the cost the Roadmap lanes exist to avoid.
 *
 * Priority stays: ranking a wishlist is the one thing you actually do to it.
 */
function LaneCardFooter({ onSetPriority, task }: { onSetPriority: (priority: number) => void; task: KanbanTask }) {
  const deps = useDependencies()
  const key = taskCardKey(task)
  const parents = deps.hasEdges ? upstreamOf(deps.graph, key).length : (task.link_counts?.parents ?? 0)

  return (
    <div className="flex min-w-0 items-center gap-2 text-[0.625rem] text-(--ui-text-tertiary)">
      {parents > 0 && <Meta icon="circle-slash">{parents}</Meta>}
      <span
        className="ml-auto"
        onClick={event => event.stopPropagation()}
        onMouseDown={event => event.stopPropagation()}
        onPointerDown={event => event.stopPropagation()}
      >
        <PriorityPicker onChange={onSetPriority} priority={task.priority} />
      </span>
    </div>
  )
}

// ── board attribution badge (All Boards mode only) ───────────────────────────

/** A small board-name chip on a card, shown ONLY in the consolidated All
 *  Boards view (`useBoardInfo()` is null in single-board mode, so this
 *  renders nothing there — same chrome/tokens as every other card meta,
 *  tinted with the board's own color when it set one). */
function BoardBadge({ task }: { task: KanbanTask }) {
  const boards = useBoardInfo()
  const slug = task.board

  if (!boards || !slug) {
    return null
  }

  const info = boards.get(slug)
  const label = task.board_name || info?.name || slug
  const tone = info?.color || 'var(--ui-text-tertiary)'

  return (
    <span
      className="inline-flex w-fit shrink-0 items-center gap-1 rounded-[3px] px-1 py-px text-[0.6rem] font-medium"
      style={{ backgroundColor: `color-mix(in srgb, ${tone} 14%, transparent)`, color: tone }}
    >
      {info?.icon && <Codicon name={info.icon} size="0.65rem" />}
      <span className="truncate">{label}</span>
    </span>
  )
}

export function Card({
  columns,
  onDelete,
  onMove,
  onOpen,
  onSetPriority,
  onToggleSelect,
  selected,
  task
}: {
  columns: string[]
  /** Every callback receives this card's `cardKey` (board + id in All Boards
   *  mode, bare id in single-board mode) — a bare id is ambiguous across
   *  boards and would let one card's action route to another board's row. */
  onDelete: (key: string) => void
  onMove: (key: string, status: string) => void
  onOpen: (key: string) => void
  onSetPriority: (key: string, priority: number) => void
  onToggleSelect: (key: string) => void
  selected: boolean
  task: KanbanTask
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const [dragging, setDragging] = useState(false)
  const meta = columnMeta(task.status)
  // A wishlist card is not work: no agent is coming for it, it has no runs and
  // no age to worry about. The presentational variant is DERIVED from the
  // card's own status rather than threaded down as a prop, so it can never
  // disagree with the lane the card is actually in.
  const lane = isRoadmapLane(task.status)
  // For a blocked card `latest_summary` IS the worker's block reason, which
  // may carry ```cmd / ```choices fences meant for the drawer's structured
  // rendering — on the 2-line card preview those are noise, so strip fences
  // and collapse whitespace to keep the preview to the prose ask.
  const rawSummary = task.latest_summary || task.body

  const summary = rawSummary
    ? rawSummary
        .replace(/```[a-zA-Z]*\s*[\s\S]*?```/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
    : rawSummary

  const fallback = useDefaultAssignee()
  const arc = arcState(task, fallback)
  const key = taskCardKey(task)

  const deps = useDependencies()
  const role = focusRole(deps, key)
  // Dimmed = a focus is active and this card is on neither end of it. Purely a
  // class swap: the card keeps its position, its identity, and its subtree.
  const dimmed = Boolean(deps.focused) && role === null
  const linked = hasDependencies(deps, task)

  // Per-card "send to roadmap ideas" (Phase 2.15 follow-up). Provenance-only
  // — title + id, never the body — reusing the exact contract + toast copy
  // the board-header free-typed capture already established (IdeaCaptureDialog
  // above). Success and roadmap-unavailable get distinct feedback; success also
  // invalidates the board query prefix, since this creates a real `idea` card
  // that every active board view must reconcile.
  //
  // The card's OWN board is passed explicitly: the endpoint creates the new
  // `idea` card in the addressed board, and without a board the backend falls
  // back to the ACTIVE one — so in All Boards mode the card could otherwise
  // appear on the wrong board, silently, under a success toast.
  const sendIdeaMut = useMutation({
    mutationFn: () => addRoadmapIdea(task.title, task.id, task.board ?? undefined),
    onSuccess: ({ ok, reason }) => {
      if (ok) {
        host.notify({ kind: 'success', message: k.ideaSaved })
        // The backend now creates a real `idea` card (Phase 2.15 successor) —
        // without a socket for this board (e.g. a stale connection, or the
        // All Boards aggregate view) the new card would stay invisible until
        // the next poll. Invalidate the board prefix so every board query
        // (single-board and All Boards alike) refetches and reconciles it in.
        void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      } else {
        host.notify({ kind: 'warning', message: reason === 'empty_idea' ? k.ideaEmpty : k.ideaUnavailable })
      }
    },
    onError: err => host.notify({ kind: 'error', message: errText(err) })
  })

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className={cn(
            'group relative flex cursor-grab flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) border-l-2 bg-(--ui-bg-elevated) p-2.5',
            // Hover matches the provider-picker rows: a quiet primary fill;
            // selected = the theme's focus color (same as a focused input).
            'transition-colors hover:bg-primary/[0.06] active:cursor-grabbing',
            selected && 'border-(--dt-composer-ring) bg-[color-mix(in_srgb,var(--dt-composer-ring)_7%,transparent)]',

            // Focus chain. Rings only — no layout property moves, so nothing
            // reflows and no card is re-ordered or unmounted.
            'transition-[opacity,filter,box-shadow,background-color]',
            role === 'focused' && 'ring-2 ring-(--ui-stroke-primary)',
            role === 'upstream' && 'ring-2 ring-amber-500/70',
            role === 'downstream' && 'ring-2 ring-sky-500/70',
            dimmed && 'opacity-35 saturate-50',
            dragging && 'opacity-40'
          )}
          draggable
          onClick={event => {
            if (event.metaKey || event.ctrlKey) {
              onToggleSelect(key)

              return
            }

            // Alt/Option-click is the power-user shortcut for the same trace
            // the hover button starts. Bare click still opens the drawer.
            if (event.altKey) {
              if (linked) {
                deps.onFocus(key)
              }

              return
            }

            onOpen(key)
          }}
          onDragEnd={() => setDragging(false)}
          onDragStart={event => {
            // The drag payload is the cardKey, not the bare id: a drop handler
            // resolving a bare id against the merged All Boards index could
            // land on a same-id card from another board.
            event.dataTransfer.setData('text/plain', key)
            event.dataTransfer.effectAllowed = 'move'
            // Snapshot the drag image before dimming the source, so the ghost
            // stays a solid card (dimming first would bake 40% into it).
            event.dataTransfer.setDragImage(event.currentTarget, event.nativeEvent.offsetX, event.nativeEvent.offsetY)
            setDragging(true)
          }}
          style={
            {
              '--kanban-tone': meta.tone,
              borderLeftColor: meta.tone
            } as CSSProperties
          }
        >
          {/* Machine-activity arc: animates ONLY while an agent is actually on
              the card (claimed + working; amber when the heartbeat is gone).
              Queued attachment is the footer's named-agent chip — a moving
              border on an idle card would lie. Hidden during drag/selection
              so those states stay legible. */}
          {(arc === 'running' || arc === 'stale') && !dragging && !selected && (
            <span aria-hidden className={cn('kanban-arc', arc === 'stale' && 'kanban-arc--stale')} />
          )}

          {/* Trace this card's dependency chain. A dedicated affordance rather
              than overloading a bare click (which must keep opening the
              drawer): it appears on hover, and
              ONLY on cards that actually have a link — so it advertises where
              dependencies exist instead of adding noise to every card.
              Alt-click on the card body does the same thing for the keyboard-
              hand crowd. Pressed state doubles as the "clear" control. */}
          {linked && (
            <Tip label={role === 'focused' ? k.depClearFocus : k.depFocusHint}>
              <button
                aria-label={role === 'focused' ? k.depClearFocus : k.depFocusHint}
                aria-pressed={role === 'focused'}
                className={cn(
                  'absolute top-1.5 right-1.5 grid size-5 place-items-center rounded text-(--ui-text-quaternary) transition-opacity hover:bg-(--chrome-action-hover) hover:text-foreground',
                  role === 'focused'
                    ? 'text-foreground opacity-100'
                    : 'opacity-0 focus-visible:opacity-100 group-hover:opacity-100'
                )}
                onClick={event => {
                  event.stopPropagation()
                  deps.onFocus(key)
                }}
                type="button"
              >
                <Codicon name="references" size="0.8rem" />
              </button>
            </Tip>
          )}
          <span
            className={cn(
              'line-clamp-2 text-[0.8125rem] font-medium leading-snug text-foreground',
              // Keep the title clear of the dependency focus affordance. Static
              // per task, so hover never reflows the card.
              linked && 'pr-5'
            )}
          >
            {task.title || task.id}
          </span>
          {!lane && <BoardBadge task={task} />}
          {summary && !lane && (
            <span className="line-clamp-2 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{summary}</span>
          )}
          {task.image_attachment_id != null && !lane && (
            <CardThumb attachmentId={task.image_attachment_id} board={task.board ?? undefined} />
          )}
          {lane ? (
            <LaneCardFooter onSetPriority={priority => onSetPriority(key, priority)} task={task} />
          ) : (
            <CardFooter arc={arc} onSetPriority={priority => onSetPriority(key, priority)} task={task} />
          )}
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => onOpen(key)}>
          <Codicon name="link-external" size="0.85rem" />
          {k.open}
        </ContextMenuItem>
        <ContextMenuItem onSelect={() => onToggleSelect(key)}>
          <Codicon name={selected ? 'close' : 'check-all'} size="0.85rem" />
          {selected ? k.deselect : k.select(formatModifierToken('mod'))}
        </ContextMenuItem>

        {lane && <ContextMenuSeparator />}
        {task.status === 'idea' && (
          <ContextMenuItem onSelect={() => onMove(key, 'roadmap')}>
            <Codicon name="map" size="0.85rem" />
            {k.laneRefine}
          </ContextMenuItem>
        )}
        {task.status === 'roadmap' && (
          <>
            <ContextMenuItem onSelect={() => onMove(key, 'idea')}>
              <Codicon name="lightbulb" size="0.85rem" />
              {k.laneDemote}
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => onMove(key, 'triage')}>
              <Codicon name="inbox" size="0.85rem" />
              {k.laneSpawnTriage}
            </ContextMenuItem>
            <ContextMenuItem onSelect={() => onMove(key, 'ready')}>
              <Codicon name="play-circle" size="0.85rem" />
              {k.laneSpawnReady}
            </ContextMenuItem>
          </>
        )}
        {lane && (
          <ContextMenuItem onSelect={() => onMove(key, 'archived')}>
            <Codicon name="archive" size="0.85rem" />
            {k.archive}
          </ContextMenuItem>
        )}

        {!lane &&
          columns
            .filter(name => name !== task.status && !isLockedTarget(name) && laneDropAllowed(task.status, name))
            .map(name => (
              <ContextMenuItem key={name} onSelect={() => onMove(key, name)}>
                <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
                {k.moveTo(columnLabel(k, name))}
              </ContextMenuItem>
            ))}
        <ContextMenuSeparator />
        {/* Sending a lane card back to the Ideas lane it already lives in is
            noise, so the capture action is live-cards-only. */}
        {!lane && (
          <>
            <ContextMenuItem disabled={sendIdeaMut.isPending} onSelect={() => sendIdeaMut.mutate()}>
              <Codicon name="lightbulb" size="0.85rem" />
              {k.sendToRoadmap}
            </ContextMenuItem>
            <ContextMenuSeparator />
          </>
        )}
        <ContextMenuItem onSelect={() => onDelete(key)} variant="destructive">
          <Codicon name="trash" size="0.85rem" />
          {k.delete}
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}

// ── column ───────────────────────────────────────────────────────────────────

function Column({
  collapsed,
  column,
  columns,
  onAdd,
  onDelete,
  onDropTask,
  onMove,
  onOpen,
  onSetPriority,
  onToggle,
  onToggleSelect,
  selected
}: {
  collapsed: boolean
  column: { name: string; tasks: KanbanTask[] }
  columns: string[]
  onAdd: (status: string) => void
  /** Card callbacks take a `cardKey`, not a bare id — see `Card`. */
  onDelete: (key: string) => void
  onDropTask: (key: string, status: string) => void
  onMove: (key: string, status: string) => void
  onOpen: (key: string) => void
  onSetPriority: (key: string, priority: number) => void
  onToggle: () => void
  onToggleSelect: (key: string) => void
  selected: ReadonlySet<string>
}) {
  const k = useKanban()
  const [over, setOver] = useState(false)
  const meta = columnMeta(column.name)
  const label = columnLabel(k, column.name)
  const locked = isLockedTarget(column.name)
  const byProfile = useValue($lanesByProfile)

  // The dashboard's "lanes by profile": sub-group Running by assignee so a
  // fleet's in-flight work reads per-worker. Null = flat (off, or trivial).
  const lanes = useMemo(() => {
    if (!byProfile || column.name !== 'running' || column.tasks.length === 0) {
      return null
    }

    const groups = new Map<string, KanbanTask[]>()

    for (const task of column.tasks) {
      const key = task.assignee || UNASSIGNED_LANE
      groups.set(key, [...(groups.get(key) ?? []), task])
    }

    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b))
  }, [byProfile, column])

  const dragHandlers = {
    onDragLeave: () => setOver(false),
    onDragOver: (event: ReactDragEvent<HTMLElement>) => {
      // Locked lanes don't preventDefault → the OS shows the no-drop cursor
      // and the drop event never fires. The lane is honest about itself.
      if (locked) {
        event.dataTransfer.dropEffect = 'none'

        return
      }

      event.preventDefault()
      event.dataTransfer.dropEffect = 'move'
      setOver(true)
    },
    onDrop: (event: ReactDragEvent<HTMLElement>) => {
      event.preventDefault()
      setOver(false)
      const id = event.dataTransfer.getData('text/plain')

      if (id) {
        onDropTask(id, column.name)
      }
    }
  }

  const wash = over && !locked ? 'bg-(--ui-bg-quinary)' : 'bg-[color-mix(in_srgb,var(--ui-bg-quinary)_50%,transparent)]'

  // Collapsed = a thin vertical rail: dot, sideways label, count. Still a live
  // drop target (drop straight onto the rail); click expands. The dot sits in
  // the same h-5 header row as an expanded lane's, so dots align across the
  // board regardless of collapse state.
  if (collapsed) {
    return (
      <button
        {...dragHandlers}
        aria-label={k.expand(label)}
        className={cn(
          'flex h-full w-8 shrink-0 flex-col items-center gap-1.5 rounded-lg p-2 transition-colors hover:bg-(--ui-bg-quinary)',
          wash
        )}
        onClick={onToggle}
        type="button"
      >
        <span className="grid h-5 shrink-0 place-items-center">
          <span className="size-1.5 rounded-full" style={{ backgroundColor: meta.tone }} />
        </span>
        <span className="text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary) [writing-mode:vertical-rl]">
          {label}
        </span>
        {column.tasks.length > 0 && (
          <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{column.tasks.length}</span>
        )}
      </button>
    )
  }

  return (
    <div
      {...dragHandlers}
      className={cn('group/col flex h-full w-64 shrink-0 flex-col rounded-lg p-2 transition-colors', wash)}
    >
      <header className="mb-1.5 flex h-5 items-center gap-1.5 px-1">
        <span className="size-1.5 rounded-full" style={{ backgroundColor: meta.tone }} />
        <Tip label={columnHelp(k, column.name)}>
          <span className="cursor-help text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)">
            {label}
          </span>
        </Tip>
        <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{column.tasks.length}</span>
        <button
          aria-label={k.collapse(label)}
          className="ml-auto grid size-5 place-items-center rounded text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100 group-hover/col:opacity-100"
          onClick={onToggle}
          type="button"
        >
          <Codicon name="chevron-left" size="0.75rem" />
        </button>
      </header>
      <div className="relative flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
        {lanes
          ? lanes.map(([assignee, tasks]) => (
              <div className="flex flex-col gap-2" key={assignee}>
                <div className="flex items-center gap-1.5 px-1 pt-1 text-[0.625rem] text-(--ui-text-quaternary)">
                  {assignee !== UNASSIGNED_LANE && <Avatar name={assignee} size="0.875rem" />}
                  {assignee}
                  <span className="tabular-nums">{tasks.length}</span>
                </div>
                {tasks.map(task => (
                  <Card
                    columns={columns}
                    key={taskCardKey(task)}
                    onDelete={onDelete}
                    onMove={onMove}
                    onOpen={onOpen}
                    onSetPriority={onSetPriority}
                    onToggleSelect={onToggleSelect}
                    selected={selected.has(taskCardKey(task))}
                    task={task}
                  />
                ))}
              </div>
            ))
          : column.tasks.map(task => (
              <Card
                columns={columns}
                key={taskCardKey(task)}
                onDelete={onDelete}
                onMove={onMove}
                onOpen={onOpen}
                onSetPriority={onSetPriority}
                onToggleSelect={onToggleSelect}
                selected={selected.has(taskCardKey(task))}
                task={task}
              />
            ))}
        {/* Jira-style lane add — dashed, faded in on lane hover. Opacity (not
            display) so it always holds its slot and never thrashes layout.
            Locked lanes get none: you can't create into a system state. The
            wishlist lanes get none either — the board header's capture dialog
            is the ONE way a card enters Ideas (there is no create-into-lane
            endpoint, and a second capture UI was explicitly ruled out). */}
        {!locked && !isRoadmapLane(column.name) && (
          <button
            aria-label={k.newTaskIn(label)}
            className="flex shrink-0 items-center justify-center rounded-md border border-dashed border-(--ui-stroke-secondary) py-1.5 text-(--ui-text-tertiary) opacity-0 transition-[opacity,color,border-color] group-hover/col:opacity-100 hover:border-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100"
            onClick={() => onAdd(column.name)}
            type="button"
          >
            <Codicon name="add" size="0.8rem" />
          </button>
        )}
        {column.tasks.length === 0 && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center text-[0.6875rem] text-(--ui-text-quaternary)">
            {k.empty}
          </div>
        )}
      </div>
    </div>
  )
}

// ── dialogs ──────────────────────────────────────────────────────────────────

const NO_PARENT = '__none__'
const PARKED = '__parked__'
const WORKSPACE_KINDS = ['scratch', 'worktree', 'dir'] as const

function Field({ children, label }: { children: ReactNode; label: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className={FIELD_LABEL}>{label}</span>
      {children}
    </label>
  )
}

// One image pasted into the new-task dialog before the task exists — staged
// server-side immediately (see api.ts's stageAttachment), previewed locally
// via an object URL, and promoted into a real attachment on submit via its
// `token`. `blob` is kept so a board switch in All Boards mode can re-stage
// the same bytes against the newly chosen board (staged blobs live in the
// target board's own staging DB, so a token from board A never promotes on
// board B).
interface PendingImage {
  token: string
  filename: string
  previewUrl: string
  size: number
  blob: Blob
  /** The board this token is staged against ('' = the server's active board,
   *  matching `boardPath`'s "no board param" fallback). */
  board: string
}

export function NewTaskDialog({
  onClose,
  parents,
  target
}: {
  onClose: () => void
  /** Candidate parent tasks. Each carries its own `board` in All Boards mode
   *  (absent in single-board mode) so the picker can offer only parents on the
   *  board the new card will actually be created on — a link across boards is
   *  rejected by the backend, which owns one board's DB per request. */
  parents: Array<{ id: string; title: string; board?: null | string }>
  target: null | string
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })
  // Title-only creates must RUN: "auto" resolves to the orchestration default
  // (ultimately the active profile), applied at create time. Never silently
  // unassigned — parking a card is the explicit choice, not the default.
  const resolvedDefault = useOrchestration()?.resolved_default_assignee || 'default'

  // Board-level workspace default: a task inherits the current board's
  // configured project dir (scratch when unset, worktree in a git repo, else
  // dir) unless the operator overrides it below. Set the board default in the
  // board switcher's "Board settings…".
  const selectedSlug = useValue($boardSlug)
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })

  // In All Boards mode `$boardSlug` is the sentinel, which resolves to NO
  // board on the wire — the server would then silently create the card on
  // whatever board is active. So the dialog asks: an explicit picker, defaulted
  // to the server's own current board, and the chosen slug is threaded through
  // every write below (create, the follow-up status patch, and image staging).
  const isAllBoards = selectedSlug === ALL_BOARDS
  const [targetBoard, setTargetBoard] = useState('')
  // The board every write in this dialog goes to. Outside All Boards mode this
  // stays `undefined`, so `boardPath` falls through to `$boardSlug` exactly as
  // it always did — single-board behavior is byte-for-byte unchanged.
  const writeBoard = isAllBoards ? targetBoard : undefined
  const effectiveSlug = isAllBoards ? targetBoard : selectedSlug || boards?.current || ''
  const currentBoard = boards?.boards.find(b => b.slug === (effectiveSlug || boards.current))
  const boardDefaultKind = currentBoard?.default_workspace_kind || 'scratch'
  const boardDefaultDir = currentBoard?.default_workdir || ''

  // Parents must live on the board the card is created on — the backend link
  // write sees one board's DB. In single-board mode nothing carries a `board`
  // and every option stays offered, exactly as before.
  const parentOptions = useMemo(
    () => (isAllBoards ? parents.filter(option => (option.board ?? '') === targetBoard) : parents),
    [isAllBoards, parents, targetBoard]
  )

  const isTriage = target === 'triage'
  const [title, setTitle] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [assignee, setAssignee] = useState('')
  const [priority, setPriority] = useState(0)
  const [skills, setSkills] = useState('')
  const [workspaceKind, setWorkspaceKind] = useState<string>(boardDefaultKind)
  // Empty = inherit the board's default project dir (backend resolves it);
  // a path here overrides just this task. Only meaningful for dir/worktree.
  const [workspacePath, setWorkspacePath] = useState('')
  const [parent, setParent] = useState('')
  const [modelOverride, setModelOverride] = useState<TaskModelOverride>(EMPTY_OVERRIDE)
  const [goalMode, setGoalMode] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)
  const [estimate, setEstimate] = useState<null | TaskEstimate>(null)
  // Images pasted (Cmd/Ctrl+V) into the dialog before the task exists — each
  // is uploaded to the staging endpoint immediately so the create-task call
  // only ever carries small tokens, never raw bytes. `uploading` tracks
  // in-flight paste uploads so the create button can wait for them.
  const [pendingImages, setPendingImages] = useState<PendingImage[]>([])
  const [uploadingImages, setUploadingImages] = useState(0)
  const pendingImagesRef = useRef<PendingImage[]>([])
  pendingImagesRef.current = pendingImages

  // Rough effort estimate from the typed title/body (before the task exists),
  // via the auto-routed auxiliary model. Makes a model call — explicit action.
  const estMut = useMutation({
    mutationFn: () => estimateNew(title.trim(), bodyText.trim()),
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: r => {
      if (r.ok) {
        setEstimate(r)
      } else {
        host.notify({ kind: 'warning', message: r.reason || k.couldNotEstimate })
      }
    }
  })

  // Reset per open — the dialog is externally controlled (open = target set),
  // so onOpenChange(true) never fires; key the reset off `target` (and the
  // resolved board default, which may arrive after the first open).
  useEffect(() => {
    if (target) {
      setTitle('')
      setBodyText('')
      setAssignee('')
      setPriority(0)
      setSkills('')
      setWorkspaceKind(boardDefaultKind)
      setWorkspacePath('')
      setParent('')
      setModelOverride(EMPTY_OVERRIDE)
      setGoalMode(false)
      setError(null)
      setBusy(false)
      setEstimate(null)
      setPendingImages([])
      setUploadingImages(0)
    }
  }, [target, boardDefaultKind])

  // Default the All Boards picker to the server's own current board, so the
  // pre-selected target matches what a single-board create would have done —
  // the difference is that it is now VISIBLE and changeable, never silent.
  // Only while the dialog is open, and only until the user picks something.
  const serverCurrent = boards?.current ?? ''

  useEffect(() => {
    if (target && isAllBoards && !targetBoard && serverCurrent) {
      setTargetBoard(serverCurrent)
    }
  }, [target, isAllBoards, targetBoard, serverCurrent])

  // Best-effort cleanup for images pasted but never submitted: revoke the
  // local object URLs (avoid leaking blob: refs) and delete the staged
  // blobs server-side. Not required for correctness — the TTL reaper cleans
  // up abandoned staged uploads regardless — but keeps the board tidy
  // immediately. Fire-and-forget: a failure here shouldn't block closing.
  const cleanupPending = () => {
    for (const image of pendingImagesRef.current) {
      URL.revokeObjectURL(image.previewUrl)
      deleteStagedAttachment(image.token, image.board || undefined).catch(() => undefined)
    }
  }

  const handleClose = () => {
    cleanupPending()
    onClose()
  }

  /** Stage one image's bytes against `board` and return the pending row. */
  const stageImage = (blob: Blob, filename: string, previewUrl: string, board: string) =>
    blob
      .arrayBuffer()
      .then(bytes => stageAttachment({ bytes, contentType: blob.type || undefined, filename }, board || undefined))
      .then(({ attachment }) => ({
        blob,
        board,
        filename: attachment.filename,
        previewUrl,
        size: attachment.size,
        token: attachment.token
      }))

  // Paste handler: pull image items off the clipboard, upload each straight
  // to the staging endpoint (before Create is ever clicked), and show a
  // thumbnail immediately. Non-image clipboard data (plain text, etc.) is
  // left alone so normal paste-into-textarea keeps working.
  const handlePaste = (event: ReactClipboardEvent<HTMLTextAreaElement>) => {
    const items = Array.from(event.clipboardData?.items ?? []).filter(item => item.type.startsWith('image/'))

    if (items.length === 0) {
      return
    }

    event.preventDefault()

    for (const item of items) {
      const blob = item.getAsFile()

      if (!blob) {
        continue
      }

      const previewUrl = URL.createObjectURL(blob)

      const filename =
        blob.name || `pasted-image-${Date.now()}.${(blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg')}`

      setUploadingImages(count => count + 1)

      stageImage(blob, filename, previewUrl, writeBoard ?? '')
        .then(image => setPendingImages(images => [...images, image]))
        .catch(err => {
          URL.revokeObjectURL(previewUrl)
          host.notify({ kind: 'error', message: `${k.imagePasteFailed}: ${errText(err)}` })
        })
        .finally(() => setUploadingImages(count => count - 1))
    }
  }

  // A staged blob lives in ITS board's staging DB, so switching the target
  // board after pasting would leave the token unresolvable at promotion —
  // the image would vanish from the created card with only a warning. Re-stage
  // the bytes we still hold against the new board and drop the old token.
  useEffect(() => {
    if (!target || !isAllBoards || !targetBoard) {
      return
    }

    const stale = pendingImagesRef.current.filter(image => image.board !== targetBoard)

    if (stale.length === 0) {
      return
    }

    for (const image of stale) {
      setUploadingImages(count => count + 1)

      stageImage(image.blob, image.filename, image.previewUrl, targetBoard)
        .then(restaged => {
          deleteStagedAttachment(image.token, image.board || undefined).catch(() => undefined)
          setPendingImages(images => images.map(candidate => (candidate.token === image.token ? restaged : candidate)))
        })
        .catch(err => host.notify({ kind: 'error', message: `${k.imagePasteFailed}: ${errText(err)}` }))
        .finally(() => setUploadingImages(count => count - 1))
    }
    // `stageImage` closes over nothing that changes per render besides the
    // board it is passed explicitly; re-running on every render would re-stage
    // in a loop. Keyed strictly on the board actually switching.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, isAllBoards, targetBoard])

  const removePendingImage = (token: string) => {
    const image = pendingImages.find(candidate => candidate.token === token)

    if (image) {
      URL.revokeObjectURL(image.previewUrl)
    }

    setPendingImages(images => images.filter(candidate => candidate.token !== token))
    deleteStagedAttachment(token, image?.board || undefined).catch(() => undefined)
  }

  // A parent chosen before the board switched now belongs to another board and
  // would be rejected as a link target. Drop it rather than sending it.
  useEffect(() => {
    if (parent && !parentOptions.some(option => option.id === parent)) {
      setParent('')
    }
  }, [parent, parentOptions])

  const submit = async () => {
    const trimmed = title.trim()

    if (!trimmed || !target || busy) {
      return
    }

    // Never create without a resolved board in All Boards mode: the sentinel
    // carries no board and the server would pick the active one silently.
    if (isAllBoards && !targetBoard) {
      setError(k.pickBoard)

      return
    }

    setBusy(true)
    setError(null)

    try {
      const skillList = skills
        .split(',')
        .map(s => s.trim())
        .filter(Boolean)

      // create() derives status (triage flag → 'triage', else 'ready'); move to
      // the requested column when they differ, so a per-column add lands right.
      // `writeBoard` pins BOTH writes to the board the user picked; it is
      // `undefined` outside All Boards mode, where `$boardSlug` still decides.
      const { task, warning } = await createTask(
        {
          assignee: assignee === PARKED ? undefined : assignee || resolvedDefault,
          body: bodyText.trim() || undefined,
          goal_mode: goalMode,
          parents: parent ? [parent] : undefined,
          // Images travel exclusively as staged tokens, never inlined into
          // `body` — the backend promotes each token into a real attachment.
          pending_attachment_tokens: pendingImages.length ? pendingImages.map(image => image.token) : undefined,
          priority,
          skills: skillList.length ? skillList : undefined,
          title: trimmed,
          triage: isTriage,
          workspace_kind: workspaceKind,
          ...overrideCreateFields(modelOverride),
          // Empty → backend inherits the board's default project dir.
          workspace_path: workspaceKind !== 'scratch' && workspacePath.trim() ? workspacePath.trim() : undefined
        },
        writeBoard
      )

      if (task && task.status !== target) {
        await patchTask(task.id, { status: target }, writeBoard)
      }

      // Dispatcher-presence warning ("this ready task will sit idle") — not an
      // error, but the user should know.
      if (warning) {
        host.notify({ kind: 'warning', message: warning })
      }

      await qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      // Submitted images are now real attachments — clear without re-deleting
      // the (already-promoted) staged blobs.
      setPendingImages([])
      onClose()
    } catch (err) {
      setError(errText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && handleClose()} open={Boolean(target)}>
      {/* `overflow-visible`: DialogContent publishes ITSELF as the portal
          container for popovers opened inside it (dialog-portal-context), and
          its default `overflow-y-auto` then crops them at the dialog's edge —
          the model menu below is born inside that scroll box. This dialog
          already owns a scroller on its body div, so the shell's clip is
          redundant here and dropping it is safe. The general fix to
          DialogContent is in flight as #75600; when that lands this override
          becomes a no-op and can go. */}
      <DialogContent className="w-[min(42rem,94vw)] max-w-none overflow-visible">
        <DialogHeader>
          <DialogTitle>{target ? k.newTaskIn(columnLabel(k, target)) : k.newTask}</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-[min(72vh,44rem)] flex-col gap-3 overflow-y-auto pr-0.5">
          {/* All Boards mode has no implied board — ask, defaulted to the
              server's current one, rather than letting the create resolve
              silently to whatever board happens to be active. */}
          {isAllBoards && (
            <Field label={k.board}>
              <Select onValueChange={setTargetBoard} value={targetBoard}>
                <SelectTrigger>
                  <SelectValue placeholder={k.pickBoard} />
                </SelectTrigger>
                <SelectContent>
                  {(boards?.boards ?? []).map(option => (
                    <SelectItem key={option.slug} value={option.slug}>
                      {option.name || option.slug}
                      {option.slug === serverCurrent ? k.boardDefaultSuffix : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.pickBoardHint}</span>
            </Field>
          )}
          <Input
            autoFocus
            onChange={event => setTitle(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              }
            }}
            placeholder={isTriage ? k.titlePlaceholderTriage : k.titlePlaceholder}
            value={title}
          />
          <Textarea
            className="min-h-20"
            onChange={event => setBodyText(event.target.value)}
            onPaste={handlePaste}
            placeholder={k.descPlaceholder}
            value={bodyText}
          />

          {(pendingImages.length > 0 || uploadingImages > 0) && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                {k.pastedImages(pendingImages.length + uploadingImages)}
              </span>
              <div className="flex flex-wrap gap-2">
                {pendingImages.map(image => (
                  <div
                    className="group relative h-16 w-16 overflow-hidden rounded-md border border-(--ui-border)"
                    key={image.token}
                  >
                    <img alt={image.filename} className="h-full w-full object-cover" src={image.previewUrl} />
                    <Button
                      aria-label={k.removeImage}
                      className="absolute top-0.5 right-0.5 h-4 w-4 opacity-0 group-hover:opacity-100"
                      onClick={() => removePendingImage(image.token)}
                      size="icon-xs"
                      variant="destructive"
                    >
                      <Codicon name="close" size="0.6rem" />
                    </Button>
                  </div>
                ))}
                {Array.from({ length: uploadingImages }).map((_, index) => (
                  <div
                    className="flex h-16 w-16 items-center justify-center rounded-md border border-(--ui-border) border-dashed"
                    key={`uploading-${index}`}
                  >
                    <Codicon name="loading" size="1rem" spinning />
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <Field label={k.priority}>
              <PriorityPicker onChange={setPriority} priority={priority} />
            </Field>
            <Field label={k.workspace}>
              <Select onValueChange={setWorkspaceKind} value={workspaceKind}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WORKSPACE_KINDS.map(kind => (
                    <SelectItem key={kind} value={kind}>
                      {kind}
                      {kind === boardDefaultKind ? k.boardDefaultSuffix : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          {workspaceKind !== 'scratch' && (
            <Field label={k.workspaceOverride}>
              <Input
                onChange={event => setWorkspacePath(event.target.value)}
                placeholder={boardDefaultDir || k.workspaceInherit}
                value={workspacePath}
              />
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                {boardDefaultDir ? k.workspaceInheritDir(boardDefaultDir) : k.workspaceInheritGeneric}
              </span>
            </Field>
          )}

          <Field label={k.assignee}>
            <Select onValueChange={v => setAssignee(v === NO_PARENT ? '' : v)} value={assignee || NO_PARENT}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PARENT}>{k.defaultOption(resolvedDefault)}</SelectItem>
                {(roster?.profiles ?? [])
                  .filter(profile => profile.name !== resolvedDefault)
                  .map(profile => (
                    <SelectItem key={profile.name} value={profile.name}>
                      {profile.name}
                    </SelectItem>
                  ))}
                <SelectItem value={PARKED}>{k.parkedOption}</SelectItem>
              </SelectContent>
            </Select>
          </Field>

          <Field label={k.skills}>
            <Input onChange={event => setSkills(event.target.value)} placeholder={k.skillsPlaceholder} value={skills} />
          </Field>

          <Field label={k.model}>
            <ModelOverrideField onChange={setModelOverride} value={modelOverride} />
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">{k.modelHint}</span>
          </Field>

          {parentOptions.length > 0 && (
            <Field label={k.parent}>
              <Select onValueChange={v => setParent(v === NO_PARENT ? '' : v)} value={parent || NO_PARENT}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_PARENT}>{k.noParent}</SelectItem>
                  {parentOptions.map(option => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.title || option.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          )}

          <label className="flex cursor-pointer items-center gap-2 text-[0.75rem] text-(--ui-text-secondary)">
            <Switch aria-label={k.goalMode} checked={goalMode} onCheckedChange={setGoalMode} size="xs" />
            {k.goalMode}
          </label>

          {error && <span className="text-[0.75rem] text-destructive">{error}</span>}
        </div>
        <DialogFooter>
          <div className="mr-auto flex items-center gap-1 text-[0.75rem] text-(--ui-text-tertiary)">
            {estimate?.ok ? (
              <>
                <Tip label={estimate.rationale || k.roughEstimate}>
                  <span className="font-medium tabular-nums text-(--ui-text-secondary)">
                    ~{compactNumber(estimate.est_tokens)} {k.tokUnit}
                    {estimate.complexity ? ` · ${k.complexity[estimate.complexity] ?? estimate.complexity}` : ''}
                  </span>
                </Tip>
                <Tip label={k.reEstimate}>
                  <Button
                    aria-label={k.reEstimate}
                    disabled={!title.trim() || estMut.isPending}
                    onClick={() => estMut.mutate()}
                    size="icon-xs"
                    variant="ghost"
                  >
                    <Codicon name="refresh" size="0.7rem" spinning={estMut.isPending} />
                  </Button>
                </Tip>
              </>
            ) : (
              <Tip label={k.estimateTip}>
                <Button
                  disabled={!title.trim() || estMut.isPending}
                  onClick={() => estMut.mutate()}
                  size="xs"
                  variant="ghost"
                >
                  <Codicon
                    name={estMut.isPending ? 'loading' : 'dashboard'}
                    size="0.75rem"
                    spinning={estMut.isPending}
                  />
                  {estMut.isPending ? k.estimating : k.estimate}
                </Button>
              </Tip>
            )}
          </div>
          <Button onClick={handleClose} variant="text">
            {k.cancel}
          </Button>
          <Button
            disabled={!title.trim() || busy || uploadingImages > 0 || (isAllBoards && !targetBoard)}
            onClick={() => void submit()}
          >
            {busy ? k.creating : k.createTask}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
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

// ── idea capture (Phase 2.15) ───────────────────────────────────────────────

/**
 * Free-typed roadmap idea capture — jot a rough idea straight from the board
 * into a card in the board's `idea` lane, without opening an editor or
 * filing a premature card. A rejected/unavailable roadmap is reported
 * distinctly from success (`k.ideaUnavailable` vs. `k.ideaSaved`) per the
 * card's acceptance criteria. On success this invalidates the board query
 * prefix so the new card shows up immediately, including in All Boards mode.
 */
export function IdeaCaptureDialog({ onClose, open }: { onClose: () => void; open: boolean }) {
  const k = useKanban()
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    if (open) {
      setText('')
      setBusy(false)
      setError(null)
    }
  }, [open])

  const submit = async () => {
    const trimmed = text.trim()

    if (!trimmed || busy) {
      return
    }

    setBusy(true)
    setError(null)

    try {
      const { ok, reason } = await addRoadmapIdea(trimmed)

      if (ok) {
        host.notify({ kind: 'success', message: k.ideaSaved })
        void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
        onClose()
      } else {
        // Distinct from a thrown error: the request succeeded, the idea card
        // creation did not (missing/unavailable roadmap lane, empty after
        // sanitization) — surface it inline so the user can decide whether
        // to retry rather than silently losing the idea.
        setError(reason === 'empty_idea' ? k.ideaEmpty : k.ideaUnavailable)
        setBusy(false)
      }
    } catch (err) {
      setError(errText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog onOpenChange={next => !next && onClose()} open={open}>
      <DialogContent className="w-[min(28rem,94vw)]">
        <DialogHeader>
          <DialogTitle>{k.ideaTitle}</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <p className="text-xs text-(--ui-text-tertiary)">{k.ideaHint}</p>
          <Textarea
            autoFocus
            className="min-h-24"
            maxLength={300}
            onChange={e => setText(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault()
                void submit()
              }
            }}
            placeholder={k.ideaPlaceholder}
            value={text}
          />
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button onClick={onClose} size="sm" variant="ghost">
            {k.cancel}
          </Button>
          <Button disabled={!text.trim() || busy} onClick={() => void submit()} size="sm">
            {busy ? k.ideaSaving : k.ideaSave}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── selection bar ────────────────────────────────────────────────────────────

/**
 * Floating bulk-actions bar, shown while cards are ⌘-selected. Deliberately
 * leaner than the dashboard's always-on toolbar: move / assign / archive /
 * delete cover the real fleet chores (requeue a batch, archive a sweep of
 * done, reassign after a profile change) via POST /tasks/bulk, which applies
 * per-id and reports partial failures — failed cards stay selected.
 */
function SelectionBar({
  columns,
  index,
  onClear,
  onDone,
  selected
}: {
  columns: string[]
  /** cardKey→task (see `indexBoard`); `selected` holds cardKeys. */
  index: Map<string, KanbanTask>
  onClear: () => void
  onDone: (failed: string[]) => void
  selected: ReadonlySet<string>
}) {
  const k = useKanban()
  const qc = useQueryClient()
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })

  const finish = (failed: Array<{ error?: string; key: string }>) => {
    void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })

    if (failed.length > 0) {
      host.notify({
        kind: 'warning',
        message: k.bulkFailed(failed.length, selected.size, failed[0].error ?? k.refused)
      })
    }

    onDone(failed.map(f => f.key))
  }

  // Group the selection by its OWN board (populated only in All Boards mode;
  // single-board mode's tasks carry no `board`, so everything lands in one
  // group under `undefined` — byte-identical to the pre-existing single call).
  // `/tasks/bulk` is a single-board endpoint, so a selection spanning boards
  // fans out to one call per board rather than sending a foreign id.
  //
  // Selection holds `cardKey`s; the wire wants bare ids, so each group carries
  // both and the per-id results are mapped back to their key by the pair.
  const byBoard = (keys: string[]): Map<string | undefined, string[]> => {
    const groups = new Map<string | undefined, string[]>()

    for (const key of keys) {
      const taskBoard = index.get(key)?.board ?? parseCardKey(key).board ?? undefined
      const bucket = groups.get(taskBoard)

      bucket ? bucket.push(key) : groups.set(taskBoard, [key])
    }

    return groups
  }

  const bulk = useMutation({
    mutationFn: async (patch: Record<string, unknown>) => {
      const groups = byBoard([...selected])

      const results = await Promise.all(
        [...groups.entries()].map(async ([taskBoard, keys]) => {
          const { results } = await bulkTasks(
            keys.map(key => parseCardKey(key).id),
            patch,
            taskBoard
          )

          // The backend answers per bare id; re-attach the board so a failure
          // is reported against the exact card the user selected.
          return results.map(row => ({ ...row, key: cardKey(row.id, taskBoard) }))
        })
      )

      return { results: results.flat() }
    },
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: data => finish(data.results.filter(r => !r.ok))
  })

  // No bulk-delete on the backend — fan out per id, same partial-failure story.
  const bulkDelete = useMutation({
    mutationFn: async () => {
      const keys = [...selected]

      const settled = await Promise.allSettled(
        keys.map(key => {
          const { board: keyBoard, id } = parseCardKey(key)

          return deleteTask(id, index.get(key)?.board ?? keyBoard ?? undefined)
        })
      )

      return keys.flatMap((key, i) => {
        const result = settled[i]

        return result.status === 'rejected' ? [{ error: errText(result.reason), key }] : []
      })
    },
    onSuccess: finish
  })

  const busy = bulk.isPending || bulkDelete.isPending
  // One menu at a time — controlled, so a click on the second trigger can
  // never race Radix's dismiss layer into two open menus.
  const [menu, setMenu] = useState<'assign' | 'move' | null>(null)

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-4 z-10 flex justify-center px-4">
      {/* Flat overlay: stroke + elevated surface do the separating, no shadow. */}
      <div className="pointer-events-auto flex items-center gap-1 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) py-1 pr-1 pl-3">
        <span className="mr-1 text-xs tabular-nums text-(--ui-text-secondary)">{k.nSelected(selected.size)}</span>

        <DropdownMenu onOpenChange={open => setMenu(open ? 'move' : null)} open={menu === 'move'}>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy} size="xs" variant="ghost">
              {k.moveToShort}
              <Codicon name="chevron-down" size="0.7rem" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            {columns
              // Bulk selection can span mixed statuses. A target is safe only
              // when the shared transition predicate accepts it for EVERY
              // selected card. Wishlist exits stay per-card because Ready
              // requires a confirmation and the bulk endpoint has no dialog
              // contract for partially accepted lane spawns.
              .filter(
                name =>
                  !isLockedTarget(name) &&
                  [...selected].every(key => {
                    const task = index.get(key)

                    return Boolean(task && !isRoadmapLane(task.status) && laneDropAllowed(task.status, name))
                  })
              )
              .map(name => (
                <DropdownMenuItem key={name} onSelect={() => bulk.mutate({ status: name })}>
                  <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
                  {columnLabel(k, name)}
                </DropdownMenuItem>
              ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <DropdownMenu onOpenChange={open => setMenu(open ? 'assign' : null)} open={menu === 'assign'}>
          <DropdownMenuTrigger asChild>
            <Button disabled={busy} size="xs" variant="ghost">
              {k.assign}
              <Codicon name="chevron-down" size="0.7rem" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="center">
            {(roster?.profiles ?? []).map(profile => (
              <DropdownMenuItem
                key={profile.name}
                onSelect={() => bulk.mutate({ assignee: profile.name, reclaim_first: true })}
              >
                <Avatar name={profile.name} size="0.875rem" />
                {profile.name}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => bulk.mutate({ assignee: '', reclaim_first: true })}>
              {k.unassignAction}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <Button disabled={busy} onClick={() => bulk.mutate({ archive: true })} size="xs" variant="ghost">
          {k.archive}
        </Button>
        <Button
          className="text-destructive"
          disabled={busy}
          onClick={() => bulkDelete.mutate()}
          size="xs"
          variant="ghost"
        >
          {k.delete}
        </Button>

        <Tip label={k.clearSelection}>
          <Button aria-label={k.clearSelection} onClick={onClear} size="icon-xs" variant="ghost">
            <Codicon name="close" size="0.8rem" />
          </Button>
        </Tip>
      </div>
    </div>
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

/** Board-scoped completed-card cleanup. The backend remains authoritative for
 * the candidate set: the preflight only enables the affordance and gives the
 * confirmation its honest count, while the mutation re-checks `done` per card.
 */
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
        onConfirm={async () => {
          await archive.mutateAsync()
        }}
        open={open}
        title={k.archiveDone}
      />
    </>
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

  // One hop only (see focusSets) — a transitive closure lights up most of a
  // busy board and defeats the dimming.
  const chain = useMemo(
    () => (focused ? focusSets(graph, focused) : { downstream: EMPTY_IDS, upstream: EMPTY_IDS }),
    [graph, focused]
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
  const [pendingLoopMove, setPendingLoopMove] = useState<null | { key: string; id: string; status: string; board?: string; title: string }>(null)

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

          {/* Focus-mode hint. Only while a trace is live, so the board chrome is
          unchanged in the common case. Its own row rather than an overlay:
          the board is dimmed underneath and an overlay would compete with the
          selection bar for the same corner. */}
          {focused && (
            <div className="mx-4 mb-2 flex shrink-0 items-center gap-2 rounded-lg bg-(--ui-bg-quinary) px-3 py-1.5 text-[0.6875rem] text-(--ui-text-secondary)">
              <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="references" size="0.8rem" />
              <span className="min-w-0 truncate">{k.depFocusHint}</span>
              <Button className="ml-auto shrink-0" onClick={() => setFocused(null)} size="xs" variant="ghost">
                <Codicon name="close" size="0.7rem" />
                {k.depClearFocus}
              </Button>
            </div>
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
              className={cn('flex min-h-0 flex-1 gap-2 overflow-x-auto px-4 pt-1 pb-3', grabbing && 'cursor-grabbing')}
              // Clicking the board background clears the trace — the gaps between
              // lanes, a lane's padding, a lane header, empty column space. Keyed
              // off "the click did not land on a card" rather than a strict
              // `currentTarget` check, which would only catch the thin gutters.
              // Cards are the draggable nodes (same vocabulary useGrabScroll uses),
              // so a click on a card — including its own trace button — is left to
              // the card's own handler.
              onClickCapture={event => {
                if (focused && !(event.target as HTMLElement).closest('[draggable="true"]')) {
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
            description={k.blockLoopConfirmBody(pendingLoopMove?.title ?? '', columnLabel(k, pendingLoopMove?.status ?? ''))}
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
