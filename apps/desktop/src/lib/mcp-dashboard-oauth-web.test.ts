/**
 * Covers the web-shim (browser, no Electron mcpOauth bridge) fallback path
 * of completeMcpDesktopOAuth() in mcp-dashboard-oauth.ts (t_d40923b6).
 *
 * On web, window.hermesDesktop.mcpOauth is always undefined (the shim omits
 * it — a browser tab cannot host a loopback HTTP listener). Before this
 * fix, completeMcpDesktopOAuth threw "Update Hermes Desktop to support MCP
 * OAuth callbacks" unconditionally whenever bridge was absent and the scope
 * was not literally connectionId:'local' — which is exactly what mcp-setup.tsx
 * always passes on web (connectionId resolves to null there, never 'local').
 * These tests drive that exact call shape and expect a browser-native REST
 * flow (POST .../auth, GET .../oauth/flows/{id}, DELETE on cleanup) instead.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'

import { completeMcpDesktopOAuth, McpOAuthCancelled } from './mcp-dashboard-oauth'

const approvedFlow = {
  flow_id: 'flow-1',
  server_name: 'reports',
  status: 'approved',
  authorization_url: 'https://idp.example/authorize?state=expected',
  error: null,
  tools: [{ name: 'list_reports', description: 'List reports' }]
}

function harness() {
  const api = vi.fn()
  const openMock = vi.fn()
  const fakeWindow = { location: { href: '' }, closed: false, opener: undefined as unknown, close: vi.fn() }

  openMock.mockReturnValue(fakeWindow)
  vi.stubGlobal('open', openMock)

  // No mcpOauth namespace at all — the web shim's actual shape.
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { api, openExternal: vi.fn() }
  })

  return { api, openMock, fakeWindow }
}

afterEach(() => {
  vi.resetAllMocks()
  vi.unstubAllGlobals()
  setApiRequestConnection(null)
  setApiRequestProfile(null)
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('completeMcpDesktopOAuth: browser fallback (no mcpOauth bridge)', () => {
  beforeEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile('origin-profile')
  })

  it('starts the flow via REST, opens a pre-created popup, polls, and returns on approval', async () => {
    const { api, openMock, fakeWindow } = harness()

    api.mockImplementation(async request => {
      if (request.method === 'POST' && request.path.endsWith('/auth')) {
        return { ...approvedFlow, status: 'authorization_required' }
      }

      if (!request.method || request.method === 'GET') {
        return approvedFlow
      }

      throw new Error(`unexpected request ${JSON.stringify(request)}`)
    })

    const result = await completeMcpDesktopOAuth({
      serverName: 'reports',
      profile: { connectionId: null, profile: 'origin-profile' },
      sleep: async () => {}
    })

    expect(result).toMatchObject({ status: 'approved', tools: approvedFlow.tools })
    // Popup opened BEFORE any await (about:blank), then navigated once the
    // authorization_url is known — never opened after an intervening await.
    expect(openMock).toHaveBeenCalledWith('about:blank', '_blank')
    expect(fakeWindow.location.href).toBe(approvedFlow.authorization_url)
    expect(fakeWindow.opener).toBeNull()

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/mcp/servers/reports/auth', method: 'POST', profile: 'origin-profile' })
    )
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/mcp/oauth/flows/flow-1', profile: 'origin-profile' })
    )
  })

  it('throws when the popup is blocked, without calling the start route', async () => {
    const { api, openMock } = harness()

    openMock.mockReturnValue(null)

    await expect(
      completeMcpDesktopOAuth({ serverName: 'reports', profile: { connectionId: null, profile: 'origin-profile' } })
    ).rejects.toThrow(/popup/i)

    expect(api).not.toHaveBeenCalled()
  })

  it('a rejecting start request propagates and closes the popup', async () => {
    const { api, fakeWindow } = harness()

    api.mockImplementation(async request => {
      if (request.method === 'POST') {
        throw new Error('backend unreachable')
      }

      throw new Error('unexpected poll before start succeeded')
    })

    await expect(
      completeMcpDesktopOAuth({ serverName: 'reports', profile: { connectionId: null, profile: 'origin-profile' } })
    ).rejects.toThrow('backend unreachable')

    expect(fakeWindow.close).toHaveBeenCalled()
  })

  it('an error status from the poll rejects with the server message', async () => {
    const { api } = harness()

    api.mockImplementation(async request => {
      if (request.method === 'POST') {
        return { ...approvedFlow, status: 'authorization_required' }
      }

      return { ...approvedFlow, status: 'error', error: 'access_denied' }
    })

    await expect(
      completeMcpDesktopOAuth({
        serverName: 'reports',
        profile: { connectionId: null, profile: 'origin-profile' },
        sleep: async () => {}
      })
    ).rejects.toThrow('access_denied')
  })

  it('cancellation rejects with McpOAuthCancelled and cleans up via DELETE', async () => {
    const { api } = harness()
    let cancelled = false

    api.mockImplementation(async request => {
      if (request.method === 'POST') {
        cancelled = true

        return { ...approvedFlow, status: 'authorization_required' }
      }

      if (request.method === 'DELETE') {
        return { ok: true }
      }

      return { ...approvedFlow, status: 'authorization_required' }
    })

    await expect(
      completeMcpDesktopOAuth({
        serverName: 'reports',
        profile: { connectionId: null, profile: 'origin-profile' },
        cancelled: () => cancelled,
        sleep: async () => {}
      })
    ).rejects.toBeInstanceOf(McpOAuthCancelled)

    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/mcp/oauth/flows/flow-1', method: 'DELETE' })
    )
  })

  it('a rejecting poll fetch propagates after maxPollFailures rather than resolving falsely', async () => {
    const { api } = harness()

    api.mockImplementation(async request => {
      if (request.method === 'POST') {
        return { ...approvedFlow, status: 'authorization_required' }
      }

      throw new Error('poll network error')
    })

    await expect(
      completeMcpDesktopOAuth({
        serverName: 'reports',
        profile: { connectionId: null, profile: 'origin-profile' },
        sleep: async () => {},
        maxPollFailures: 2
      })
    ).rejects.toThrow('poll network error')
  })
})

describe('completeMcpDesktopOAuth: bridge-present (Electron) behavior is untouched', () => {
  it('still takes the native-listener path when mcpOauth bridge exists, even off "local"', async () => {
    // Guards against an over-broad fix: when the Electron bridge IS present,
    // the REST fallback must never fire, regardless of connectionId. This
    // exact call shape (bridge present, remote connectionId) is already
    // covered end-to-end by mcp-dashboard-oauth.test.ts; this test only
    // proves the new `!bridge` branch does not accidentally widen to
    // swallow the bridge-present case too.
    const listen = vi.fn().mockResolvedValue({ id: 'listener-1', redirectUri: 'http://127.0.0.1:1/callback' })
    const wait = vi.fn().mockResolvedValue({ code: null, state: null, error: 'unused' })
    const cancel = vi.fn().mockResolvedValue(true)

    const api = vi.fn(async () => {
      throw new Error('REST fallback must not be used when the bridge is present')
    })

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { mcpOauth: { listen, wait, cancel }, openExternal: vi.fn(), api }
    })

    // No RPC mock is wired here, so mcpOAuthRpc's underlying gateway call
    // will reject — sufficient to prove we reached the bridge path (listen()
    // was called) rather than silently falling through to REST.
    await expect(
      completeMcpDesktopOAuth({ serverName: 'reports', profile: { connectionId: 'remote-gw', profile: 'p' } })
    ).rejects.toThrow()

    expect(listen).toHaveBeenCalled()
    expect(api).not.toHaveBeenCalled()
  })
})
