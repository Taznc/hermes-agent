import { type RefObject, useEffect, useRef, useState } from 'react'

import { type Box, expandRect, type Rect, rectFromPoint, shouldYieldPet } from './pet-exclusion'

// How far past the composer's own box the exclusion zone extends — enough
// that the pet visibly clears the input area, not just its exact edge.
const COMPOSER_MARGIN_PX = 28
// Radius around the pointer while a press/drag (double-click, drag-select) is
// in progress — big enough that the pet gets out from under a selection
// that's still growing toward it, not just the exact pixel under the cursor.
const POINTER_DRAG_RADIUS_PX = 90
// Re-poll cadence: mirrors the click-through poll (`floating-pet.tsx`) so a
// roaming pet that wanders into a zone under a motionless pointer/composer is
// caught promptly without a dedicated per-frame loop.
const POLL_MS = 120

const COMPOSER_SURFACE_SELECTOR = '[data-slot="composer-surface"]'
const COMPOSER_INPUT_SELECTOR = '[data-slot="composer-rich-input"]'

interface PetExclusionOptions {
  containerRef: RefObject<HTMLElement | null>
  /** Gate the whole effect (mirrors the pet's actual mounted/active condition). */
  enabled: boolean
}

/**
 * True while the pet's own box overlaps an active exclusion zone and it
 * should visually yield (fade out of the way) instead of sitting on top of
 * what the user is doing.
 *
 * Two zones, matching the two ways the pet can get between the user and their
 * text even after the alpha hit-test (`pet-hit-test.ts`) stopped it from
 * silently EATING clicks there (#95001):
 *
 *  - **The active composer.** While a composer input holds keyboard focus,
 *    its surface (expanded by a margin) is a standing exclusion zone — a pet
 *    parked or roaming over the box you're typing into is still a problem
 *    even though clicking through it now works, because you can't see what
 *    you're typing.
 *  - **The pointer during a press.** A double-click or drag-select is a
 *    pointer *down* somewhere followed by movement before *up* — exactly the
 *    interaction #95001 broke. A generous radius around the pointer while a
 *    button is held covers the selection as it grows, not just its start
 *    point. A press that starts ON the pet itself is the pet's own drag
 *    handle, not a text selection, and is excluded.
 *
 * Both zones are recomputed from live layout each check (never cached across
 * renders), so a resize, composer growing, or the roam loop moving the pet
 * under a stationary pointer are all caught by the poll, not just the events
 * that normally drive a recheck.
 */
export function usePetExclusion({ containerRef, enabled }: PetExclusionOptions): boolean {
  const [yielding, setYielding] = useState(false)
  const draggingRef = useRef(false)
  const pointerRef = useRef<{ x: number; y: number } | null>(null)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref writes (drag/pointer interaction state, not atom mirrors)
  useEffect(() => {
    if (!enabled) {
      setYielding(false)

      return
    }

    const composerZone = (): Rect | null => {
      const focused = document.activeElement?.closest(COMPOSER_INPUT_SELECTOR)

      if (!focused) {
        return null
      }

      const surface = focused.closest(COMPOSER_SURFACE_SELECTOR) ?? document.querySelector(COMPOSER_SURFACE_SELECTOR)
      const rect = surface?.getBoundingClientRect()

      return rect ? expandRect(rect, COMPOSER_MARGIN_PX) : null
    }

    const pointerZone = (): Rect | null => {
      if (!draggingRef.current || !pointerRef.current) {
        return null
      }

      return rectFromPoint(pointerRef.current.x, pointerRef.current.y, POINTER_DRAG_RADIUS_PX)
    }

    const evaluate = () => {
      const el = containerRef.current

      if (!el) {
        return
      }

      const rect = el.getBoundingClientRect()
      const box: Box = { h: rect.height, w: rect.width, x: rect.left, y: rect.top }
      const zones = [composerZone(), pointerZone()].filter((zone): zone is Rect => zone !== null)

      setYielding(prev => {
        const next = shouldYieldPet(box, zones)

        return prev === next ? prev : next
      })
    }

    const onPointerDown = (e: PointerEvent) => {
      const target = e.target

      // A press starting on the pet itself is the pet's own drag handle
      // (floating-pet.tsx's onPointerDown), not a text selection — don't make
      // the pet fade out from under its own drag.
      if (containerRef.current && target instanceof Node && containerRef.current.contains(target)) {
        return
      }

      draggingRef.current = true
      pointerRef.current = { x: e.clientX, y: e.clientY }
      evaluate()
    }

    const onPointerMove = (e: PointerEvent) => {
      pointerRef.current = { x: e.clientX, y: e.clientY }

      if (draggingRef.current) {
        evaluate()
      }
    }

    const onPointerUp = () => {
      draggingRef.current = false
      evaluate()
    }

    window.addEventListener('pointerdown', onPointerDown, { passive: true })
    window.addEventListener('pointermove', onPointerMove, { passive: true })
    window.addEventListener('pointerup', onPointerUp, { passive: true })
    document.addEventListener('focusin', evaluate)
    document.addEventListener('focusout', evaluate)

    evaluate()
    const poll = window.setInterval(evaluate, POLL_MS)

    return () => {
      window.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
      document.removeEventListener('focusin', evaluate)
      document.removeEventListener('focusout', evaluate)
      window.clearInterval(poll)
    }
  }, [enabled, containerRef])

  return yielding
}
