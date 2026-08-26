import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

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
function stubDesktop(config: Record<string, unknown> | null) {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      getRecentLogs: async () => ({ lines: [] }),
      getConnectionConfig: async () => config,
      repairBootstrap: async () => ({ ok: true }),
      resetBootstrap: async () => ({ ok: true })
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
})
