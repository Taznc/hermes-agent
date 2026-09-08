import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The shim installs `window.hermesDesktop` as an import side effect and reads
// import.meta.env / localStorage / the URL at module-eval time, so each test
// takes a fresh instance — same pattern as web-bridge-shim.test.ts.
interface PluginBridge {
  installDesktopPlugin: (payload: { identifier?: string; repo?: string; force?: boolean }) => Promise<{
    ok: boolean
    pluginName?: string
    path?: string
    error?: string
  }>
  probePluginRepo: (payload: { identifier?: string; repo?: string }) => Promise<{
    ok: boolean
    agent: boolean
    desktop: boolean
    agentName?: null | string
    desktopName?: null | string
    warnings?: string[]
    insecure?: boolean
    error?: string
  }>
}

async function loadShim(): Promise<PluginBridge> {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (window as unknown as { hermesDesktop: PluginBridge }).hermesDesktop
}

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    text: async () => JSON.stringify(payload)
  }
}

function lastCall(fetchMock: ReturnType<typeof vi.fn>) {
  const [url, init] = fetchMock.mock.calls.at(-1) as [URL, { body?: string; method?: string }]

  return { url: String(url), method: init.method, body: JSON.parse(String(init.body)) as Record<string, unknown> }
}

describe('web-bridge-shim desktop plugin install door', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('installs a desktop plugin and returns the installed path', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ ok: true, pluginName: 'account-limits', path: '/home/u/.hermes/desktop-plugins/account-limits' })
    )

    const { installDesktopPlugin } = await loadShim()
    const result = await installDesktopPlugin({ identifier: 'owner/account-limits', force: true })

    expect(result).toEqual({
      ok: true,
      pluginName: 'account-limits',
      path: '/home/u/.hermes/desktop-plugins/account-limits'
    })

    const call = lastCall(fetchMock)

    expect(call.url).toContain('/api/dashboard/desktop-plugins/install')
    expect(call.method).toBe('POST')
    expect(call.body).toEqual({ identifier: 'owner/account-limits', force: true })
  })

  // global.d.ts declares both members as `{identifier?, repo?}`; the install
  // modal passes `identifier`, but deep links carry `repo`. Dropping either
  // spelling would send an empty identifier the backend can only reject.
  it('accepts the `repo` spelling of the identifier on both members', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true, agent: false, desktop: true }))

    const { probePluginRepo, installDesktopPlugin } = await loadShim()

    await probePluginRepo({ repo: 'owner/thing' })
    expect(lastCall(fetchMock).body).toEqual({ identifier: 'owner/thing' })

    await installDesktopPlugin({ repo: 'owner/thing' })
    expect(lastCall(fetchMock).body).toEqual({ identifier: 'owner/thing', force: false })
  })

  // The modal calls both members with no try/catch, so a rejecting fetch must
  // arrive as visible `{ok:false, error}` copy rather than as an unhandled
  // rejection that strands the dialog in its probing state.
  it('surfaces a rejecting fetch as a visible error result, not a rejection', async () => {
    fetchMock.mockRejectedValue(new Error('Failed to fetch'))

    const { probePluginRepo, installDesktopPlugin } = await loadShim()

    await expect(probePluginRepo({ identifier: 'owner/thing' })).resolves.toEqual({
      ok: false,
      agent: false,
      desktop: false,
      warnings: [],
      insecure: false,
      error: 'Failed to fetch'
    })

    await expect(installDesktopPlugin({ identifier: 'owner/thing' })).resolves.toEqual({
      ok: false,
      error: 'Failed to fetch'
    })
  })

  it('surfaces a non-2xx install response as an error result, not a rejection', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'nope' }, false, 500))

    const { installDesktopPlugin } = await loadShim()
    const result = await installDesktopPlugin({ identifier: 'owner/thing' })

    expect(result.ok).toBe(false)
    expect(result.error).toContain('500')
  })

  // An agent-only repo is a normal, successful probe: the modal then offers
  // only the agent half. Reporting it as an error would block a legitimate
  // agent-plugin install behind a desktop-half failure.
  it('reports an agent-only repo as ok with desktop:false', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        ok: true,
        agent: true,
        desktop: false,
        agentName: 'demo',
        desktopName: null,
        warnings: [],
        insecure: false
      })
    )

    const { probePluginRepo } = await loadShim()
    const result = await probePluginRepo({ identifier: 'owner/demo' })

    expect(result.ok).toBe(true)
    expect(result.agent).toBe(true)
    expect(result.desktop).toBe(false)
    expect(result.error).toBeUndefined()
  })
})
