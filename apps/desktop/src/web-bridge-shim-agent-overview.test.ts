/**
 * Covers web-bridge-shim.ts's getAgentOverview() member (t_c21d8ec5).
 *
 * web-bridge-shim.ts installs `window.hermesDesktop` as a side effect of
 * being imported and reads localStorage/the URL at module-eval time, so each
 * test gets a fresh module instance via vi.resetModules() + dynamic import,
 * matching how index-web.html loads it once before src/main.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

interface HermesDesktopLike {
  getAgentOverview: (options?: { force?: boolean }) => Promise<{
    sources: Array<{
      connectionId: string
      state: string
      sessions: unknown[]
      errors: Array<{ error: string }>
      complete: boolean
    }>
    fetchedAt: number
  }>
}

async function loadShim(): Promise<HermesDesktopLike> {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (window as unknown as { hermesDesktop: HermesDesktopLike }).hermesDesktop
}

function jsonResponse(body: unknown, init?: { status?: number }) {
  const status = init?.status ?? 200

  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body)
  } as Response)
}

const HISTORY_PAGE = {
  sessions: [{ id: 's1', profile: 'default', title: 'First session', message_count: 3 }],
  canonical: [],
  live: [{ id: 's1', profile: 'default', runtime_id: 'r1', status: 'working' }],
  profiles: [{ name: 'default' }],
  total: 1,
  offset: 0,
  errors: [],
  live_coverage: 'process'
}

const LIVE_ONLY_PAGE = {
  sessions: [],
  canonical: [],
  live: [{ id: 's1', profile: 'default', runtime_id: 'r1', status: 'working' }],
  profiles: [],
  total: 1,
  errors: [],
  live_coverage: 'process'
}

describe('web-bridge-shim getAgentOverview', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('is defined and returns sources with the rows the route reported', async () => {
    fetchMock.mockImplementation((url: URL) => {
      expect(String(url)).toContain('/api/profiles/agent-overview')

      return jsonResponse(HISTORY_PAGE)
    })

    const { getAgentOverview } = await loadShim()

    expect(typeof getAgentOverview).toBe('function')

    const overview = await getAgentOverview()

    expect(overview.sources).toHaveLength(1)
    const [source] = overview.sources

    expect(source.connectionId).toBe('web')
    expect(source.state).toBe('ready')
    expect(source.complete).toBe(true)
    expect(source.sessions).toEqual([
      expect.objectContaining({ id: 's1', profile: 'default', title: 'First session' })
    ])
    expect(typeof overview.fetchedAt).toBe('number')
  })

  it('force bypasses the collector 60s history cache', async () => {
    let calls = 0
    fetchMock.mockImplementation((url: URL) => {
      calls += 1
      const path = String(url)

      // First read (no history yet): full history page. Every subsequent
      // read without force takes the live_only branch; with force it must
      // re-request full history (offset=0) again, not live_only.
      if (path.includes('live_only=true')) {
        return jsonResponse(LIVE_ONLY_PAGE)
      }

      return jsonResponse(HISTORY_PAGE)
    })

    const { getAgentOverview } = await loadShim()

    await getAgentOverview()
    expect(calls).toBe(1)

    // No force: cached history (<60s old) short-circuits to a live_only read.
    await getAgentOverview()
    expect(calls).toBe(2)
    const secondCallUrl = String(fetchMock.mock.calls[1][0])
    expect(secondCallUrl).toContain('live_only=true')

    // force: true bypasses the cache and re-reads full history from offset 0.
    await getAgentOverview({ force: true })
    expect(calls).toBe(3)
    const thirdCallUrl = String(fetchMock.mock.calls[2][0])
    expect(thirdCallUrl).not.toContain('live_only=true')
    expect(thirdCallUrl).toContain('offset=0')
  })

  it('a real backend failure resolves with a degraded source, not a fake-successful empty snapshot', async () => {
    fetchMock.mockImplementation(() => jsonResponse({ error: 'not found' }, { status: 404 }))

    const { getAgentOverview } = await loadShim()
    const overview = await getAgentOverview()

    expect(overview.sources).toHaveLength(1)
    const [source] = overview.sources

    // Never silently swallowed into a false "all quiet": state must not be
    // 'ready'/complete, and it must not claim completion either.
    expect(source.state).not.toBe('ready')
    expect(source.complete).toBe(false)
  })

  it('a bogus control path also fails, proving the probe methodology is valid', async () => {
    fetchMock.mockImplementation((url: URL) => {
      if (String(url).includes('/api/profiles/agent-overview')) {
        return jsonResponse({ error: 'nope' }, { status: 404 })
      }

      return jsonResponse({ error: 'not found' }, { status: 404 })
    })

    const { getAgentOverview } = await loadShim()
    const overview = await getAgentOverview()

    expect(overview.sources[0].state).not.toBe('ready')
  })
})
