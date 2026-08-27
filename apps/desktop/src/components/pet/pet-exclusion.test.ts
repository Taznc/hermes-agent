import { describe, expect, it } from 'vitest'

import { boxOverlapsRect, expandRect, rectFromPoint, shouldYieldPet } from './pet-exclusion'

describe('expandRect', () => {
  it('grows every side by margin', () => {
    expect(expandRect({ bottom: 100, left: 0, right: 100, top: 0 }, 10)).toEqual({
      bottom: 110,
      left: -10,
      right: 110,
      top: -10
    })
  })
})

describe('rectFromPoint', () => {
  it('centers a square zone on the point', () => {
    expect(rectFromPoint(50, 50, 20)).toEqual({ bottom: 70, left: 30, right: 70, top: 30 })
  })
})

describe('boxOverlapsRect', () => {
  const box = { h: 50, w: 50, x: 100, y: 100 }

  it('is true when the box genuinely overlaps the zone', () => {
    expect(boxOverlapsRect(box, { bottom: 150, left: 90, right: 130, top: 90 })).toBe(true)
  })

  it('is false when merely touching edges', () => {
    // Zone's right edge is exactly at the box's left edge — no real overlap.
    expect(boxOverlapsRect(box, { bottom: 150, left: 50, right: 100, top: 100 })).toBe(false)
  })

  it('is false when far apart', () => {
    expect(boxOverlapsRect(box, { bottom: 10, left: 0, right: 10, top: 0 })).toBe(false)
  })
})

describe('shouldYieldPet', () => {
  const box = { h: 40, w: 40, x: 200, y: 200 }

  it('is false with no zones', () => {
    expect(shouldYieldPet(box, [])).toBe(false)
  })

  it('is true when any one zone overlaps', () => {
    const far = { bottom: 10, left: 0, right: 10, top: 0 }
    const overlapping = { bottom: 250, left: 190, right: 230, top: 190 }

    expect(shouldYieldPet(box, [far, overlapping])).toBe(true)
  })

  it('is false when every zone misses', () => {
    const a = { bottom: 10, left: 0, right: 10, top: 0 }
    const b = { bottom: 20, left: 500, right: 520, top: 500 }

    expect(shouldYieldPet(box, [a, b])).toBe(false)
  })
})
