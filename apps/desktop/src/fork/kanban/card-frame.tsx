/**
 * The per-card half of focus mode, wrapped around upstream's card by ONE
 * anchor in its `Card` render (`host.fork.kanban.frameCard`). Everything is
 * read from module atoms (`state.ts`) the board overlay publishes, so nothing
 * is threaded through upstream's props.
 *
 * The frame is a plain block around the card, the same box the lane lays
 * out, so the arrows layer can measure it (`data-card-key`) and the trace
 * rings/dimming land on the card's outline. It also carries:
 *  - the trace toggle (hover button, only on cards that have a link) and
 *    Alt-click on the card body — both need the fork backend's edges;
 *  - the focused card's verdict roll-up;
 *  - the lane fold: an unrelated card renders as nothing (a hidden marker
 *    kept for DOM order), or — for the first card of a run — as the
 *    "+N cards" gap that stands for the run;
 *  - the All Boards board badge and board filter.
 *
 * With no fork backend the frame is an inert wrapper: no button, no rings.
 */

import { useStore as useValue } from '@nanostores/react'
import { type ComponentType, type MouseEvent, type ReactElement, type ReactNode, useEffect, useMemo } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { FocusRollup } from '@/fork/kanban/answer-bar'
import { downstreamOf, taskCardKey, upstreamOf } from '@/fork/kanban/deps'
import { $depView, $folds, $hiddenBoards, type DependencyView, markSelected, openGap, toggleFocus } from '@/fork/kanban/state'
import { useKanban } from '@/fork/kanban/text'
import type { KanbanTask } from '@/fork/kanban/types'
import { cn } from '@/lib/utils'

/** Marks a folded run of unrelated cards. The overlay's click-off-to-clear
 *  handler skips it, so opening a gap never ends the trace it belongs to. */
export const LANE_GAP_ATTR = 'data-lane-gap'

/** Marks a folded-away card: empty and hidden, kept only so the overlay can
 *  read each lane's full order from the DOM. */
export const FOLD_KEY_ATTR = 'data-fold-key'

export type FocusRole = 'downstream' | 'focused' | 'upstream'

/** Where a card sits relative to the focused one, by `cardKey`. `null` while
 *  nothing is focused AND for unrelated cards. */
export function focusRole(deps: DependencyView, key: string): FocusRole | null {
  if (!deps.focused) {
    return null
  }

  if (deps.focused === key) {
    return 'focused'
  }

  return deps.upstream.has(key) ? 'upstream' : deps.downstream.has(key) ? 'downstream' : null
}

/** Does this card have any link — i.e. is focusing it meaningful? Only with
 *  edges: without them there is nothing to trace (upstream's footer already
 *  shows the link counts). */
export const isLinked = (deps: DependencyView, key: string): boolean =>
  deps.hasEdges && (upstreamOf(deps.graph, key).length > 0 || downstreamOf(deps.graph, key).length > 0)

export function CardFrame({ children, selected, task }: { children: ReactNode; selected: boolean; task: KanbanTask }) {
  const k = useKanban()
  const key = taskCardKey(task)
  const deps = useValue($depView)
  const fold = useValue($folds).get(key)
  const hiddenBoards = useValue($hiddenBoards)

  // Selected cards never fold, and Esc belongs to the selection while one
  // exists — the overlay reads both from this report.
  useEffect(() => {
    markSelected(key, selected)

    return () => markSelected(key, false)
  }, [key, selected])

  if (task.board && hiddenBoards.has(task.board)) {
    return null
  }

  if (fold?.kind === 'hidden') {
    return <div hidden {...{ [FOLD_KEY_ATTR]: key }} />
  }

  if (fold?.kind === 'gap') {
    return (
      <>
        <Tip label={k.depGapShow}>
          <button
            {...{ [LANE_GAP_ATTR]: fold.count }}
            aria-label={`${k.depGap(fold.count)} · ${k.depGapShow}`}
            className="flex shrink-0 items-center justify-center gap-1 rounded-md border border-dashed border-(--ui-stroke-tertiary) py-0.5 text-[0.625rem] tabular-nums text-(--ui-text-quaternary) transition-colors hover:border-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-(--ui-text-secondary)"
            onClick={() => openGap(fold.gap)}
            type="button"
          >
            <Codicon name="unfold" size="0.7rem" />
            {k.depGap(fold.count)}
          </button>
        </Tip>
        <div hidden {...{ [FOLD_KEY_ATTR]: key }} />
      </>
    )
  }

  const role = focusRole(deps, key)
  // Dimmed = a focus is active and this card is on neither end of it.
  const dimmed = Boolean(deps.focused) && role === null
  const linked = isLinked(deps, key)

  // Alt/Option-click is the power-user shortcut for the same trace the hover
  // button starts. Capture phase, so upstream's own click (open the drawer)
  // never sees it. A bare click is left alone.
  const onClickCapture = (event: MouseEvent) => {
    if (event.altKey && linked) {
      event.preventDefault()
      event.stopPropagation()
      toggleFocus(key)
    }
  }

  return (
    <div
      className={cn(
        'group/fork relative shrink-0 rounded-md transition-[opacity,filter,box-shadow]',
        // Rings only — no layout property moves, so nothing reflows.
        role === 'focused' && 'ring-2 ring-(--ui-stroke-primary)',
        role === 'upstream' && 'ring-2 ring-amber-500/70',
        role === 'downstream' && 'ring-2 ring-sky-500/70',
        dimmed && 'opacity-35 saturate-50'
      )}
      data-card-key={key}
      data-focus-role={role ?? (dimmed ? 'dimmed' : undefined)}
      onClickCapture={onClickCapture}
    >
      {children}
      {task.board_name && (
        <span
          className="pointer-events-none absolute -top-1.5 left-2 max-w-28 truncate rounded bg-(--ui-bg-quaternary) px-1 text-[0.5625rem] leading-3 text-(--ui-text-tertiary)"
          data-board-badge={task.board ?? ''}
        >
          {task.board_name}
        </span>
      )}
      {/* Trace this card's dependency chain: on hover, and ONLY on cards
          that actually have a link, so it advertises where dependencies
          exist. Pressed state doubles as the "clear" control. */}
      {linked && (
        <Tip label={role === 'focused' ? k.depClearFocus : k.depFocusHint}>
          <button
            aria-label={role === 'focused' ? k.depClearFocus : k.depFocusHint}
            aria-pressed={role === 'focused'}
            className={cn(
              'absolute top-1.5 right-1.5 grid size-5 place-items-center rounded bg-(--ui-bg-elevated) text-(--ui-text-quaternary) transition-opacity hover:bg-(--chrome-action-hover) hover:text-foreground',
              role === 'focused'
                ? 'text-foreground opacity-100'
                : 'opacity-0 focus-visible:opacity-100 group-hover/fork:opacity-100'
            )}
            onClick={event => {
              event.stopPropagation()
              toggleFocus(key)
            }}
            type="button"
          >
            <Codicon name="references" size="0.8rem" />
          </button>
        </Tip>
      )}
      {/* The focused card answers "why am I stuck?" on itself too. */}
      {role === 'focused' && deps.hasEdges && (
        <div className="px-2.5 pt-1 pb-2">
          <FocusRollup graph={deps.graph} index={deps.index} taskKey={key} />
        </div>
      )}
    </div>
  )
}

/**
 * The `board.tsx` anchor, in upstream's `Card` render (after its hooks):
 *
 *   const fork = host.fork?.kanban?.frameCard(Card, { ...Card's props })
 *   return fork ?? (<upstream's card JSX>)
 *
 * The frame renders upstream's `Card` again INSIDE itself with a marked
 * clone of the task; for a marked task `frameCard` returns null, so the inner
 * render falls through to upstream's own JSX. No props are added to
 * upstream's component, and nothing upstream renders changes.
 */
const framed = new WeakSet<object>()

/** Upstream's `Card` props: the two the frame reads, plus its callbacks (passed through). */
type CardProps = { selected: boolean; task: KanbanTask } & Record<string, unknown>

function FramedCard({ Card, props }: { Card: ComponentType<CardProps>; props: CardProps }) {
  const inner = useMemo(() => {
    const clone = { ...props.task }

    framed.add(clone)

    return clone
  }, [props.task])

  return (
    <CardFrame selected={props.selected} task={props.task}>
      <Card {...props} task={inner} />
    </CardFrame>
  )
}

/** `Card` is typed `unknown` on purpose: upstream's component return type is
 *  inferred through this very call, so a typed parameter would make it
 *  circular. It is always upstream's `Card`. */
export function frameCard(Card: unknown, props: CardProps): null | ReactElement {
  return framed.has(props.task) ? null : <FramedCard Card={Card as ComponentType<CardProps>} props={props} />
}
