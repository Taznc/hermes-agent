/**
 * Routing for the on-board dependency arrows: given the focused chain's edges
 * and where each real card sits on the lane strip, produce one SVG path per
 * edge. Pure — no React, no DOM — so the layer and its tests share one source
 * of truth for which side an arrow leaves and enters.
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
   *  visible edge so the arrow still points toward it. */
  offscreen: boolean
  right: number
  top: number
}

export type ArrowSide = 'left' | 'right'

export interface BoardArrow {
  child: string
  d: string
  /** Either end is scrolled out of its lane (drawn dashed). */
  offscreen: boolean
  parent: string
}

/** Clearance between an arrow's tip and the card border, so the head never
 *  hides under the focus ring. */
export const ARROW_GAP = 3
/** How far a same-lane arrow swings out past the cards before turning back. */
export const LOOP_OUT = 22

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

/** Route every edge whose BOTH cards are on screen in some lane. An edge to a
 *  card that isn't rendered (filtered out, collapsed lane, hidden board)
 *  would point at nothing, so it is skipped rather than guessed at.
 *
 *  Several arrows on the same side of one card are spread along that side,
 *  ordered by the far end's height, so they fan instead of stacking into one
 *  indistinguishable line. */
export function routeArrows(
  edges: ReadonlyArray<readonly [string, string]>,
  boxes: Map<string, CardBox>
): BoardArrow[] {
  const routed = edges.flatMap(([parent, child]) => {
    const from = boxes.get(parent)
    const to = boxes.get(child)

    return from && to ? [{ child, from, parent, sides: arrowSides(from, to), to }] : []
  })

  // Port slots: (cardKey, side) → every arrow end attached there. Out-ends
  // are tagged `i`, in-ends `~i` (always negative), so one card side carrying
  // both an outgoing and an incoming arrow spreads them together.
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

  const portY = new Map<number, number>()

  for (const { box, ends } of ports.values()) {
    const sorted = [...ends].sort((a, b) => a.farY - b.farY || a.end - b.end)
    const height = box.bottom - box.top

    sorted.forEach(({ end }, i) => {
      portY.set(end, box.top + (height * (i + 1)) / (sorted.length + 1))
    })
  }

  return routed.map(({ child, from, parent, sides, to }, i) => {
    const y1 = portY.get(i)!
    const y2 = portY.get(~i)!
    const x1 = sides[0] === 'right' ? from.right : from.left
    const x2 = sides[1] === 'right' ? to.right + ARROW_GAP : to.left - ARROW_GAP

    let c1: number
    let c2: number

    if (sides[0] === sides[1]) {
      // Same lane: swing out to the right, a little wider for a longer span
      // so nested brackets on one lane don't sit on top of each other.
      const out = LOOP_OUT + Math.min(40, Math.abs(y2 - y1) / 8)

      c1 = x1 + out
      c2 = x2 + out
    } else {
      const dir = sides[0] === 'right' ? 1 : -1
      const bend = Math.max(24, Math.abs(x2 - x1) / 2)

      c1 = x1 + dir * bend
      c2 = x2 - dir * bend
    }

    return {
      child,
      d: `M ${x1} ${y1} C ${c1} ${y1}, ${c2} ${y2}, ${x2} ${y2}`,
      offscreen: from.offscreen || to.offscreen,
      parent
    }
  })
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

/** Which edges to draw for the current focus. `direct` mirrors `focusSets`
 *  (one hop): only the links that touch the focused card, so an arrow never
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
