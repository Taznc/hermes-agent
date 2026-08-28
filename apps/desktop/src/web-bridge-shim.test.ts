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
