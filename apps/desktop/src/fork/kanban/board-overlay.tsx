/**
 * The board-level half of focus mode + All Boards, mounted by ONE anchor in
 * upstream's `KanbanBoardPage` (`host.fork.kanban.boardOverlay`), right above
 * the lane strip. It owns everything that is per-board rather than per-card:
 *
 *  - the fork-backend probe, and the dependency edges (`/link-edges` for one
 *    board; the merged payload already carries them for All Boards);
 *  - building the graph ONCE per payload and publishing the trace to the
 *    card frames (`$depView`);
 *  - the answer bar, the arrows layer (portalled into upstream's strip so it
 *    pans with the lanes), Esc / click-off-to-clear;
 *  - the lane fold: reading each lane's rendered order from the DOM (upstream
 *    groups/filters lanes its own way) and publishing `$folds`;
 *  - All Boards: board filter chips, the partial-failure notice, and one live
 *    socket per board;
 *  - the dispatch-paused banner (`dispatch-pause.tsx`).
 *
 * With no fork backend it renders nothing and publishes nothing.
 */

import { useStore as useValue } from '@nanostores/react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type RefObject, useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { ALL_BOARDS, type AllBoardsInfo, type AllBoardsPayload, fetchLinkEdges, leaveAllBoards, watchAllBoards } from '@/fork/kanban/all-boards'
import { FocusAnswerBar } from '@/fork/kanban/answer-bar'
import { useForkBackend } from '@/fork/kanban/backend'
import { $hotEdge, BoardDependencyArrows } from '@/fork/kanban/board-arrows-layer'
import { FOLD_KEY_ATTR, LANE_GAP_ATTR } from '@/fork/kanban/card-frame'
import { buildGraph, chainSets, focusSets, indexBoard } from '@/fork/kanban/deps'
import { DispatchPausedNotice } from '@/fork/kanban/dispatch-pause'
import { foldLane } from '@/fork/kanban/lane-fold'
import {
  $depView,
  $focusDepth,
  $focused,
  $folds,
  $hiddenBoards,
  $openGaps,
  $selectedKeys,
  EMPTY_IDS,
  type FoldSlot,
  NO_DEPENDENCIES,
  NO_FOLDS,
  resetFocus,
  toggleBoardHidden
} from '@/fork/kanban/state'
import { useKanban } from '@/fork/kanban/text'
import type { KanbanBoard } from '@/fork/kanban/types'
import { cn } from '@/lib/utils'
import { $activeConnectionId } from '@/store/connections'

/** Anything open on top of the board owns Esc (drawer, dialogs, menus). */
const LAYER_OPEN = '[role="dialog"], [role="alertdialog"], [role="menu"]'

function sameFolds(a: ReadonlyMap<string, FoldSlot>, b: ReadonlyMap<string, FoldSlot>): boolean {
  if (a.size !== b.size) {
    return false
  }

  for (const [key, slot] of a) {
    const other = b.get(key)

    if (!other || other.kind !== slot.kind) {
      return false
    }

    if (slot.kind === 'gap' && other.kind === 'gap' && (slot.count !== other.count || slot.gap !== other.gap)) {
      return false
    }
  }

  return true
}

/** Fold every lane of the strip around the lit set, from the DOM order
 *  upstream actually rendered (frames and folded-away markers alike). */
function computeFolds(
  strip: HTMLElement,
  keep: (key: string) => boolean,
  open: ReadonlySet<string>
): Map<string, FoldSlot> {
  const lanes = new Map<Element, string[]>()

  for (const el of strip.querySelectorAll<HTMLElement>(`[data-card-key], [${FOLD_KEY_ATTR}]`)) {
    const key = el.getAttribute('data-card-key') ?? el.getAttribute(FOLD_KEY_ATTR)!
    const lane = el.parentElement

    if (lane) {
      lanes.set(lane, [...(lanes.get(lane) ?? []), key])
    }
  }

  const folds = new Map<string, FoldSlot>()

  for (const keys of lanes.values()) {
    for (const slot of foldLane(keys, key => key, keep, open)) {
      if (slot.kind === 'gap') {
        slot.items.forEach((key, i) =>
          folds.set(key, i === 0 ? { count: slot.items.length, gap: slot.id, kind: 'gap' } : { kind: 'hidden' })
        )
      }
    }
  }

  return folds
}

function BoardFilterChips({ boards }: { boards: AllBoardsInfo[] }) {
  const k = useKanban()
  const hidden = useValue($hiddenBoards)

  if (boards.length === 0) {
    return null
  }

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-1 px-4 pb-2" data-board-chips>
      {boards.map(info => {
        const visible = !hidden.has(info.slug)

        return (
          <Tip key={info.slug} label={k.boardChipTip(info.name || info.slug)}>
            <button
              aria-pressed={visible}
              className={cn(
                'flex items-center gap-1 rounded-full border px-2 py-px text-[0.625rem] transition-colors',
                visible
                  ? 'border-(--ui-stroke-secondary) text-(--ui-text-secondary)'
                  : 'border-dashed border-(--ui-stroke-tertiary) text-(--ui-text-quaternary)'
              )}
              data-board-chip={info.slug}
              onClick={() => toggleBoardHidden(info.slug)}
              type="button"
            >
              {info.color && <span className="size-1.5 rounded-full" style={{ backgroundColor: info.color }} />}
              {info.name || info.slug}
              <span className="tabular-nums text-(--ui-text-quaternary)">{info.task_count}</span>
            </button>
          </Tip>
        )
      })}
    </div>
  )
}

function BoardsErrorNotice({ errors }: { errors?: AllBoardsPayload['errors'] }) {
  const k = useKanban()

  if (!errors?.length) {
    return null
  }

  return (
    <div
      className="mx-4 mb-2 flex shrink-0 items-center gap-1.5 rounded-md bg-amber-500/10 px-2.5 py-1 text-[0.6875rem] text-amber-600 dark:text-amber-400"
      data-boards-error
      role="status"
    >
      <Codicon name="warning" size="0.75rem" />
      {k.boardsError(errors.length)}
      <span className="truncate text-(--ui-text-tertiary)">{errors.map(e => e.board).join(', ')}</span>
    </div>
  )
}

export function BoardOverlay({
  board,
  slug,
  stripRef
}: {
  board: KanbanBoard | undefined
  slug: string
  stripRef: RefObject<HTMLDivElement | null>
}) {
  const backend = useForkBackend()
  const qc = useQueryClient()
  const scope = useValue($activeConnectionId) ?? 'local'
  const focused = useValue($focused)
  const depth = useValue($focusDepth)
  const selected = useValue($selectedKeys)
  const openGaps = useValue($openGaps)
  const isAll = slug === ALL_BOARDS
  const on = backend === true
  const payload = board as AllBoardsPayload | undefined

  // A persisted All Boards selection on a backend without the fork plugin:
  // fall back to the server's current board, silently.
  useEffect(() => {
    if (backend === false && isAll) {
      leaveAllBoards()
    }
  }, [backend, isAll])

  // Single board: upstream's payload has no edges; the fork backend serves
  // them. Re-read whenever the board payload moves (a link is an event).
  const cardCount = board?.columns.reduce((sum, col) => sum + col.tasks.length, 0) ?? 0
  const tail = (board as { latest_event_id?: number } | undefined)?.latest_event_id ?? 0

  const { data: edges } = useQuery({
    enabled: on && !isAll && Boolean(board),
    queryFn: () => fetchLinkEdges(slug),
    queryKey: ['fork-kanban', 'edges', scope, slug, tail, cardCount],
    retry: false,
    staleTime: Infinity
  })

  const withEdges = useMemo<KanbanBoard | undefined>(() => {
    if (!on || !board) {
      return undefined
    }

    if (isAll) {
      return board
    }

    return edges ? { ...board, link_edges: edges } : undefined
  }, [board, edges, isAll, on])

  const hasEdges = Boolean(withEdges?.link_edges)
  const graph = useMemo(() => buildGraph(withEdges), [withEdges])
  const index = useMemo(() => indexBoard(board), [board])

  const chain = useMemo(
    () =>
      focused && hasEdges
        ? (depth === 'chain' ? chainSets : focusSets)(graph, focused)
        : { downstream: EMPTY_IDS, upstream: EMPTY_IDS },
    [depth, focused, graph, hasEdges]
  )

  // Publish the trace for the card frames — before paint, so a focus never
  // flashes undimmed.
  useLayoutEffect(() => {
    $depView.set(
      hasEdges
        ? { downstream: chain.downstream, focused, graph, hasEdges, index, upstream: chain.upstream }
        : NO_DEPENDENCIES
    )
  }, [chain, focused, graph, hasEdges, index])

  // A new board (or leaving the page) starts unfocused.
  useEffect(() => () => resetFocus(), [slug])

  // A focused card that left the board would strand every other card dimmed.
  useEffect(() => {
    if (focused && board && (!hasEdges || !index.has(focused))) {
      $focused.set(null)
    }
  }, [board, focused, hasEdges, index])

  // The strip is upstream's; hold it in state so the portal re-targets when
  // it (re)mounts (it only exists while the board shows cards). Re-read on
  // every payload and focus change — the moments a trace needs it.
  const [strip, setStrip] = useState<HTMLDivElement | null>(null)

  useLayoutEffect(() => {
    setStrip(stripRef.current)
  }, [board, focused, stripRef])

  // The arrows layer is positioned against the strip's scroll content; while
  // a trace is live the right gutter grows to fit the last lane's brackets.
  useLayoutEffect(() => {
    if (!strip || !on) {
      return
    }

    strip.style.position = 'relative'
    strip.style.paddingRight = focused ? '4rem' : ''

    return () => {
      strip.style.paddingRight = ''
    }
  }, [focused, on, strip])

  // Esc clears the focus, but only when it is the topmost dismissable thing:
  // an open drawer/dialog/menu and upstream's own selection own Esc first.
  useEffect(() => {
    if (!focused || selected.size > 0) {
      return
    }

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !document.querySelector(LAYER_OPEN)) {
        $focused.set(null)
      }
    }

    window.addEventListener('keydown', onKey)

    return () => window.removeEventListener('keydown', onKey)
  }, [focused, selected.size])

  // Clicking the board background clears the trace: anywhere in the strip
  // that isn't a card, a line, or a fold marker.
  useEffect(() => {
    if (!strip || !focused) {
      return
    }

    const onClick = (event: MouseEvent) => {
      const target = event.target as Element

      if (!$hotEdge.get() && !target.closest(`[data-card-key], [data-board-arrows], [${LANE_GAP_ATTR}]`)) {
        $focused.set(null)
      }
    }

    strip.addEventListener('click', onClick, true)

    return () => strip.removeEventListener('click', onClick, true)
  }, [focused, strip])

  // Lane fold, recomputed from the DOM whenever the lanes re-render.
  useLayoutEffect(() => {
    if (!strip || !focused) {
      if ($folds.get() !== NO_FOLDS) {
        $folds.set(NO_FOLDS)
      }

      return
    }

    const open = openGaps.focus === focused ? openGaps.ids : EMPTY_IDS
    const lit = new Set([focused, ...chain.upstream, ...chain.downstream])

    const run = () => {
      const next = computeFolds(strip, key => lit.has(key) || selected.has(key), open)

      if (!sameFolds(next, $folds.get())) {
        $folds.set(next.size ? next : NO_FOLDS)
      }
    }

    run()
    const mutation = new MutationObserver(run)
    mutation.observe(strip, { childList: true, subtree: true })

    return () => mutation.disconnect()
  }, [chain, focused, openGaps, selected, strip])

  // All Boards: upstream's socket follows one board; follow them all.
  const cursorKey = isAll && on ? Object.keys(payload?.cursors ?? {}).sort().join(',') : ''

  useEffect(() => {
    if (!cursorKey) {
      return
    }

    return watchAllBoards(qc)
  }, [cursorKey, qc])

  if (!on) {
    return null
  }

  return (
    <>
      <DispatchPausedNotice slug={slug} />
      {isAll && <BoardFilterChips boards={payload?.boards ?? []} />}
      {isAll && <BoardsErrorNotice errors={payload?.errors} />}
      {focused && hasEdges && (
        <FocusAnswerBar
          depth={depth}
          focused={focused}
          graph={graph}
          index={index}
          onClear={() => $focused.set(null)}
          onDepth={next => $focusDepth.set(next)}
          onFocus={key => $focused.set(key)}
        />
      )}
      {strip &&
        createPortal(
          <BoardDependencyArrows
            depth={depth}
            downstream={chain.downstream}
            focused={hasEdges ? focused : null}
            graph={graph}
            index={index}
            stripRef={stripRef}
            upstream={chain.upstream}
          />,
          strip
        )}
    </>
  )
}

/** The `board.tsx` anchor. */
export const boardOverlay = (props: {
  board: KanbanBoard | undefined
  slug: string
  stripRef: RefObject<HTMLDivElement | null>
}) => <BoardOverlay {...props} />
