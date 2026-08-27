import { afterEach, describe, expect, it } from 'vitest'

import { ALPHA_HIT_THRESHOLD, isSolidCanvasPixel } from './pet-hit-test'

function paintCanvas(): HTMLCanvasElement {
  const canvas = document.createElement('canvas')

  canvas.width = 10
  canvas.height = 10
  // Real JSDOM canvas has no 2D backend, so getContext('2d') returns null there —
  // stub a minimal one that reports a fixed alpha per call, driven by the test.
  document.body.append(canvas)

  return canvas
}

afterEach(() => {
  document.body.replaceChildren()
})

describe('isSolidCanvasPixel', () => {
  it('is false outside the canvas rect regardless of pixel content', () => {
    const canvas = paintCanvas()

    Object.defineProperty(canvas, 'getBoundingClientRect', {
      value: () => ({ bottom: 10, height: 10, left: 0, right: 10, top: 0, width: 10 })
    })

    expect(isSolidCanvasPixel(canvas, 50, 50)).toBe(false)
    expect(isSolidCanvasPixel(canvas, -5, 5)).toBe(false)
  })

  it('fails open (true) when the canvas has zero size', () => {
    const canvas = paintCanvas()

    Object.defineProperty(canvas, 'getBoundingClientRect', {
      value: () => ({ bottom: 0, height: 0, left: 0, right: 0, top: 0, width: 0 })
    })

    expect(isSolidCanvasPixel(canvas, 0, 0)).toBe(true)
  })

  it('fails open (true) when 2D context or pixel read is unavailable', () => {
    const canvas = paintCanvas()

    Object.defineProperty(canvas, 'getBoundingClientRect', {
      value: () => ({ bottom: 10, height: 10, left: 0, right: 10, top: 0, width: 10 })
    })
    // jsdom's canvas has no 2D backend by default -> getContext('2d') is null,
    // which is exactly the "unreadable" path this test exercises.

    expect(isSolidCanvasPixel(canvas, 5, 5)).toBe(true)
  })

  it('respects the alpha threshold when a context IS available', () => {
    const canvas = paintCanvas()

    Object.defineProperty(canvas, 'getBoundingClientRect', {
      value: () => ({ bottom: 10, height: 10, left: 0, right: 10, top: 0, width: 10 })
    })

    let alpha = 0

    const ctx = {
      getImageData: () => ({ data: new Uint8ClampedArray([0, 0, 0, alpha]) })
    } as unknown as CanvasRenderingContext2D

    Object.defineProperty(canvas, 'getContext', { value: () => ctx })

    alpha = ALPHA_HIT_THRESHOLD - 1
    expect(isSolidCanvasPixel(canvas, 5, 5)).toBe(false)

    alpha = ALPHA_HIT_THRESHOLD
    expect(isSolidCanvasPixel(canvas, 5, 5)).toBe(true)
  })
})
