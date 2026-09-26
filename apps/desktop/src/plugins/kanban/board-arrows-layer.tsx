/**
 * The on-board dependency lines: while a card is focused, an SVG layer over
 * the lane strip draws a thick "ribbon" from each blocker to the card it
 * blocks, between the REAL cards where they sit on the board. The routing
 * and every mark's geometry are pure (`board-arrows.ts`); this file only
 * measures the DOM and paints.
 *
 * A line's COLOUR is its blocker's status (`LINK_TONE`), so "is everything
 * that holds this card On hold?" reads straight off the board; a satisfied
 * blocker's line is dashed and muted. Lines touching the focused card are
 * thicker than the rest of a Full chain. Each line has a dark casing so
 * crossings stay readable, a start dot on the blocker, a big arrowhead on the
 * held-up card (painted after every line so nothing covers it), optional
 * direction chevrons, and optional "moving dots" that flow blocker → blocked.
 *
 * Hovering a line (or its row in the answer bar) isolates it: the rest fade
 * and its two cards get a highlight ring. The hovered edge is a tiny atom,
 * not board state, so a hover never re-renders the board.
 *
 * The layer lives inside the horizontally-scrolling lane strip, sized to its
 * scroll width, so a horizontal scroll moves lines and cards together for
 * free. A lane's own vertical scroll, a resize, or a card changing height
 * re-measures (rAF-coalesced). Nothing is mounted or measured while no card
 * is focused, so the common board pays nothing.
 *
 * `FocusDepthControls` is the Direct / Full chain switch the answer bar shows
 * while a trace is live; it decides which edges this layer draws.
 */

import { atom, cn, SegmentedControl, useValue } from '@hermes/plugin-sdk'
import { Fragment, type RefObject, useEffect, useLayoutEffect, useMemo, useState } from 'react'

import { $depChevrons, $depFlow } from './api'
import {
  arrowHead,
  type BoardArrow,
  type CardBox,
  chevrons,
  clampToLane,
  edgeId,
  focusEdges,
  revealDelta,
  routeArrows,
  sameArrows
} from './board-arrows'
import { type DependencyGraph, isGating } from './deps'
import { linkTone } from './focus-verdict'
import type { KanbanTask } from './types'
import { useKanban } from './ui'

export type FocusDepth = 'chain' | 'direct'

/** The edge (`edgeId`) under the pointer — a line, or its answer-bar row.
 *  Presentation only; never persisted. */
export const $hotEdge = atom<null | string>(null)

/** Direct links (one hop) vs Full chain (transitive, both directions). */
export function FocusDepthControls({ depth, onDepth }: { depth: FocusDepth; onDepth: (depth: FocusDepth) => void }) {
  const k = useKanban()

  return (
    <SegmentedControl
      className="shrink-0"
      onChange={onDepth}
      options={[
        { id: 'direct', label: k.depFocusDirect },
        { id: 'chain', label: k.depFocusChain }
      ]}
      value={depth}
    />
  )
}

/** Attribute every rendered card carries (its `cardKey`), and the one each
 *  lane's vertical scroller carries — the layer's only coupling to `card.tsx`. */
export const CARD_KEY_ATTR = 'data-card-key'
const LANE_SCROLLER_ATTR = 'data-lane-scroller'
/** Set on the two cards of the hovered line (styled in kanban.css). */
const HOT_CARD_ATTR = 'data-dep-hot'

/** Casing drawn under every line: the surface colour, so a crossing reads as
 *  one line passing OVER the other in both themes. */
const CASE = 'var(--ui-surface-background, #111318)'

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

/** How one line is painted. */
export interface LineStyle {
  color: string
  /** Touches the focused card — thicker and fully opaque. */
  direct: boolean
  /** The blocker no longer gates: dashed + muted, no flow. */
  gating: boolean
  head: number
  opacity: number
  status: string
  width: number
}

export function lineStyle(parentTask: KanbanTask | undefined, direct: boolean): LineStyle {
  const status = parentTask?.status ?? 'unknown'
  const gating = !parentTask || isGating(status)

  return {
    color: linkTone(status),
    direct,
    gating,
    head: direct ? 22 : 16,
    opacity: gating ? (direct ? 1 : 0.75) : 0.62,
    status,
    width: direct ? 5 : 3.2
  }
}

const cardEl = (strip: HTMLElement, key: string) =>
  Array.from(strip.querySelectorAll<HTMLElement>(`[${CARD_KEY_ATTR}]`)).find(
    el => el.getAttribute(CARD_KEY_ATTR) === key
  )

/** Width of a line's invisible hover band, in px. */
const HIT_WIDTH = 14

/** Is `at` (in the layer's own coordinates) on this hover band? A renderer
 *  without SVG geometry (jsdom) simply never hovers a line. */
function onStroke(path: SVGPathElement, at: { x: number; y: number }): boolean {
  if (typeof path.isPointInStroke !== 'function') {
    return false
  }

  const svg = path.ownerSVGElement
  const point: DOMPointInit = svg?.createSVGPoint?.() ?? { x: 0, y: 0 }

  point.x = at.x
  point.y = at.y

  return path.isPointInStroke(point)
}

/** Scroll `view` along one axis just enough to show `el` (see `revealDelta`).
 *  A view with no layout yet (hidden pane, jsdom) is left alone. */
function revealIn(view: HTMLElement | null, el: HTMLElement, axis: 'x' | 'y') {
  const box = view?.getBoundingClientRect()

  if (!view || !box || box.width === 0 || box.height === 0) {
    return
  }

  const rect = el.getBoundingClientRect()

  const delta =
    axis === 'y'
      ? revealDelta({ end: rect.bottom, start: rect.top }, { end: box.bottom, start: box.top })
      : revealDelta({ end: rect.right, start: rect.left }, { end: box.right, start: box.left })

  if (delta !== 0) {
    view[axis === 'y' ? 'scrollTop' : 'scrollLeft'] += delta
  }
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
  const [arrows, setArrows] = useState<BoardArrow[]>(NO_ARROWS)
  const [size, setSize] = useState({ height: 0, width: 0 })
  const hot = useValue($hotEdge)
  const showChevrons = useValue($depChevrons)
  const flow = useValue($depFlow)

  const edges = useMemo(
    () => (focused ? focusEdges(graph, focused, depth, { downstream, upstream }) : []),
    [depth, downstream, focused, graph, upstream]
  )

  const endpoints = useMemo(() => new Set(edges.flat()), [edges])

  // Keep the focused card on screen. Opening the answer bar pushes the lane
  // strip down by the bar's height, and the focused card grows its roll-up, so
  // a card clicked near the bottom of its lane would otherwise land below the
  // fold — every line then aims at a lane edge instead of the card. Clicking
  // an answer-bar row can also focus a card scrolled out of view. Runs before
  // the measure below, in the same commit as the bar, so the first paint
  // already has the card (and its lines) in place.
  useLayoutEffect(() => {
    const strip = stripRef.current
    const card = focused && strip ? cardEl(strip, focused) : undefined

    if (!strip || !card) {
      return
    }

    revealIn(card.closest<HTMLElement>(`[${LANE_SCROLLER_ATTR}]`), card, 'y')
    revealIn(strip, card, 'x')
  }, [focused, stripRef])

  // Layout effect: the first measure lands before paint, so a trace never
  // flashes line-less.
  useLayoutEffect(() => {
    const strip = stripRef.current

    if (!strip || edges.length === 0) {
      setArrows(prev => (prev.length === 0 ? prev : NO_ARROWS))

      return
    }

    let frame = 0
    // Endpoint cards can change height (a thumbnail decoding, a summary
    // arriving, the focused card's roll-up) and the strip changes size with
    // the pane.
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
    // them costs little and catches every way the board can re-flow. Only
    // `childList`: the hover ring is an attribute write, so it can't loop.
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

  // A hover belongs to one trace: a new focus (or none) starts clean.
  useEffect(() => () => $hotEdge.set(null), [focused, depth])

  // Line hover. The layer sits OVER the cards (z-10) and its hover bands are
  // 14px wide, so if the lines were pointer targets they would swallow every
  // click, drag, context menu and lane wheel on whatever card they cross.
  // Instead the whole layer stays `pointer-events: none` and the strip
  // hit-tests the bands itself on pointermove (rAF-coalesced), topmost line
  // first. It only ever clears a hover IT set, so an answer-bar row's hover
  // is never stomped.
  useEffect(() => {
    const strip = stripRef.current

    if (!strip || !focused) {
      return
    }

    let frame = 0
    let point: { x: number; y: number } | null = null
    let mine: null | string = null

    const settle = (id: null | string) => {
      if (id) {
        $hotEdge.set(id)
      } else if (mine && $hotEdge.get() === mine) {
        $hotEdge.set(null)
      }

      mine = id
    }

    const run = () => {
      frame = 0
      const svg = strip.querySelector<SVGSVGElement>('[data-board-arrows]')

      if (!svg || !point) {
        return settle(null)
      }

      const origin = svg.getBoundingClientRect()
      const at = { x: point.x - origin.left, y: point.y - origin.top }
      const bands = Array.from(svg.querySelectorAll<SVGPathElement>('[data-hit]')).reverse()
      const band = bands.find(path => onStroke(path, at))

      settle(band?.closest('[data-edge]')?.getAttribute('data-edge') ?? null)
    }

    const move = (event: PointerEvent) => {
      point = { x: event.clientX, y: event.clientY }
      frame ||= requestAnimationFrame(run)
    }

    const leave = () => {
      point = null
      frame ||= requestAnimationFrame(run)
    }

    strip.addEventListener('pointermove', move)
    strip.addEventListener('pointerleave', leave)

    return () => {
      cancelAnimationFrame(frame)
      strip.removeEventListener('pointermove', move)
      strip.removeEventListener('pointerleave', leave)
      settle(null)
    }
  }, [focused, stripRef])

  // Ring the two cards of the hovered line. Direct attribute writes on the
  // two elements, so a hover costs two DOM writes instead of a board render.
  useEffect(() => {
    const strip = stripRef.current
    const arrow = hot ? arrows.find(a => edgeId(a.parent, a.child) === hot) : undefined

    if (!strip || !arrow) {
      return
    }

    const lit = [cardEl(strip, arrow.parent), cardEl(strip, arrow.child)].filter(Boolean) as HTMLElement[]

    lit.forEach(el => el.setAttribute(HOT_CARD_ATTR, ''))

    return () => lit.forEach(el => el.removeAttribute(HOT_CARD_ATTR))
  }, [arrows, hot, stripRef])

  if (!focused || arrows.length === 0) {
    return null
  }

  const drawn = arrows.map(arrow => ({
    arrow,
    id: edgeId(arrow.parent, arrow.child),
    // Colour matches the card rings for the SIDE (kept as data for tests
    // and tooling); the stroke colour is the blocker's status.
    side: arrow.child === focused || upstream.has(arrow.child) ? ('upstream' as const) : ('downstream' as const),
    style: lineStyle(index.get(arrow.parent), arrow.parent === focused || arrow.child === focused)
  }))

  const hovering = Boolean(hot && drawn.some(line => line.id === hot))

  return (
    <svg
      aria-hidden
      className={cn('kanban-dep-lines pointer-events-none absolute top-0 left-0 z-10 overflow-visible')}
      data-board-arrows
      data-hovering={hovering || undefined}
      height={size.height}
      width={size.width}
    >
      {/* Casings first, under every line, so each crossing reads as one
          line passing over another rather than a merged blob. */}
      <g>
        {drawn.map(({ arrow, id, style }) => (
          <path
            d={arrow.d}
            data-hot={hot === id || undefined}
            data-line-part
            fill="none"
            key={id}
            opacity={style.opacity}
            stroke={CASE}
            strokeLinecap="round"
            strokeWidth={style.width + 4}
          />
        ))}
      </g>
      <g>
        {drawn.map(({ arrow, id, side, style }) => (
          <g
            data-edge={id}
            data-gating={style.gating}
            data-hot={hot === id || undefined}
            data-line-part
            data-offscreen={arrow.offscreen}
            data-side={side}
            data-status={style.status}
            key={id}
            opacity={style.opacity}
          >
            <path
              className="kanban-dep-line"
              d={arrow.d}
              fill="none"
              stroke={style.color}
              strokeDasharray={
                !style.gating
                  ? `${style.width * 1.6} ${style.width * 1.4}`
                  : arrow.offscreen
                    ? `${style.width * 2} ${style.width * 1.2}`
                    : undefined
              }
              strokeLinecap="round"
              strokeWidth={style.width}
            />
            {flow && style.gating && (
              <path
                className="kanban-dep-flow"
                d={arrow.d}
                fill="none"
                stroke="#fff"
                strokeDasharray="0.1 17.9"
                strokeLinecap="round"
                strokeOpacity={0.75}
                strokeWidth={Math.max(1.6, style.width * 0.42)}
              />
            )}
            {/* Fat invisible twin: a 5px line is too thin to hover reliably.
                Hit-tested by the strip's pointermove (see above), never a
                pointer target itself. */}
            <path d={arrow.d} data-hit fill="none" stroke="transparent" strokeWidth={HIT_WIDTH} />
          </g>
        ))}
      </g>
      {/* Start dots, then arrowheads and chevrons LAST: the head on the
          held-up card is the one mark that must never be covered. */}
      <g>
        {drawn.map(({ arrow, id, style }) => (
          <Fragment key={id}>
            <circle
              cx={arrow.curve[0].x}
              cy={arrow.curve[0].y}
              data-hot={hot === id || undefined}
              data-line-part
              fill={style.color}
              opacity={style.opacity}
              r={style.direct ? 4.6 : 3.4}
              stroke={CASE}
              strokeWidth={2}
            />
            <polygon
              data-head={id}
              data-hot={hot === id || undefined}
              data-line-part
              fill={style.color}
              opacity={style.opacity}
              points={arrowHead(arrow.curve, style.head)}
              stroke={CASE}
              strokeLinejoin="round"
              strokeWidth={1.6}
            />
            {showChevrons &&
              chevrons(arrow.curve, style.width).map((points, i) => (
                <polyline
                  data-chevron={id}
                  data-hot={hot === id || undefined}
                  data-line-part
                  fill="none"
                  key={i}
                  opacity={style.opacity * 0.9}
                  points={points}
                  stroke="#0d0f13"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={Math.max(1.6, style.width * 0.42)}
                />
              ))}
          </Fragment>
        ))}
      </g>
    </svg>
  )
}
