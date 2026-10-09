import { describe, expect, it } from 'vitest'

import { foldLane, type LaneSlot } from './lane-fold'

const id = (s: string) => s
const linked = (keys: readonly string[]) => (item: string) => keys.includes(item)

/** Render slots as a compact string: cards by key, gaps as `+N`. */
const shape = (slots: LaneSlot<string>[]) =>
  slots.map(slot => (slot.kind === 'card' ? slot.item : `+${slot.items.length}`)).join(' ')

describe('foldLane', () => {
  it('folds each run of unrelated cards into one gap, keeping lane order', () => {
    expect(shape(foldLane(['a', 'b', 'L1', 'c', 'L2', 'd', 'e', 'f'], id, linked(['L1', 'L2'])))).toBe('+2 L1 +1 L2 +3')
  })

  it('never loses or duplicates a card: gaps + cards account for the whole lane', () => {
    const lane = ['a', 'L1', 'b', 'c', 'L2']
    const slots = foldLane(lane, id, linked(['L1', 'L2']))
    const flat = slots.flatMap(slot => (slot.kind === 'card' ? [slot.item] : slot.items))

    expect(flat).toEqual(lane)
  })

  it('a lane with nothing linked becomes a single gap', () => {
    expect(shape(foldLane(['a', 'b', 'c'], id, () => false))).toBe('+3')
  })

  it('a lane where everything is linked is left as it is', () => {
    expect(shape(foldLane(['a', 'b'], id, () => true))).toBe('a b')
  })

  it('an opened gap renders its cards again; the others stay folded', () => {
    // The gap's id is its first card's key.
    expect(shape(foldLane(['a', 'b', 'L1', 'c', 'd'], id, linked(['L1']), new Set(['c'])))).toBe('+2 L1 c d')
  })

  it('an empty lane has no slots', () => {
    expect(foldLane([], id, () => false)).toEqual([])
  })
})
