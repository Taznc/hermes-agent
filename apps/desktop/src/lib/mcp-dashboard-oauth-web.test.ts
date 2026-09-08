/**
 * Covers the web-shim (browser, no Electron mcpOauth bridge) fallback path
 * of completeMcpDesktopOAuth() in mcp-dashboard-oauth.ts (t_d40923b6).
 *
 * On web, window.hermesDesktop.mcpOauth is always undefined (the shim omits
 * it — a browser tab cannot host a loopback HTTP listener) and
 * window.hermesDesktop.isWebBuild is always true (the shim's explicit
 * build-identity flag — see fork/desktop-api.d.ts). completeMcpDesktopOAuth
 * must take the browser-native REST/popup path (POST .../auth, GET
 * .../oauth/flows/{id}, DELETE on cleanup) whenever isWebBuild is true,
 * regardless of connectionId — the pre-fix code inferred "web" purely from
 * an absent bridge + a non-'local' connectionId, which is exactly the shape
 * mcp-setup.tsx always passes (connectionId resolves to null there, never
 * 'local'), but ALSO exactly the shape a bridge-absent REMOTE Electron
 * connection has — see the second describe block below for that regression.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/api/client'

import { completeMcpDesktopOAuth, McpOAuthCancelled, openMcpOAuthPopup } from './mcp-dashboard-oauth'

const approvedFlow = {
  flow_id: 'flow-1',
  server_name: 'reports',
  status: 'approved',
  authorization_url: 'https://idp.example/authorize?state=expected',
  error: null,
  tools: [{ name: 'list_reports', description: 'List reports' }]
}

const originQuery = `client_public_origin=${encodeURIComponent(window.location.origin)}`

function harness() {
  const api = vi.fn()
  const openMock = vi.fn()
  const fakeWindow = { location: { href: '' }, closed: false, opener: undefined as unknown, close: vi.fn() }

  openMock.mockReturnValue(fakeWindow)
  vi.stubGlobal('open', openMock)

  // No mcpOauth namespace at all — the web shim's actual shape. isWebBuild:
  // true is the explicit build-identity flag the shim always sets.
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { api, openExternal: vi.fn(), isWebBuild: true }
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

describe('completeMcpDesktopOAuth: browser fallback (isWebBuild, no mcpOauth bridge)', () => {
  beforeEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile('origin-profile')
  })

  it('starts the flow via REST, opens a pre-created popup, polls, and returns on approval', async () => {
    const { api, openMock, fakeWindow } = harness()

    api.mockImplementation(async request => {
      if (request.method === 'POST' && request.path.startsWith('/api/mcp/servers/reports/auth')) {
        expect(request.path).toContain(originQuery)

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
      expect.objectContaining({
        path: expect.stringContaining('/api/mcp/servers/reports/auth?client_public_origin='),
        method: 'POST',
        profile: 'origin-profile'
      })
    )
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({ path: '/api/mcp/oauth/flows/flow-1', profile: 'origin-profile' })
    )
  })

  it('reuses a caller-supplied popupWindow (pre-opened at the real click boundary) instead of opening its own', async () => {
    const { api, openMock } = harness()
    const preOpened = { location: { href: '' }, closed: false, opener: 'preexisting', close: vi.fn() }

    api.mockImplementation(async request => {
      if (request.method === 'POST') {
        return { ...approvedFlow, status: 'authorization_required' }
      }

      return approvedFlow
    })

    const result = await completeMcpDesktopOAuth({
      serverName: 'reports',
      profile: { connectionId: null, profile: 'origin-profile' },
      sleep: async () => {},
      popupWindow: preOpened as unknown as Window
    })

    expect(result).toMatchObject({ status: 'approved' })
    // No new window.open call — the caller's popup was reused as-is (opener
    // is only nulled by openMcpOAuthPopup itself, not by the completer).
    expect(openMock).not.toHaveBeenCalled()
    expect(preOpened.location.href).toBe(approvedFlow.authorization_url)
    expect(preOpened.close).toHaveBeenCalled()
  })

  it('a null popupWindow (caller already tried and was blocked) throws immediately with no network call', async () => {
    const { api, openMock } = harness()

    await expect(
      completeMcpDesktopOAuth({
        serverName: 'reports',
        profile: { connectionId: null, profile: 'origin-profile' },
        popupWindow: null
      })
    ).rejects.toThrow(/popup/i)

    expect(openMock).not.toHaveBeenCalled()
    expect(api).not.toHaveBeenCalled()
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

describe('openMcpOAuthPopup', () => {
  it('opens about:blank and nulls opener', () => {
    const fakeWindow = { opener: 'preexisting' as unknown }
    const openMock = vi.fn().mockReturnValue(fakeWindow)
    vi.stubGlobal('open', openMock)

    const result = openMcpOAuthPopup()

    expect(openMock).toHaveBeenCalledWith('about:blank', '_blank')
    expect(result).toBe(fakeWindow)
    expect(fakeWindow.opener).toBeNull()
    vi.unstubAllGlobals()
  })

  it('returns null when the popup is blocked, without throwing', () => {
    vi.stubGlobal(
      'open',
      vi.fn(() => null)
    )

    expect(openMcpOAuthPopup()).toBeNull()
    vi.unstubAllGlobals()
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
    Reflect.deleteProperty(window, 'hermesDesktop')
  })
})

describe('completeMcpDesktopOAuth: bridge-absent, Electron (NOT web) compat paths are unchanged', () => {
  // Both of these simulate an OLD Electron preload build that predates the
  // mcpOauth bridge member — isWebBuild is undefined (real Electron preload
  // never sets it), matching every actual Electron build ever shipped,
  // including this one. Neither must take the REST/popup fallback: a
  // browser tab's window.open()/navigate trick cannot complete inside
  // Electron's renderer the way it can in an actual browser tab, and these
  // are exactly the two legacy shapes the pre-fix code already handled
  // correctly by inferring "not web" from connectionId === 'local' alone —
  // this proves the isWebBuild-based rewrite preserves both outcomes.
  const bridgeAbsentElectron = () => {
    const api = vi.fn(async () => {
      throw new Error('REST fallback must not be used on a bridge-absent Electron build')
    })

    const openMock = vi.fn(() => {
      throw new Error('window.open must not be called on a bridge-absent Electron build')
    })

    vi.stubGlobal('open', openMock)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      // No mcpOauth, no isWebBuild — the exact shape of an old Electron
      // preload build. openExternal exists because every Electron preload
      // (old or new) defines it.
      value: { api, openExternal: vi.fn() }
    })

    return { api, openMock }
  }

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('bridge-absent + explicit connectionId "local" throws the compat-upgrade message (pre-existing behavior)', async () => {
    const { api, openMock } = bridgeAbsentElectron()

    await expect(
      completeMcpDesktopOAuth({ serverName: 'reports', profile: { connectionId: 'local', profile: 'p' } })
    ).rejects.toThrow('Update Hermes Desktop to support MCP OAuth callbacks.')

    expect(api).not.toHaveBeenCalled()
    expect(openMock).not.toHaveBeenCalled()
  })

  it('bridge-absent + a REMOTE connectionId also throws the compat-upgrade message, not a browser popup fallback', async () => {
    // This is the exact regression: the pre-fix rewrite inferred "web" from
    // bridge-absent + non-'local', which misclassifies THIS shape (an old
    // Electron build talking to a remote gateway) as the web build and sent
    // it down the REST/popup path, which cannot work inside Electron.
    const { api, openMock } = bridgeAbsentElectron()

    await expect(
      completeMcpDesktopOAuth({ serverName: 'reports', profile: { connectionId: 'remote-gateway', profile: 'p' } })
    ).rejects.toThrow('Update Hermes Desktop to support MCP OAuth callbacks.')

    expect(api).not.toHaveBeenCalled()
    expect(openMock).not.toHaveBeenCalled()
  })

  it('bridge-absent + no connectionId at all also throws the compat-upgrade message', async () => {
    const { api, openMock } = bridgeAbsentElectron()

    await expect(
      completeMcpDesktopOAuth({ serverName: 'reports', profile: { profile: 'p' } })
    ).rejects.toThrow('Update Hermes Desktop to support MCP OAuth callbacks.')

    expect(api).not.toHaveBeenCalled()
    expect(openMock).not.toHaveBeenCalled()
  })
})
