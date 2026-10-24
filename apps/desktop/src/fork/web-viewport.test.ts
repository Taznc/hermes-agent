import { afterEach, describe, expect, it } from 'vitest'

import { installBrowserViewport } from './web-viewport'

type Listener = () => void

function fakeWindow(innerHeight: number, vv: { height: number; offsetTop?: number; scale?: number }) {
  const listeners = new Map<string, Set<Listener>>()

  const on = (type: string, fn: Listener) => {
    if (!listeners.has(type)) {
      listeners.set(type, new Set())
    }

    listeners.get(type)!.add(fn)
  }

  const off = (type: string, fn: Listener) => listeners.get(type)?.delete(fn)

  const viewport = {
    height: vv.height,
    offsetTop: vv.offsetTop ?? 0,
    scale: vv.scale ?? 1,
    addEventListener: on,
    removeEventListener: off
  }

  const win = {
    document,
    innerHeight,
    visualViewport: viewport,
    getComputedStyle: () => ({ zoom: '1' }),
    // Synchronous frame: returns 0 so the "frame pending" guard is clear
    // again once the callback has already run.
    requestAnimationFrame: (fn: () => void) => {
      fn()

      return 0
    },
    cancelAnimationFrame: () => {},
    addEventListener: (type: string, fn: Listener) => on(`win:${type}`, fn),
    removeEventListener: (type: string, fn: Listener) => off(`win:${type}`, fn)
  }

  const fire = (type: string) => listeners.get(type)?.forEach(fn => fn())

  return { win: win as unknown as Window, viewport, fire, listeners }
}

describe('installBrowserViewport (fork web desktop)', () => {
  let root: HTMLDivElement

  afterEach(() => {
    root?.remove()
    delete document.documentElement.dataset.hermesDesktopHost
  })

  const mount = (host: boolean) => {
    root = document.createElement('div')
    root.id = 'root'
    document.body.append(root)

    if (host) {
      document.documentElement.dataset.hermesDesktopHost = 'browser'
    }
  }

  it('is inert outside the browser host (Electron windows)', () => {
    mount(false)
    const { win, listeners } = fakeWindow(664, { height: 400 })
    installBrowserViewport(win)
    expect(root.hasAttribute('data-browser-viewport')).toBe(false)
    expect(listeners.size).toBe(0)
  })

  it('leaves layout alone when the visual viewport equals innerHeight (iOS Safari)', () => {
    mount(true)
    const { win } = fakeWindow(664, { height: 664 })
    installBrowserViewport(win)
    expect(root.hasAttribute('data-browser-viewport')).toBe(false)
  })

  it('fits the shell to the visible area when a keyboard shrinks only the visual viewport', () => {
    mount(true)
    const { win, viewport, fire } = fakeWindow(664, { height: 664 })
    const dispose = installBrowserViewport(win)

    viewport.height = 380
    viewport.offsetTop = 20
    fire('resize')
    expect(root.getAttribute('data-browser-viewport')).toBe('')
    expect(root.style.getPropertyValue('--browser-viewport-height')).toBe('380px')
    expect(root.style.getPropertyValue('--browser-viewport-top')).toBe('20px')

    viewport.height = 664
    viewport.offsetTop = 0
    fire('resize')
    expect(root.hasAttribute('data-browser-viewport')).toBe(false)

    viewport.height = 300
    fire('resize')
    dispose()
    expect(root.hasAttribute('data-browser-viewport')).toBe(false)
    expect(root.style.getPropertyValue('--browser-viewport-height')).toBe('')
  })

  it('keeps the app layout during a pinch zoom (height is unscaled)', () => {
    mount(true)
    const { win } = fakeWindow(664, { height: 332, scale: 2 })
    installBrowserViewport(win)
    expect(root.hasAttribute('data-browser-viewport')).toBe(false)
  })
})
