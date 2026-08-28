import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Boundary mocks — same shape as floating-pet-click-through.test.tsx: keep
// @/store/pet, @/store/session, @/store/pet-overlay, @/lib/storage real so the
// component drives its actual state machine; stub only network/routing/
// Electron-bridge surfaces.
vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ requestGateway: vi.fn().mockRejectedValue(new Error('unused in test')) })
}))
vi.mock('@/app/hooks/use-on-profile-switch', () => ({ useOnProfileSwitch: () => undefined }))
vi.mock('@/app/hooks/use-route-overlay-active', () => ({ useRouteOverlayActive: () => false }))
vi.mock('@/components/chat/vibe-hearts', () => ({ PetHeartField: () => null }))
vi.mock('@/store/windows', () => ({ isSecondaryWindow: () => false }))
vi.mock('@/themes/context', () => ({ useTheme: () => ({ resolvedMode: 'dark' }) }))

import type * as PetStore from '@/store/pet'
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

// Fully-opaque canvas everywhere — isolates these tests to the exclusion-zone
// logic rather than the alpha hit-test (already covered elsewhere).
function mockCanvasFullyOpaque() {
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    getImageData: () => ({ data: new Uint8ClampedArray([0, 0, 0, 255]) })
  } as unknown as CanvasRenderingContext2D)
}

let petStores: typeof PetStore
let sessionStore: typeof SessionStore

beforeEach(async () => {
  ;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
  vi.useFakeTimers()

  petStores = await import('@/store/pet')
  sessionStore = await import('@/store/session')
  const overlayStore = await import('@/store/pet-overlay')

  petStores.setPetInfo(PET_INFO)
  petStores.$petActivity.set({})
  sessionStore.$gatewayState.set('idle')
  overlayStore.$petOverlayActive.set(false)

  // Pet parked at a stable, known rect.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    bottom: 120,
    height: 20,
    left: 100,
    right: 120,
    toJSON: () => ({}),
    top: 100,
    width: 20,
    x: 100,
    y: 100
  } as DOMRect)

  mockCanvasFullyOpaque()
})

afterEach(() => {
  mount.unmount()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  petStores.setPetInfo({ enabled: false })
  document.body.innerHTML = ''
})

function containerEl(): HTMLDivElement {
  const canvas = mount.container?.querySelector('canvas')
  const el = canvas?.closest('div[style*="position: fixed"]') as HTMLDivElement | null

  expect(el).not.toBeNull()

  return el!
}

// Same pattern as status-pulse.test.tsx: stub matchMedia so useMediaQuery's
// prefers-reduced-motion read is deterministic in jsdom (which has no real
// implementation).
function installMatchMedia(matches: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      addEventListener: vi.fn(),
      matches,
      media: '(prefers-reduced-motion: reduce)',
      onchange: null,
      removeEventListener: vi.fn()
    }))
  )
}

// Regression coverage for Phase 2.3: the alpha hit-test (#95001) stops the pet
// from EATING clicks on transparent padding, but a fully opaque sprite parked
// on the composer or in the path of a growing text selection is still a
// non-interference violation — the user can't see what they're typing, or the
// pet visually sits where the selection is headed. These tests pin that the
// pet visually yields (fades + forces click-through) in both cases, and
// recovers the instant the zone clears.
describe('FloatingPet non-interference: exclusion zones (Phase 2.3)', () => {
  it('stays fully opaque and normally interactive with no composer focused and no drag in progress', () => {
    act(() => {
      mount.render(<FloatingPet />)
    })

    act(() => {
      vi.advanceTimersByTime(120)
    })

    expect(containerEl().style.opacity).toBe('1')
  })

  it('fades and yields click-through while a composer input holds focus, overlap or not', () => {
    document.body.innerHTML = '<div data-slot="composer-surface"><input data-slot="composer-rich-input" /></div>'
    const input = document.querySelector('[data-slot="composer-rich-input"]') as HTMLInputElement

    vi.spyOn(input, 'closest').mockReturnValue(input)
    vi.spyOn(document.querySelector('[data-slot="composer-surface"]')!, 'getBoundingClientRect').mockReturnValue({
      bottom: 500,
      height: 40,
      left: 0,
      right: 300,
      toJSON: () => ({}),
      top: 460,
      width: 300,
      x: 0,
      y: 460
    } as DOMRect)

    act(() => {
      mount.render(<FloatingPet />)
    })

    // Pet parked at y:100-120, composer at y:460-500 — no geometric overlap,
    // but the composer holding focus is still a standing exclusion zone.
    act(() => {
      input.focus()
      vi.advanceTimersByTime(120)
    })

    expect(containerEl().style.opacity).toBe('0.16')

    // Pointer sits on the (fully opaque) sprite — normally that would go
    // interactive, but yielding must force click-through regardless.
    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 110, clientY: 110 }))
    })
    expect(containerEl().style.pointerEvents).toBe('none')

    // Blurring the composer releases the zone.
    act(() => {
      input.blur()
      vi.advanceTimersByTime(120)
    })
    expect(containerEl().style.opacity).toBe('1')
  })

  it('fades while a drag-selection radius reaches toward the pet, and recovers on pointer up', () => {
    act(() => {
      mount.render(<FloatingPet />)
    })

    // Press far from the pet, then drag toward it — a growing text selection,
    // not the pet's own drag handle (which starts ON the pet).
    act(() => {
      window.dispatchEvent(new PointerEvent('pointerdown', { clientX: 400, clientY: 400 }))
    })
    expect(containerEl().style.opacity).toBe('1')

    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { clientX: 150, clientY: 110 }))
    })
    expect(containerEl().style.opacity).toBe('0.16')

    act(() => {
      window.dispatchEvent(new PointerEvent('pointerup', { clientX: 150, clientY: 110 }))
      vi.advanceTimersByTime(120)
    })
    expect(containerEl().style.opacity).toBe('1')
  })

  it('does not yield for a press that starts on the pet itself (its own drag handle)', () => {
    act(() => {
      mount.render(<FloatingPet />)
    })

    const el = containerEl()

    el.setPointerCapture = vi.fn()
    el.releasePointerCapture = vi.fn()

    act(() => {
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: 110, clientY: 110, pointerId: 1 }))
    })

    act(() => {
      vi.advanceTimersByTime(120)
    })

    expect(containerEl().style.opacity).toBe('1')
  })

  it('skips the fade transition under prefers-reduced-motion', () => {
    installMatchMedia(true)

    document.body.innerHTML = '<div data-slot="composer-surface"><input data-slot="composer-rich-input" /></div>'
    const input = document.querySelector('[data-slot="composer-rich-input"]') as HTMLInputElement

    vi.spyOn(input, 'closest').mockReturnValue(input)
    vi.spyOn(document.querySelector('[data-slot="composer-surface"]')!, 'getBoundingClientRect').mockReturnValue({
      bottom: 500,
      height: 40,
      left: 0,
      right: 300,
      toJSON: () => ({}),
      top: 460,
      width: 300,
      x: 0,
      y: 460
    } as DOMRect)

    act(() => {
      mount.render(<FloatingPet />)
    })

    expect(containerEl().style.transition).toBe('none')

    act(() => {
      input.focus()
      vi.advanceTimersByTime(120)
    })

    expect(containerEl().style.opacity).toBe('0.16')
    expect(containerEl().style.transition).toBe('none')
  })
})
