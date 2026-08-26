/**
 * Shared alpha-based "is this pixel actually part of the sprite" test.
 *
 * A pet's on-screen box is a plain rectangle, but the art inside it is not —
 * padding, the transparent gap between limbs, the halo around anti-aliased
 * edges. Treating the whole box as a solid click target means the pet blocks
 * clicks/selection on whatever content happens to be underneath its bounding
 * box, not just underneath its visible pixels (#95001: the roaming in-window
 * mascot sat on top of message text and silently ate double-click/drag-select
 * there, no error, just nothing happened — text selection "worked" everywhere
 * except wherever the pet currently stood).
 *
 * Both pet surfaces — the in-window floating pet and the popped-out desktop
 * overlay — sample the SAME canvas the sprite draws to, so this is the one
 * place that decision lives.
 */

/** A sprite pixel counts as "solid" (interactive) at/above this alpha (0-255).
 *  Low enough to catch anti-aliased edges, high enough that the faint halo
 *  around the art still clicks through. */
export const ALPHA_HIT_THRESHOLD = 16

/**
 * True when viewport point (x, y) lands on an opaque-enough pixel of
 * `canvas`. Geometry-checked against the canvas's own rect first (so a
 * point outside it is always `false`, regardless of alpha), then the pixel
 * itself is sampled via a 1×1 `getImageData` read.
 *
 * Fails OPEN (returns `true`) on anything unreadable — a canvas that never
 * lets go of the pointer is a worse bug than one that's slightly too eager
 * to grab it.
 */
export function isSolidCanvasPixel(canvas: HTMLCanvasElement, x: number, y: number): boolean {
  const rect = canvas.getBoundingClientRect()

  if (rect.width === 0 || rect.height === 0) {
    return true
  }

  if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) {
    return false
  }

  const ctx = canvas.getContext('2d')

  if (!ctx) {
    return true
  }

  const px = Math.floor((x - rect.left) * (canvas.width / rect.width))
  const py = Math.floor((y - rect.top) * (canvas.height / rect.height))

  try {
    return ctx.getImageData(px, py, 1, 1).data[3] >= ALPHA_HIT_THRESHOLD
  } catch {
    return true
  }
}
