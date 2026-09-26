/**
 * Routing for the on-board dependency lines ("curved ribbons"): given the
 * focused chain's edges and where each real card sits on the lane strip,
 * produce one smooth S-curve per edge straight from blocker to blocked card,
 * plus the arrowhead and direction chevrons that ride on it. Pure — no React,
 * no DOM (every point is computed from the curve's own control points, never
 * `getPointAtLength`) — so the layer and its tests share one source of truth.
 *
 * Vocabulary matches `deps.ts`: an edge is `[parent, child]` and means the
 * parent BLOCKS the child, so every arrow's head lands on the blocked card.
 */

import { chainEdges, type DependencyGraph, downstreamOf, upstreamOf } from './deps'

/** A card's box in lane-strip content coordinates (scroll-independent). */
export interface CardBox {
  bottom: number
  left: number
  /** The card is scrolled out of its lane; the box is pinned to the lane's
   *  visible edge so the line still points toward it. */
  offscreen: boolean
  right: number
  top: number
}

export type ArrowSide = 'left' | 'right'

export interface Point {
  x: number
  y: number
}

/** A cubic Bézier: start, two control points, end. */
export type Curve = readonly [Point, Point, Point, Point]

export interface BoardArrow {
  child: string
  curve: Curve
  d: string
  /** Either end is scrolled out of its lane. */
  offscreen: boolean
  parent: string
}

/** Clearance between a line's tip and the card border, so the head never
 *  hides under the focus ring. */
export const ARROW_GAP = 2
/** Vertical spread between ribbons that land on the same card side, so two
 *  curves aiming at one card arc through their own band instead of running
 *  on top of each other. */
export const FAN_LIFT = 26

/** Which side of each card an edge uses. A child to the right is entered from
 *  its left; a child to the left from its right; a child in the SAME lane
 *  (overlapping x) gets a bracket that leaves and re-enters on the right, so
 *  the curve never cuts through the cards stacked between them. */
export function arrowSides(from: CardBox, to: CardBox): [ArrowSide, ArrowSide] {
  if (to.left >= from.right) {
    return ['right', 'left']
  }

  if (to.right <= from.left) {
    return ['left', 'right']
  }

  return ['right', 'right']
}

const midY = (box: CardBox) => (box.top + box.bottom) / 2
const round = (n: number) => Math.round(n * 10) / 10

/** How far the S-curve's handles reach: half the span on a short hop so the
 *  curve stays an S, capped on a long one so it doesn't balloon. */
export function handleReach(dx: number): number {
  return dx < 90 ? dx * 0.5 : Math.min(dx * 0.45, 120)
}

/** How far a same-lane bracket swings out, a little wider for a longer span
 *  so nested brackets on one lane don't sit on top of each other. */
export function bracketReach(dy: number): number {
  return 34 + Math.min(40, dy / 6)
}

/** The widest a bracket can reach past its card: the curve's extreme is 3/4
 *  of the handle offset. The strip's right padding must cover this. */
export const BRACKET_MAX = Math.ceil(bracketReach(Infinity) * 0.75)

/** Route every edge whose BOTH cards are on screen in some lane. An edge to a
 *  card that isn't rendered (filtered out, collapsed lane, hidden board)
 *  would point at nothing, so it is skipped rather than guessed at.
 *
 *  Several ends on the same side of one card are spread along that side,
 *  ordered by the far end's height, so they fan instead of stacking into one
 *  indistinguishable line; the ribbons landing there also arc through their
 *  own height band (`FAN_LIFT`). */
export function routeArrows(
  edges: ReadonlyArray<readonly [string, string]>,
  boxes: Map<string, CardBox>
): BoardArrow[] {
  const routed = edges.flatMap(([parent, child]) => {
    const from = boxes.get(parent)
    const to = boxes.get(child)

    return from && to ? [{ child, from, parent, sides: arrowSides(from, to), to }] : []
  })

  // Port slots: (cardKey, side) → every line end attached there. Out-ends
  // are tagged `i`, in-ends `~i` (always negative), so one card side carrying
  // both an outgoing and an incoming line spreads them together.
  const ports = new Map<string, { box: CardBox; ends: Array<{ end: number; farY: number }> }>()

  const claim = (key: string, box: CardBox, side: ArrowSide, end: number, farY: number) => {
    const slot = `${key}\u0001${side}`
    const port = ports.get(slot)

    port ? port.ends.push({ end, farY }) : ports.set(slot, { box, ends: [{ end, farY }] })
  }

  routed.forEach((arrow, i) => {
    claim(arrow.parent, arrow.from, arrow.sides[0], i, midY(arrow.to))
    claim(arrow.child, arrow.to, arrow.sides[1], ~i, midY(arrow.from))
  })

  const slot = new Map<number, { count: number; index: number; y: number }>()

  for (const { box, ends } of ports.values()) {
    const sorted = [...ends].sort((a, b) => a.farY - b.farY || a.end - b.end)
    const height = box.bottom - box.top

    sorted.forEach(({ end }, index) => {
      slot.set(end, { count: sorted.length, index, y: box.top + (height * (index + 1)) / (sorted.length + 1) })
    })
  }

  return routed.map(({ child, from, parent, sides, to }, i) => {
    const y1 = slot.get(i)!.y
    const target = slot.get(~i)!
    const y2 = target.y
    const x1 = sides[0] === 'right' ? from.right : from.left
    const x2 = sides[1] === 'right' ? to.right + ARROW_GAP : to.left - ARROW_GAP

    let curve: Curve

    if (sides[0] === sides[1]) {
      const out = bracketReach(Math.abs(y2 - y1))

      curve = [
        { x: x1, y: y1 },
        { x: x1 + out, y: y1 },
        { x: x2 + out, y: y2 },
        { x: x2, y: y2 }
      ]
    } else {
      const dir = sides[0] === 'right' ? 1 : -1
      const reach = handleReach(Math.abs(x2 - x1))
      const lift = target.count > 1 ? (target.index - (target.count - 1) / 2) * FAN_LIFT : 0

      curve = [
        { x: x1, y: y1 },
        { x: x1 + dir * reach, y: y1 + lift },
        { x: x2 - dir * reach, y: y2 + lift },
        { x: x2, y: y2 }
      ]
    }

    const [p0, p1, p2, p3] = curve.map(p => ({ x: round(p.x), y: round(p.y) }))

    return {
      child,
      curve,
      d: `M ${p0.x} ${p0.y} C ${p1.x} ${p1.y}, ${p2.x} ${p2.y}, ${p3.x} ${p3.y}`,
      offscreen: from.offscreen || to.offscreen,
      parent
    }
  })
}

// ── geometry on a routed curve ──────────────────────────────────────────────

export function pointAt([p0, p1, p2, p3]: Curve, t: number): Point {
  const u = 1 - t
  const a = u * u * u
  const b = 3 * u * u * t
  const c = 3 * u * t * t
  const d = t * t * t

  return { x: a * p0.x + b * p1.x + c * p2.x + d * p3.x, y: a * p0.y + b * p1.y + c * p2.y + d * p3.y }
}

const SAMPLES = 48

/** The curve flattened to a polyline with cumulative arc length — enough to
 *  place marks at even spacing along it. */
function sample(curve: Curve): { length: number; points: Point[]; at: number[] } {
  const points: Point[] = []
  const at: number[] = []
  let length = 0

  for (let i = 0; i <= SAMPLES; i += 1) {
    const p = pointAt(curve, i / SAMPLES)

    if (i > 0) {
      length += Math.hypot(p.x - points[i - 1].x, p.y - points[i - 1].y)
    }

    points.push(p)
    at.push(length)
  }

  return { at, length, points }
}

/** Position + heading at arc length `s` along a sampled curve. */
function along(s: ReturnType<typeof sample>, distance: number): { angle: number; point: Point } {
  let i = 1

  while (i < s.at.length - 1 && s.at[i] < distance) {
    i += 1
  }

  const a = s.points[i - 1]
  const b = s.points[i]
  const span = s.at[i] - s.at[i - 1] || 1
  const f = Math.min(1, Math.max(0, (distance - s.at[i - 1]) / span))

  return { angle: Math.atan2(b.y - a.y, b.x - a.x), point: { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f } }
}

/** Direction the curve is travelling as it arrives at its end. */
function endAngle([p0, p1, p2, p3]: Curve): number {
  for (const from of [p2, p1, p0]) {
    if (from.x !== p3.x || from.y !== p3.y) {
      return Math.atan2(p3.y - from.y, p3.x - from.x)
    }
  }

  return 0
}

const placed = (origin: Point, angle: number) => {
  const cos = Math.cos(angle)
  const sin = Math.sin(angle)

  return (fx: number, fy: number) => `${round(origin.x + fx * cos - fy * sin)},${round(origin.y + fx * sin + fy * cos)}`
}

/** A notched arrowhead whose tip sits exactly on the curve's end, aligned to
 *  its end tangent. Its length never exceeds 60% of the straight distance
 *  between the two cards, so on a short hop between neighbouring lanes the
 *  head can't reach back over the blocker. */
export function arrowHead(curve: Curve, size: number): string {
  const span = Math.hypot(curve[3].x - curve[0].x, curve[3].y - curve[0].y)
  const s = Math.max(8, Math.min(size, span * 0.6))
  const w = s * 0.5
  const pt = placed(curve[3], endAngle(curve))

  return `${pt(0, 0)} ${pt(-s, -w)} ${pt(-s * 0.72, 0)} ${pt(-s, w)}`
}

/** Spacing between direction chevrons, and the shortest line that gets any. */
export const CHEVRON_EVERY = 46
const CHEVRON_MIN_LENGTH = 70

/** Small `>` marks every ~46px along the line, so its direction reads anywhere
 *  on it, not only at the tip. Returns one polyline `points` string each;
 *  none on a line too short to need them. */
export function chevrons(curve: Curve, strokeWidth: number): string[] {
  const s = sample(curve)

  if (s.length < CHEVRON_MIN_LENGTH) {
    return []
  }

  const n = Math.floor((s.length - 40) / CHEVRON_EVERY)
  const k = strokeWidth * 0.75 + 1.5
  const out: string[] = []

  for (let i = 1; i <= n; i += 1) {
    const { angle, point } = along(s, 20 + i * ((s.length - 40) / (n + 1)))
    const pt = placed(point, angle)

    out.push(`${pt(-k * 0.6, -k)} ${pt(k * 0.5, 0)} ${pt(-k * 0.6, k)}`)
  }

  return out
}

/** Pin a card's box into the visible part of its lane scroller. A card
 *  scrolled fully out collapses to a zero-height box on the edge it left by. */
export function clampToLane(
  card: { bottom: number; top: number },
  lane: { bottom: number; top: number } | null
): { bottom: number; offscreen: boolean; top: number } {
  if (!lane) {
    return { bottom: card.bottom, offscreen: false, top: card.top }
  }

  if (card.bottom <= lane.top) {
    return { bottom: lane.top, offscreen: true, top: lane.top }
  }

  if (card.top >= lane.bottom) {
    return { bottom: lane.bottom, offscreen: true, top: lane.bottom }
  }

  return { bottom: Math.min(card.bottom, lane.bottom), offscreen: false, top: Math.max(card.top, lane.top) }
}

/** Arrow lists are rebuilt on every measure; keep the old array when nothing
 *  moved so React bails out instead of re-rendering the layer per scroll tick. */
export function sameArrows(a: readonly BoardArrow[], b: readonly BoardArrow[]): boolean {
  return (
    a.length === b.length &&
    a.every(
      (arrow, i) =>
        arrow.d === b[i].d &&
        arrow.offscreen === b[i].offscreen &&
        arrow.parent === b[i].parent &&
        arrow.child === b[i].child
    )
  )
}

/** Stable id for one drawn edge — what a hovered answer-bar row and a hovered
 *  line agree on. */
export const edgeId = (parent: string, child: string) => `${parent}->${child}`

/** Which edges to draw for the current focus. `direct` mirrors `focusSets`
 *  (one hop): only the links that touch the focused card, so a line never
 *  lands on a card the trace left dimmed. `chain` draws every link among the
 *  lit cards — the transitive picture the user asked for. */
export function focusEdges(
  graph: DependencyGraph,
  focused: string,
  depth: 'chain' | 'direct',
  lit: { downstream: ReadonlySet<string>; upstream: ReadonlySet<string> }
): Array<[string, string]> {
  if (depth === 'chain') {
    return chainEdges(graph, new Set([focused, ...lit.upstream, ...lit.downstream]))
  }

  // Deduplicated like chainEdges: a repeated link_edges row must not yield two
  // paths with the same React key.
  const seen = new Set<string>()
  const edges: Array<[string, string]> = []

  const add = (parent: string, child: string) => {
    const id = `${parent}\u0001${child}`

    if (parent !== child && !seen.has(id)) {
      seen.add(id)
      edges.push([parent, child])
    }
  }

  upstreamOf(graph, focused).forEach(parent => add(parent, focused))
  downstreamOf(graph, focused).forEach(child => add(focused, child))

  return edges
}
