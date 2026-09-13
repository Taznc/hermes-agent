import { afterEach, describe, expect, it, vi } from 'vitest'

import { nativeContextMenuHandled } from './target'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

afterEach(() => {
  delete desktopWindow.hermesDesktop
})

describe('nativeContextMenuHandled', () => {
  it('is false when window.hermesDesktop is entirely absent (no bridge object at all)', () => {
    expect(nativeContextMenuHandled()).toBe(false)
  })

  it('is false when the bridge exists but omits contextMenuEdit — the web build shim', () => {
    // Mirrors web-bridge-shim.ts: the shim object is present (boot succeeded)
    // but deliberately never defines contextMenuEdit.
    desktopWindow.hermesDesktop = {
      openExternal: vi.fn()
    } as unknown as Window['hermesDesktop']

    expect(nativeContextMenuHandled()).toBe(false)
  })

  it('is true when contextMenuEdit is a function — the real Electron preload bridge', () => {
    desktopWindow.hermesDesktop = {
      contextMenuEdit: vi.fn().mockResolvedValue(undefined)
    } as unknown as Window['hermesDesktop']

    expect(nativeContextMenuHandled()).toBe(true)
  })
})
