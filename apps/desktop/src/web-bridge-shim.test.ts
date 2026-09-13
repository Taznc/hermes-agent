import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// web-bridge-shim.ts installs `window.hermesDesktop` as a side effect of
// being imported, and reads `import.meta.env.DEV` / localStorage / the URL
// at module-eval time — so every test gets a fresh module instance via
// vi.resetModules() + dynamic import, matching how index-web.html loads it
// once before src/main.tsx.
async function loadShim(): Promise<{ api: <T>(request: Record<string, unknown>) => Promise<T> }> {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (window as unknown as { hermesDesktop: { api: <T>(request: Record<string, unknown>) => Promise<T> } })
    .hermesDesktop
}

describe('web-bridge-shim reload trap re-entrancy guard', () => {
  afterEach(() => {
    Reflect.deleteProperty(window as unknown as Record<string, unknown>, '__hermesWebReloadTrapInstalled')
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.unstubAllGlobals()
  })

  it('a second module evaluation does not re-trap window.location.reload', async () => {
    // jsdom's real window.location.reload is a non-configurable own
    // property, so Object.defineProperty on it throws even here — the exact
    // failure mode web-bridge-shim.ts's try/catch exists to survive. Stub a
    // configurable location so the trap can actually install, matching a
    // real browser where `reload` IS configurable.
    const originalReload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload: originalReload })

    vi.resetModules()
    const webReload = await import('./store/web-reload')
    const registerSpy = vi.spyOn(webReload, 'registerNativeWebReload')

    await import('./web-bridge-shim')
    const reloadAfterFirstInstall = window.location.reload

    expect(registerSpy).toHaveBeenCalledTimes(1)
    expect(reloadAfterFirstInstall).not.toBe(originalReload)
    expect((window as unknown as Record<string, unknown>).__hermesWebReloadTrapInstalled).toBe(true)

    // A fresh module instance (mirrors Vite HMR re-evaluating this file
    // without a real page navigation) must be a no-op: it must not throw,
    // and it must not re-capture the already-trapped reload as "native".
    vi.resetModules()
    await expect(import('./web-bridge-shim')).resolves.toBeDefined()

    expect(registerSpy).toHaveBeenCalledTimes(1)
    expect(window.location.reload).toBe(reloadAfterFirstInstall)
  })
})

describe('web-bridge-shim api() default timeout', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.useFakeTimers()
    fetchMock = vi.fn(
      (_url: unknown, init?: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          // Simulates a stalled socket / dead backend: only settles if the
          // AbortController fires, exactly like a real fetch() would.
          init?.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'))
          })
        })
    )
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('rejects a hung request at the 30s default when no timeoutMs is given', async () => {
    const { api } = await loadShim()

    const pending = api({ path: '/api/whatever' })
    const assertion = expect(pending).rejects.toThrow()

    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
  })

  it('does not reject before the 30s default fires', async () => {
    const { api } = await loadShim()

    const pending = api({ path: '/api/whatever' })
    let settled = false

    pending.catch(() => {
      settled = true
    })

    await vi.advanceTimersByTimeAsync(29_000)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(settled).toBe(true)
  })

  it('an explicit timeoutMs only RAISES the budget above the 30s default', async () => {
    const { api } = await loadShim()

    const pending = api({ path: '/api/whatever', timeoutMs: 60_000 })
    let settled = false

    pending.catch(() => {
      settled = true
    })

    await vi.advanceTimersByTimeAsync(30_000)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(30_000)
    expect(settled).toBe(true)
  })
})
