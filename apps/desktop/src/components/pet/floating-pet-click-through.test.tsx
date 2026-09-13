import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Boundary mocks — everything OUTSIDE the click-through logic under test.
// Keeping @/store/pet, @/store/session, @/store/pet-overlay, @/lib/storage real
// (they are plain nanostores/localStorage) so the component drives its actual
// state machine; only the surfaces that would otherwise reach the network,
// routing, or Electron bridges are stubbed.
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ requestGateway: vi.fn().mockRejectedValue(new Error('unused in test')) })
}))
vi.mock('@/app/hooks/use-on-profile-switch', () => ({ useOnProfileSwitch: () => undefined }))
vi.mock('@/app/hooks/use-route-overlay-active', () => ({ useRouteOverlayActive: () => false }))
vi.mock('@/components/chat/vibe-hearts', () => ({ PetHeartField: () => null }))
vi.mock('@/store/windows', () => ({ isSecondaryWindow: () => false }))
vi.mock('@/themes/context', () => ({ useTheme: () => ({ resolvedMode: 'dark' }) }))

import type * as PetStore from '@/store/pet'
import type * as PetOverlayStore from '@/store/pet-overlay'
import type * as SessionStore from '@/store/session'
import { reactRoot } from '@/test/react-root'

import { FloatingPet } from './floating-pet'

const mount = reactRoot()

const PET_INFO = {
  enabled: true,
  frameW: 20,
  frameH: 20,
  framesPerState: 1,
  loopMs: 1000,
  scale: 1,
  spritesheetBase64: 'stub',
  stateRows: ['idle']
}

// Alpha-sampling canvas mock: solid everywhere inside a configurable "sprite
// rect" (in canvas-local pixel space), transparent everywhere else — enough
// to drive isSolidCanvasPixel's real geometry + threshold logic end to end.
function mockCanvasAlpha(spriteRect: { x: number; y: number; w: number; h: number }) {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    getImageData: (px: number, py: number) => {
      const inSprite = px >= spriteRect.x && px < spriteRect.x + spriteRect.w && py >= spriteRect.y && py < spriteRect.y + spriteRect.h

      return { data: new Uint8ClampedArray([0, 0, 0, inSprite ? 255 : 0]) }
    }
  } as unknown as CanvasRenderingContext2D)
}

let petStores: typeof PetStore
let sessionStore: typeof SessionStore
let overlayStore: typeof PetOverlayStore

beforeEach(async () => {
  ;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
  vi.useFakeTimers()

  petStores = await import('@/store/pet')
  sessionStore = await import('@/store/session')
  overlayStore = await import('@/store/pet-overlay')

  petStores.setPetInfo(PET_INFO)
  petStores.$petActivity.set({})
  // Keep the poll effect a no-op — gatewayState !== 'open' short-circuits it
  // before any requestGateway call, so the test never touches the network.
  sessionStore.$gatewayState.set('idle')
  overlayStore.$petOverlayActive.set(false)

  // The pet renders at a fixed container position; give it a stable on-screen
  // rect so pointer coordinates below are meaningful.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    bottom: 20,
    height: 20,
    left: 0,
    right: 20,
    toJSON: () => ({}),
    top: 0,
    width: 20,
    x: 0,
    y: 0
  } as DOMRect)
})

afterEach(() => {
  mount.unmount()
  vi.useRealTimers()
  vi.restoreAllMocks()
  petStores.setPetInfo({ enabled: false })
})

function containerEl(): HTMLDivElement {
  const canvas = mount.container?.querySelector('canvas')
  const el = canvas?.closest('div[style*="position: fixed"]') as HTMLDivElement | null

  expect(el).not.toBeNull()

  return el!
}

// Regression for #95001 / t_38157d53: the floating pet used to be
// pointer-events:auto over its FULL bounding box, so whenever it happened to
// be standing over conversation text it silently swallowed clicks,
// double-clicks, and drag-selects there — no error, the interaction just did
// nothing. These tests pin the fix: the pet defaults to click-through and
// only turns solid while the pointer sits on an actually-opaque sprite pixel.
describe('FloatingPet click-through (regression #95001)', () => {
  it('defaults to pointer-events:none so it never blocks the text underneath it', () => {
    mockCanvasAlpha({ h: 0, w: 0, x: 0, y: 0 }) // nothing is solid

    act(() => {
      mount.render(<FloatingPet />)
    })

    // This is the literal assertion that fails against the pre-fix source,
    // which set `pointerEvents: 'auto'` unconditionally in the inline style.
    expect(containerEl().style.pointerEvents).toBe('none')
  })

  it('stays click-through while the pointer moves over transparent padding of the pet box', () => {
    mockCanvasAlpha({ h: 0, w: 0, x: 0, y: 0 }) // fully transparent canvas

    act(() => {
      mount.render(<FloatingPet />)
    })

    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 10, clientY: 10 }))
    })

    expect(containerEl().style.pointerEvents).toBe('none')
  })

  it('turns interactive only once the pointer sits on an opaque sprite pixel', () => {
    // Sprite occupies the left half of the 20x20 canvas.
    mockCanvasAlpha({ h: 20, w: 10, x: 0, y: 0 })

    act(() => {
      mount.render(<FloatingPet />)
    })

    // Pointer over the transparent right half: still click-through.
    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 15, clientY: 10 }))
    })
    expect(containerEl().style.pointerEvents).toBe('none')

    // Pointer over the opaque left half: the pet itself becomes clickable.
    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 5, clientY: 10 }))
    })
    expect(containerEl().style.pointerEvents).toBe('auto')
  })

  it('re-tests on the 120ms poll so a pet roaming under a motionless cursor releases the click zone', () => {
    // Start fully solid so the initial move over it goes interactive.
    mockCanvasAlpha({ h: 20, w: 20, x: 0, y: 0 })

    act(() => {
      mount.render(<FloatingPet />)
    })

    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 10, clientY: 10 }))
    })
    expect(containerEl().style.pointerEvents).toBe('auto')

    // The sprite "walks away" from underneath the still cursor — no pointermove
    // fires (the cursor never moved), only the poll can notice.
    mockCanvasAlpha({ h: 0, w: 0, x: 0, y: 0 })

    act(() => {
      vi.advanceTimersByTime(120)
    })

    expect(containerEl().style.pointerEvents).toBe('none')
  })

  it('never drops pointer capture mid-drag, however far the pointer strays from the sprite', () => {
    mockCanvasAlpha({ h: 20, w: 20, x: 0, y: 0 })

    act(() => {
      mount.render(<FloatingPet />)
    })

    const el = containerEl()
    el.setPointerCapture = vi.fn()
    el.releasePointerCapture = vi.fn()

    act(() => {
      el.dispatchEvent(
        new PointerEvent('pointerdown', { bubbles: true, clientX: 10, clientY: 10, pointerId: 1 })
      )
    })

    // Now the canvas reads fully transparent (as if the cursor wandered off
    // the sprite mid-drag) and the pointer moves far outside the pet's rect.
    mockCanvasAlpha({ h: 0, w: 0, x: 0, y: 0 })

    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 500, clientY: 500 }))
    })

    // The drag owns the gesture — pointer-events must stay 'auto' so the drag
    // keeps receiving events (see floating-pet.tsx's isOverSolidPixel dragRef
    // early-return).
    expect(el.style.pointerEvents).toBe('auto')
  })
})
