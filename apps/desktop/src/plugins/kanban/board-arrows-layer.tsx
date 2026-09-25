/**
 * The on-board dependency arrows: while a card is focused, an SVG layer over
 * the lane strip draws an arrow from each blocker to the card it blocks,
 * between the REAL cards where they sit on the board. The routing is pure
 * (`board-arrows.ts`); this file only measures the DOM and paints.
 *
 * The layer lives inside the horizontally-scrolling lane strip, sized to its
 * scroll width, so a horizontal scroll moves arrows and cards together for
 * free. A lane's own vertical scroll, a resize, or a card changing height
 * re-measures (rAF-coalesced). Nothing is mounted or measured while no card
 * is focused, so the common board pays nothing.
 */

import { cn } from '@hermes/plugin-sdk'
import { type RefObject, useId, useLayoutEffect, useMemo, useState } from 'react'

import { type BoardArrow, type CardBox, clampToLane, focusEdges, routeArrows, sameArrows } from './board-arrows'
import { type DependencyGraph, isGating } from './deps'
import type { KanbanTask } from './types'

/** Attribute every rendered card carries (its `cardKey`), and the one each
 *  lane's vertical scroller carries — the layer's only coupling to `card.tsx`. */
const CARD_KEY_ATTR = 'data-card-key'
const LANE_SCROLLER_ATTR = 'data-lane-scroller'

const NO_ARROWS: BoardArrow[] = []

/** Measure every endpoint card that is actually rendered, in strip content
 *  coordinates. Cards outside `keys` are never read. */
function measure(strip: HTMLElement, keys: ReadonlySet<string>): { boxes: Map<string, CardBox>; elements: Element[] } {
  const origin = strip.getBoundingClientRect()
  const dx = strip.scrollLeft - origin.left
  const dy = strip.scrollTop - origin.top
  const boxes = new Map<string, CardBox>()
  const elements: Element[] = []

  for (const el of strip.querySelectorAll<HTMLElement>(`[${CARD_KEY_ATTR}]`)) {
    const key = el.getAttribute(CARD_KEY_ATTR)!

    if (!keys.has(key)) {
      continue
    }

    const rect = el.getBoundingClientRect()
    const lane = el.closest(`[${LANE_SCROLLER_ATTR}]`)?.getBoundingClientRect() ?? null
    const clamped = clampToLane(rect, lane)

    elements.push(el)
    boxes.set(key, {
      bottom: clamped.bottom + dy,
      left: rect.left + dx,
      offscreen: clamped.offscreen,
      right: rect.right + dx,
      top: clamped.top + dy
    })
  }

  return { boxes, elements }
}

export function BoardDependencyArrows({
  depth,
  downstream,
  focused,
  graph,
  index,
  stripRef,
  upstream
}: {
  depth: 'chain' | 'direct'
  downstream: ReadonlySet<string>
  focused: null | string
  graph: DependencyGraph
  index: Map<string, KanbanTask>
  stripRef: RefObject<HTMLDivElement | null>
  upstream: ReadonlySet<string>
}) {
  const markerId = useId().replace(/:/g, '')
  const [arrows, setArrows] = useState<BoardArrow[]>(NO_ARROWS)
  const [size, setSize] = useState({ height: 0, width: 0 })

  const edges = useMemo(
    () => (focused ? focusEdges(graph, focused, depth, { downstream, upstream }) : []),
    [depth, downstream, focused, graph, upstream]
  )

  const endpoints = useMemo(() => new Set(edges.flat()), [edges])

  // Layout effect: the first measure lands before paint, so a trace never
  // flashes arrow-less.
  useLayoutEffect(() => {
    const strip = stripRef.current

    if (!strip || edges.length === 0) {
      setArrows(prev => (prev.length === 0 ? prev : NO_ARROWS))

      return
    }

    let frame = 0
    // Endpoint cards can change height (a thumbnail decoding, a summary
    // arriving) and the strip changes size with the pane.
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => schedule())
    const watched = new Set<Element>()

    const run = () => {
      frame = 0
      const { boxes, elements } = measure(strip, endpoints)
      const next = routeArrows(edges, boxes)

      for (const el of elements) {
        if (!watched.has(el)) {
          watched.add(el)
          resize?.observe(el)
        }
      }

      setArrows(prev => (sameArrows(prev, next) ? prev : next))
      setSize(prev =>
        prev.width === strip.scrollWidth && prev.height === strip.clientHeight
          ? prev
          : { height: strip.clientHeight, width: strip.scrollWidth }
      )
    }

    function schedule() {
      if (!frame) {
        frame = requestAnimationFrame(run)
      }
    }

    run()
    resize?.observe(strip)

    // `scroll` doesn't bubble; capture catches every lane's own scroller.
    strip.addEventListener('scroll', schedule, true)
    window.addEventListener('resize', schedule)

    // Cards mounting/unmounting move the others without resizing them: a
    // search filter, a lane collapsing to a rail, a refresh moving a card to
    // another lane. Structure changes are rare next to scrolls, so watching
    // them costs little and catches every way the board can re-flow.
    const mutation = new MutationObserver(schedule)
    mutation.observe(strip, { childList: true, subtree: true })

    return () => {
      cancelAnimationFrame(frame)
      strip.removeEventListener('scroll', schedule, true)
      window.removeEventListener('resize', schedule)
      resize?.disconnect()
      mutation.disconnect()
    }
  }, [edges, endpoints, stripRef])

  if (!focused || arrows.length === 0) {
    return null
  }

  return (
    <svg
      aria-hidden
      className="pointer-events-none absolute top-0 left-0 z-10 overflow-visible"
      data-board-arrows
      height={size.height}
      width={size.width}
    >
      <defs>
        {(['upstream', 'downstream'] as const).map(side => (
          <marker
            id={`${markerId}-${side}`}
            key={side}
            markerHeight="7"
            markerWidth="7"
            orient="auto-start-reverse"
            refX="6"
            refY="3.5"
            viewBox="0 0 7 7"
          >
            <path className={side === 'upstream' ? 'fill-amber-500' : 'fill-sky-500'} d="M 0 0 L 7 3.5 L 0 7 z" />
          </marker>
        ))}
      </defs>
      {arrows.map(({ child, d, offscreen, parent }) => {
        // Colour matches the card rings: an edge on the blocker side of the
        // focus (it ends at the focused card or at one of its blockers) is
        // amber; everything waiting on the focus is sky.
        const side = child === focused || upstream.has(child) ? 'upstream' : 'downstream'
        // A satisfied blocker no longer gates — muted, like the graph dialog.
        const parentTask = index.get(parent)
        const gating = !parentTask || isGating(parentTask.status)

        return (
          <path
            className={cn(
              side === 'upstream' ? 'stroke-amber-500' : 'stroke-sky-500',
              gating ? 'opacity-90' : 'opacity-40'
            )}
            d={d}
            data-edge={`${parent}->${child}`}
            data-gating={gating}
            data-offscreen={offscreen}
            data-side={side}
            fill="none"
            key={`${parent}\u0001${child}`}
            markerEnd={`url(#${markerId}-${side})`}
            strokeDasharray={offscreen ? '4 3' : undefined}
            strokeLinecap="round"
            strokeWidth={gating ? 1.75 : 1.25}
          />
        )
      })}
    </svg>
  )
}
