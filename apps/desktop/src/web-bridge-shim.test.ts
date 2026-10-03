import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// web-bridge-shim.ts installs `window.hermesDesktop` as a side effect of
// being imported, and reads localStorage / the URL at module-eval time — so every test gets a fresh module instance via
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

// ── desktop-plugin door (fork-web-desktop-bridge backend plugin) ─────────────────
interface BridgeMembers {
  desktopPluginsRoot: () => Promise<string>
  agentPluginsRoot: () => Promise<string>
  readPluginSource: (filePath: string) => Promise<{ text: string; byteSize: number }>
  probePluginRepo: (payload: { identifier?: string; repo?: string }) => Promise<Record<string, unknown>>
  installDesktopPlugin: (payload: { identifier?: string; force?: boolean }) => Promise<Record<string, unknown>>
}

async function loadBridge(): Promise<BridgeMembers> {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (window as unknown as { hermesDesktop: BridgeMembers }).hermesDesktop
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('web-bridge-shim desktop-plugin door', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let handler: (url: URL, init: RequestInit) => Response

  beforeEach(() => {
    handler = () => jsonResponse(500, {})
    fetchMock = vi.fn(async (input: URL | string, init: RequestInit = {}) => handler(new URL(String(input)), init))
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  // Import-time side requests (e.g. /api/local-models/status) are not ours.
  function bridgeCalls(): [URL, RequestInit][] {
    return fetchMock.mock.calls
      .map(([input, init]) => [new URL(String(input)), (init ?? {}) as RequestInit] as [URL, RequestInit])
      .filter(([url]) => url.pathname.startsWith('/api/plugins/fork-web-desktop-bridge/'))
  }

  function calledUrl(index = 0): URL {
    return bridgeCalls()[index][0]
  }

  it('desktopPluginsRoot resolves the plugin route path', async () => {
    handler = () => jsonResponse(200, { path: '/srv/home/desktop-plugins' })
    const bridge = await loadBridge()

    await expect(bridge.desktopPluginsRoot()).resolves.toBe('/srv/home/desktop-plugins')
    expect(calledUrl().pathname).toBe('/api/plugins/fork-web-desktop-bridge/desktop-plugins-root')
  })

  it('desktopPluginsRoot answers "" on 404 and stops re-requesting the absent route', async () => {
    handler = () => jsonResponse(404, { detail: 'Not Found' })
    const bridge = await loadBridge()

    await expect(bridge.desktopPluginsRoot()).resolves.toBe('')
    await expect(bridge.desktopPluginsRoot()).resolves.toBe('')
    await expect(bridge.agentPluginsRoot()).resolves.toBe('')
    expect(bridgeCalls()).toHaveLength(1)
  })

  it('desktopPluginsRoot answers "" on a transient failure but retries next time', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    handler = () => jsonResponse(503, {})
    const bridge = await loadBridge()

    await expect(bridge.desktopPluginsRoot()).resolves.toBe('')
    handler = () => jsonResponse(200, { path: '/p' })
    await expect(bridge.desktopPluginsRoot()).resolves.toBe('/p')
    expect(bridgeCalls()).toHaveLength(2)
    expect(warn.mock.calls.filter(([msg]) => String(msg).includes('desktop-plugins-root'))).toHaveLength(1)
  })

  it('a successful probe clears the absent latch so the disk scan resumes', async () => {
    handler = () => jsonResponse(404, {})
    const bridge = await loadBridge()
    await bridge.desktopPluginsRoot()

    handler = url =>
      url.pathname.endsWith('/probe')
        ? jsonResponse(200, { ok: true, agent: false, desktop: true, warnings: [] })
        : jsonResponse(200, { path: '/now-enabled' })

    await bridge.probePluginRepo({ identifier: 'owner/repo' })
    await expect(bridge.desktopPluginsRoot()).resolves.toBe('/now-enabled')
  })

  it('readPluginSource passes the absolute path and returns the full text', async () => {
    handler = () => jsonResponse(200, { byteSize: 3, path: '/r/p/plugin.js', text: 'x()', truncated: false })
    const bridge = await loadBridge()

    await expect(bridge.readPluginSource('/r/p/plugin.js')).resolves.toMatchObject({ text: 'x()' })
    expect(calledUrl().pathname).toBe('/api/plugins/fork-web-desktop-bridge/read-plugin-source')
    expect(calledUrl().searchParams.get('path')).toBe('/r/p/plugin.js')
  })

  it.each([403, 404, 413])('readPluginSource rejects on %i instead of yielding partial source', async status => {
    handler = () => jsonResponse(status, {})
    const bridge = await loadBridge()

    await expect(bridge.readPluginSource('/r/p/plugin.js')).rejects.toThrow(String(status))
  })

  it('probePluginRepo POSTs the trimmed identifier (repo alias accepted) and fills warnings', async () => {
    handler = () => jsonResponse(200, { ok: true, agent: true, desktop: false, agentName: 'a' })
    const bridge = await loadBridge()

    await expect(bridge.probePluginRepo({ repo: '  owner/repo ' })).resolves.toMatchObject({
      ok: true,
      agent: true,
      warnings: []
    })
    const [, init] = bridgeCalls()[0]
    expect(calledUrl().pathname).toBe('/api/plugins/fork-web-desktop-bridge/probe')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({ identifier: 'owner/repo' })
  })

  it('probePluginRepo resolves ok:false (never rejects) when the plugin is not enabled', async () => {
    handler = () => jsonResponse(404, {})
    const bridge = await loadBridge()

    const result = await bridge.probePluginRepo({ identifier: 'owner/repo' })
    expect(result).toMatchObject({ ok: false, agent: false, desktop: false, warnings: [] })
    expect(String(result.error)).toMatch(/web-desktop-bridge/)
  })

  it('probePluginRepo resolves ok:false on network failure', async () => {
    fetchMock.mockImplementation(async () => {
      throw new TypeError('Failed to fetch')
    })
    const bridge = await loadBridge()

    await expect(bridge.probePluginRepo({ identifier: 'owner/repo' })).resolves.toMatchObject({ ok: false })
  })

  it('installDesktopPlugin POSTs identifier + force and returns the backend result verbatim', async () => {
    handler = () => jsonResponse(200, { ok: true, pluginName: 'p', path: '/r/p' })
    const bridge = await loadBridge()

    await expect(bridge.installDesktopPlugin({ identifier: 'owner/repo', force: true })).resolves.toEqual({
      ok: true,
      pluginName: 'p',
      path: '/r/p'
    })
    const [, init] = bridgeCalls()[0]
    expect(calledUrl().pathname).toBe('/api/plugins/fork-web-desktop-bridge/desktop-install')
    expect(calledUrl().searchParams.has('profile')).toBe(false)
    expect(JSON.parse(String(init.body))).toEqual({ identifier: 'owner/repo', force: true })
  })

  it('installDesktopPlugin resolves ok:false on 404', async () => {
    handler = () => jsonResponse(404, {})
    const bridge = await loadBridge()

    await expect(bridge.installDesktopPlugin({ identifier: 'owner/repo' })).resolves.toMatchObject({ ok: false })
  })
})
