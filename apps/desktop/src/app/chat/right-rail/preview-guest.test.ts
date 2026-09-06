import { cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { openInBrowserTab, previewGuestSupported } from './preview-guest'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  delete desktopWindow.hermesDesktop
})

describe('previewGuestSupported', () => {
  // jsdom IS the web build's runtime shape: `<webview>` upgrades to nothing,
  // so the element has no loadURL. This is the state that produced the blank
  // pane the user reported.
  it('is false in a plain browser, where <webview> is an inert unknown element', () => {
    const probe = document.createElement('webview')

    expect(probe).toBeInstanceOf(HTMLElement)
    expect((probe as { loadURL?: unknown }).loadURL).toBeUndefined()
    expect(previewGuestSupported()).toBe(false)
  })

  // Electron upgrades the tag synchronously at createElement, so the methods
  // are already on the element the probe holds.
  it('is true when the created element carries the guest API', () => {
    const upgraded = document.createElement('div') as HTMLElement & { loadURL?: unknown }
    upgraded.loadURL = () => Promise.resolve()

    const createElement = vi.spyOn(document, 'createElement').mockImplementation(tag => {
      if (tag === 'webview') {
        return upgraded
      }

      throw new Error(`unexpected createElement(${tag})`)
    })

    expect(previewGuestSupported()).toBe(true)
    expect(createElement).toHaveBeenCalledWith('webview')
  })

  // The probe answers "can this document host a guest", never "who launched
  // me" — a UA/hostname/platform test would go stale the day the web build
  // gains a real guest, and lies today for anyone spoofing either.
  it('ignores the user agent and the hostname', () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (X11; Linux x86_64) HermesDesktop/1.0 Electron/38.0.0'
    )
    desktopWindow.hermesDesktop = { platform: 'darwin' } as unknown as Window['hermesDesktop']

    expect(previewGuestSupported()).toBe(false)
  })
})

describe('openInBrowserTab', () => {
  it('prefers the app bridge, which is the top-level-tab route in both builds', async () => {
    const openExternal = vi.fn(async () => undefined)
    const open = vi.spyOn(window, 'open')
    desktopWindow.hermesDesktop = { openExternal } as unknown as Window['hermesDesktop']

    await openInBrowserTab('https://google.com', 'blocked')

    expect(openExternal).toHaveBeenCalledWith('https://google.com')
    expect(open).not.toHaveBeenCalled()
  })

  it('falls back to window.open with the opener severed when no bridge is installed', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window)

    await openInBrowserTab('https://google.com', 'blocked')

    expect(open).toHaveBeenCalledWith('https://google.com', '_blank', 'noopener,noreferrer')
  })

  // A popup blocker returns null. That is a real failure the user has to see,
  // not something to swallow into another silent no-op.
  it('rejects with the caller-supplied copy when the browser blocks the tab', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)

    await expect(openInBrowserTab('https://google.com', 'Allow pop-ups.')).rejects.toThrow('Allow pop-ups.')
  })
})
