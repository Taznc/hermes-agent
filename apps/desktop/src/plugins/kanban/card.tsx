/**
 * The card/column render cluster: `Card` (with its private footer/thumb/badge
 * helpers) and `Column` (with its collapsed-rail variant and per-assignee lane
 * grouping). Split out of `board.tsx` (Fork-anchor extraction phase 4b) —
 * everything here is pure rendering driven by callbacks and the two board-wide
 * contexts (`useDependencies` from `dependency-view.tsx`, `useBoardInfo` from
 * this file), never by `KanbanBoardPage`'s own state directly.
 */

import {
  cn,
  Codicon,
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
  formatModifierToken,
  host,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import {
  createContext,
  type CSSProperties,
  type DragEvent as ReactDragEvent,
  type ReactNode,
  useContext,
  useMemo,
  useState
} from 'react'

import { $lanesByProfile, addRoadmapIdea, fetchAttachmentDataUrl } from './api'
import { focusRole, hasDependencies, PROMOTABLE_STATUSES, useDependencies } from './dependency-view'
import { blockerStand, downstreamOf, taskCardKey, upstreamOf } from './deps'
import { PriorityPicker } from './priority-picker'
import { type BoardAllInfo, columnMeta, isRoadmapLane, type KanbanTask, laneDropAllowed } from './types'
import {
  ago,
  type ArcState,
  arcState,
  Avatar,
  columnHelp,
  columnLabel,
  errText,
  IdChip,
  isLockedTarget,
  RunClock,
  useDefaultAssignee,
  useKanban,
  useOrchestration
} from './ui'

// ── board attribution (All Boards mode only) ──────────────────────────────────

export const EMPTY_BOARD_INFO: readonly BoardAllInfo[] = []

/** Per-board display chrome (name/color/icon), keyed by slug — populated only
 *  in the consolidated All Boards view so `Card` can render a board badge.
 *  `null` in single-board mode: cards never need attribution against
 *  themselves, and `Card` skips the badge entirely when this is null.
 *  Exported so `BoardBadge` is testable by wrapping it in a provider without
 *  mounting the whole page. */
export const BoardInfoContext = createContext<Map<string, BoardAllInfo> | null>(null)

const useBoardInfo = () => useContext(BoardInfoContext)

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
          data-card-key={key}
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
          {/* Sibling affordance: the same trace, drawn as a graph. Sits left of
              the trace button so the two read as one hover group. */}
          {linked && (
            <Tip label={k.depGraphHint}>
              <button
                aria-label={k.depGraphHint}
                className="absolute top-1.5 right-7 grid size-5 place-items-center rounded text-(--ui-text-quaternary) opacity-0 transition-opacity group-hover:opacity-100 hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100"
                onClick={event => {
                  event.stopPropagation()
                  deps.onOpenGraph(key)
                }}
                type="button"
              >
                <Codicon name="type-hierarchy" size="0.8rem" />
              </button>
            </Tip>
          )}
          <span
            className={cn(
              'line-clamp-2 text-[0.8125rem] font-medium leading-snug text-foreground',
              // Keep the title clear of the dependency focus affordances. Static
              // per task, so hover never reflows the card.
              linked && 'pr-11'
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

const UNASSIGNED_LANE = 'unassigned'

export function Column({
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
      <div className="relative flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto" data-lane-scroller>
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
