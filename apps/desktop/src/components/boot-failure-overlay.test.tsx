import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $desktopOnboarding } from '@/store/onboarding'

import { BootFailureOverlay } from './boot-failure-overlay'

// Remote-backend users hit a hard boot failure that isn't OAuth reauth (token
// auth, wrong URL, unreachable host). The recovery screen must let them fix the
// remote connection in place — the "Connection settings" action swaps the card
// to an in-line connect form — instead of stranding them (the old bug forced a
// hand-edit of connection.json).

function failBoot() {
  $desktopBoot.set({
    error: 'Could not connect to Hermes gateway',
    fakeMode: false,
    message: 'boot failed',
    phase: 'renderer.error',
    progress: 40,
    running: false,
    timestamp: Date.now(),
    visible: true
  })
}

// Simulates the real Electron preload bridge: repairBootstrap/getConnectionConfig
// are always defined there (only the browser web-spike shim omits them — see
// web-bridge-shim.ts), so every scenario here gets both unless a test is
// specifically exercising the web-spike's missing-capability case.
function stubDesktop(config: Record<string, unknown> | null, overrides: Record<string, unknown> = {}) {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      getRecentLogs: async () => ({ lines: [] }),
      getConnectionConfig: async () => config,
      repairBootstrap: async () => ({ ok: true }),
      resetBootstrap: async () => ({ ok: true }),
      ...overrides
    }
  })

  return () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
}

const remoteToken = {
  envOverride: false,
  mode: 'remote',
  profile: null,
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: true,
  remoteUrl: 'http://100.116.104.53:9191',
  cloudOrg: ''
}

beforeEach(() => {
  $desktopOnboarding.set({
    configured: true,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
  failBoot()
})

afterEach(cleanup)

describe('BootFailureOverlay', () => {
  it('swaps to the in-place gateway settings view (no route nav) and back', async () => {
    const restore = stubDesktop(null)

    try {
      render(<BootFailureOverlay />)

      fireEvent.click(screen.getByRole('button', { name: /gateway settings/i }))
      // Recovery actions give way to the embedded panel (behind a Back control).
      expect(await screen.findByRole('button', { name: /back/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()

      fireEvent.click(screen.getByRole('button', { name: /back/i }))
      expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /back/i })).toBeNull()
    } finally {
      restore()
    }
  })

  it('drops local-only Repair and Use-local-gateway on a local failure', () => {
    const restore = stubDesktop(null)

    try {
      render(<BootFailureOverlay />)
      // No remote connection config → treated as a local failure. Electron
      // (unlike the browser web-spike) always exposes repairBootstrap, so
      // Repair is offered.
      expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /repair/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /use local gateway/i })).toBeNull()
    } finally {
      restore()
    }
  })

  it('hides Repair and Gateway settings when the bridge lacks those capabilities (web-spike)', () => {
    // The browser-served web build's web-bridge-shim.ts deliberately omits
    // repairBootstrap/getConnectionConfig — the boot-failure overlay must not
    // offer buttons that silently no-op or route into an "unavailable" panel.
    const original = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getRecentLogs: async () => ({ lines: [] }) }
    })

    try {
      render(<BootFailureOverlay />)
      expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /repair/i })).toBeNull()
      expect(screen.queryByRole('button', { name: /gateway settings/i })).toBeNull()
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('leads with Gateway settings and drops Repair for a remote (token) failure', async () => {
    const restore = stubDesktop(remoteToken)

    try {
      render(<BootFailureOverlay />)
      await waitFor(() => expect(screen.queryByRole('button', { name: /repair/i })).toBeNull())
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
    } finally {
      restore()
    }
  })

  it('opens gateway settings with a partial persisted remote config', async () => {
    const restore = stubDesktop({ mode: 'remote', remoteAuthMode: undefined, remoteUrl: undefined })

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(screen.getByRole('button', { name: /gateway settings/i }))

      expect(await screen.findByRole('button', { name: /back/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    } finally {
      restore()
    }
  })

  it('clears and signs in only the failed gateway once', async () => {
    const gatewayUrl = 'http://100.116.104.53:9191'
    const logout = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const login = vi.fn().mockResolvedValue({ ok: true, connected: false })

    const restore = stubDesktop(
      {
        ...remoteToken,
        remoteAuthMode: 'oauth',
        remoteOauthConnected: false,
        remoteTokenSet: false,
        remoteUrl: gatewayUrl
      },
      {
        oauthLoginConnectionConfig: login,
        oauthLogoutConnectionConfig: logout,
        probeConnectionConfig: vi.fn().mockResolvedValue({ providers: [{ id: 'basic', type: 'password' }] })
      }
    )

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign out & sign in/i }))

      await waitFor(() => expect(login).toHaveBeenCalledWith(gatewayUrl))
      expect(logout).toHaveBeenCalledTimes(1)
      expect(logout).toHaveBeenCalledWith(gatewayUrl)
      expect(login).toHaveBeenCalledTimes(1)
    } finally {
      restore()
    }
  })

  it('recovers a cloud connection through the portal cascade instead of native OAuth', async () => {
    const gatewayUrl = 'https://agent-1.agents.nousresearch.com'
    const logout = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const nativeLogin = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const cloudStatus = vi.fn().mockResolvedValue({ portalBaseUrl: 'https://portal.nousresearch.com', signedIn: false })

    const cloudLogin = vi.fn().mockResolvedValue({
      ok: true,
      portalBaseUrl: 'https://portal.nousresearch.com',
      signedIn: true
    })

    const cloudAgentSignIn = vi.fn().mockResolvedValue({ baseUrl: gatewayUrl, connected: false })

    const restore = stubDesktop(
      {
        ...remoteToken,
        mode: 'cloud',
        remoteAuthMode: 'oauth',
        remoteOauthConnected: false,
        remoteTokenSet: false,
        remoteUrl: gatewayUrl
      },
      {
        cloud: { status: cloudStatus, login: cloudLogin, agentSignIn: cloudAgentSignIn },
        oauthLoginConnectionConfig: nativeLogin,
        oauthLogoutConnectionConfig: logout,
        probeConnectionConfig: vi.fn().mockResolvedValue({ providers: [{ id: 'nous', type: 'oauth' }] })
      }
    )

    try {
      render(<BootFailureOverlay />)
      fireEvent.click(await screen.findByRole('button', { name: /sign in/i }))

      await waitFor(() => expect(cloudAgentSignIn).toHaveBeenCalledWith(gatewayUrl))
      expect(logout).toHaveBeenCalledWith(gatewayUrl)
      expect(cloudStatus).toHaveBeenCalledTimes(1)
      expect(cloudLogin).toHaveBeenCalledTimes(1)
      expect(nativeLogin).not.toHaveBeenCalled()
    } finally {
      restore()
    }
  })

  it('shows the Nous Cloud down recovery when the backend flags isCloudBackendDown', async () => {
    const restore = stubDesktop(remoteToken)
    $desktopBoot.set({
      error: 'Nous Cloud agent ares-3009.agents.nousresearch.com is down (HTTP 503: server-side fault).',
      fakeMode: false,
      isCloudBackendDown: true,
      message: 'boot failed',
      phase: 'renderer.error',
      progress: 40,
      running: false,
      statusCode: 503,
      timestamp: Date.now(),
      visible: true
    })

    try {
      render(<BootFailureOverlay />)
      // Cloud-specific title + actionable recovery instead of the generic
      // remote-failure copy.
      expect(await screen.findByText(/Nous Cloud agent is down/i)).toBeTruthy()
      // Portal and Discord are dedicated action buttons (localized labels
      // can't drift the URLs, which live in code).
      expect(screen.getByRole('button', { name: /check portal status/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /get help on discord/i })).toBeTruthy()
      // Cloud-down is a remote failure: local-only Repair is dropped; the
      // actionable paths are Gateway settings + Use local gateway.
      expect(screen.queryByRole('button', { name: /repair/i })).toBeNull()
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
      // The electron-built error message (portal / local mode / Discord) is
      // still surfaced in the error box.
      expect(screen.getByText(/ares-3009\.agents\.nousresearch\.com/i)).toBeTruthy()
    } finally {
      restore()
    }
  })

  // The WS-auth-rejected boot failure (#t_360b3fcb): use-gateway-boot.ts
  // detects that the gateway answered /api/health while the WS credential
  // was refused (or was never present) and tags the boot error with the
  // wsAuthRejectedMessage() marker instead of the generic "could not
  // connect" message. The overlay must show the honest "gateway is fine,
  // sign-in required" copy, not the misleading "gateway didn't come up"
  // title/description — this is the whole bug the card reports.
  it('shows the honest auth-rejected copy (not "gateway didn\'t come up") for a WS credential failure', async () => {
    const restore = stubDesktop(null)
    $desktopBoot.set({
      error:
        'Hermes gateway is reachable, but the live connection was refused (missing or invalid access credential). No access token was found for this session (missing ?token= link or stored credential).',
      fakeMode: false,
      message: 'boot failed',
      phase: 'renderer.error',
      progress: 40,
      running: false,
      timestamp: Date.now(),
      visible: true
    })

    try {
      render(<BootFailureOverlay />)
      // Must NOT show the misleading "gateway didn't come up" copy.
      expect(screen.queryByText(/couldn't start/i)).toBeNull()
      expect(screen.queryByText(/background gateway didn't come up/i)).toBeNull()
      // Must show the honest sign-in-required copy naming the real cause.
      expect(await screen.findByText(/sign-in required/i)).toBeTruthy()
      expect(screen.getByText(/access credential was rejected/i)).toBeTruthy()
      // Retry must not be offered: it would silently redial the identical
      // tokenless URL into the same rejection forever (scope item 4).
      expect(screen.queryByRole('button', { name: /^retry$/i })).toBeNull()
    } finally {
      restore()
    }
  })

  it('a genuinely dead gateway (no health-probe classification applied) keeps the original "couldn\'t start" copy', () => {
    // Regression guard: an ordinary connect failure (the health probe found
    // nothing / never ran, e.g. main.ts's IPC-bridge-unavailable path) must
    // still show the original overlay, not the new auth-specific one.
    const restore = stubDesktop(null)

    try {
      render(<BootFailureOverlay />)
      expect(screen.getByText(/couldn't start/i)).toBeTruthy()
      expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    } finally {
      restore()
    }
  })
})
