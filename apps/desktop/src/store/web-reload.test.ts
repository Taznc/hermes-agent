import { afterEach, describe, expect, it, vi } from 'vitest'

// web-reload.ts holds `nativeReload` as a module-scoped variable with no
// reset export (by design — see the module's own doc comment on
// registerNativeWebReload). Each test that cares whether a trap is installed
// needs a fresh module instance, so we reset the module registry and
// dynamically re-import between such tests rather than sharing one import
// across the file.
async function loadWebReload() {
  vi.resetModules()

  return import('./web-reload')
}

// jsdom's window.location.reload is a non-configurable own property in this
// environment (the exact constraint web-bridge-shim.ts's doc comment warns
// about — Object.defineProperty on it throws even with configurable: true).
// vi.stubGlobal replaces the `location` binding on window wholesale instead
// of redefining one of its properties, so it sidesteps that restriction.
function stubLocationReload() {
  const reloadMock = vi.fn()

  vi.stubGlobal('location', { ...window.location, reload: reloadMock })

  return reloadMock
}

describe('performWebReload', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('bypasses window.location.reload when a native reload is registered', async () => {
    const { performWebReload, registerNativeWebReload } = await loadWebReload()
    const reloadMock = stubLocationReload()
    const nativeReload = vi.fn()

    registerNativeWebReload(nativeReload)
    performWebReload()

    expect(nativeReload).toHaveBeenCalledTimes(1)
    expect(reloadMock).not.toHaveBeenCalled()
  })

  it('falls through to window.location.reload when no trap is installed', async () => {
    const { performWebReload } = await loadWebReload()
    const reloadMock = stubLocationReload()

    performWebReload()

    expect(reloadMock).toHaveBeenCalledTimes(1)
  })
})

describe('markWebReloadPending', () => {
  it('flips $webReloadPending to true', async () => {
    const { $webReloadPending, markWebReloadPending } = await loadWebReload()

    expect($webReloadPending.get()).toBe(false)

    markWebReloadPending()

    expect($webReloadPending.get()).toBe(true)
  })
})
