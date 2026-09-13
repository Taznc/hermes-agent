/**
 * Pure geometry for the floating pet's non-interference guarantee: "never
 * visually sit on top of what the user is composing or actively selecting."
 *
 * The alpha hit-test (`pet-hit-test.ts`) already stops the pet from EATING
 * clicks on transparent padding around its sprite (#95001). This module
 * covers the other half of the same regression class: the pet's OPAQUE
 * sprite pixels can still visually cover the composer while you're typing, or
 * cover the exact spot you're drag-selecting, even though a click there would
 * now correctly reach the text underneath. Non-interference means the pet
 * gets out of the way, not just "technically doesn't block the click."
 *
 * Kept dependency-free and pure (no DOM reads) so every decision is unit
 * testable without mounting a component or mocking `getBoundingClientRect`.
 */

export interface Rect {
  top: number
  left: number
  right: number
  bottom: number
}

export interface Box {
  x: number
  y: number
  w: number
  h: number
}

/** Grow a rect by `margin` on every side. */
export function expandRect(rect: Rect, margin: number): Rect {
  return {
    bottom: rect.bottom + margin,
    left: rect.left - margin,
    right: rect.right + margin,
    top: rect.top - margin
  }
}

/** A square exclusion zone of side `2*radius` centered on a point (the cursor). */
export function rectFromPoint(x: number, y: number, radius: number): Rect {
  return { bottom: y + radius, left: x - radius, right: x + radius, top: y - radius }
}

/** True when a pet box and a zone rect overlap at all (touching edges don't count). */
export function boxOverlapsRect(box: Box, zone: Rect): boolean {
  return box.x < zone.right && box.x + box.w > zone.left && box.y < zone.bottom && box.y + box.h > zone.top
}

/**
 * True when the pet should yield (fade out of the way) because its box
 * overlaps ANY active exclusion zone. Zones are typically: the composer
 * surface (expanded by a margin) while it holds focus, and a small radius
 * around the pointer while a drag-selection is in progress.
 */
export function shouldYieldPet(box: Box, zones: Rect[]): boolean {
  return zones.some(zone => boxOverlapsRect(box, zone))
}
