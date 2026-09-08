import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

// Collect the component graph before the behavioral test deadline starts.
import { GatewaySettings } from './gateway-settings'

const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()

// This test owns the machine-level GatewaySettings contract. The managed SSH
// update section mounted below the registry has its own focused coverage
// (store/managed-updates.test.ts); keep its store subscriptions out of this
// single-purpose test.
vi.mock('./managed-updates-section', () => ({ ManagedUpdatesSection: () => null }))

const localConnection = {
  cloudOrg: '',
  envOverride: false,
  mode: 'local',
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: ''
}

beforeEach(() => {
  getConnectionConfig.mockResolvedValue(localConnection)
  saveConnectionConfig.mockResolvedValue(localConnection)
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getConnectionConfig, saveConnectionConfig }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  $connection.set(null)
})

describe('GatewaySettings', () => {
  it('loads the machine-level connection config (no profile scoping)', async () => {
    render(<GatewaySettings />)
    expect(await screen.findByText('Local gateway')).toBeTruthy()
    expect(
      screen.getByText('Start a private Hermes backend on localhost. This is the default and works offline.')
    ).toBeTruthy()

    // The page manages the machine's gateway connections; it must load the
    // global config, never a per-profile override.
    await waitFor(() => expect(getConnectionConfig).toHaveBeenCalledWith(null))
    expect(getConnectionConfig).not.toHaveBeenCalledWith(expect.any(String))

    // The legacy per-profile scope switcher must not render.
    expect(screen.queryByText('Applies to')).toBeNull()
    expect(screen.queryByText('All profiles')).toBeNull()
    expect(screen.queryByText('Use default gateway')).toBeNull()

    // The host DOES own a connection registry here, so the browser-build
    // notice must stay off the page — the counterpart of the two cases below.
    expect(screen.queryByText('One backend, managed on the server')).toBeNull()
  })

  // A host with no connection registry (the browser build: the web bridge shim
  // deliberately omits getConnectionConfig) cannot register a second gateway at
  // all. The page must SAY so, naming the backend it is attached to — not
  // render an empty "unavailable" state that reads like a fixable failure.
  it('explains the single-backend build instead of the connection editor', async () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { saveConnectionConfig }
    })
    $connection.set({
      baseUrl: 'https://hermes-desktop-dev.jashworth.com',
      isFullscreen: false,
      logs: [],
      nativeOverlayWidth: 0,
      token: 'x',
      windowButtonPosition: null,
      wsUrl: 'wss://hermes-desktop-dev.jashworth.com/api/ws'
    })

    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)

    expect(await screen.findByText('One backend, managed on the server')).toBeTruthy()
    expect(
      screen.getByText(
        'This browser build is attached to a single Hermes backend on hermes-desktop-dev.jashworth.com. Registering remote, SSH, or Cloud gateways requires the Hermes Desktop app.'
      )
    ).toBeTruthy()

    const docs = screen.getByText('Connecting Desktop to many Hermes instances').closest('a')

    expect(docs?.getAttribute('href')).toBe(
      'https://hermes-agent.nousresearch.com/docs/user-guide/multi-connection-desktop'
    )

    // No connection editor: the mode cards are the thing the user came to use
    // and none of them can work here.
    expect(screen.queryByText('Local gateway')).toBeNull()
    expect(screen.queryByText('Remote gateway')).toBeNull()
    expect(screen.queryByText('Hermes Cloud')).toBeNull()
    expect(getConnectionConfig).not.toHaveBeenCalled()
  })

  // Before the descriptor lands (or if its baseUrl is unparseable) the sentence
  // drops the host clause rather than painting "on undefined".
  it('omits the host clause when no connection descriptor is available', async () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { saveConnectionConfig }
    })
    $connection.set(null)

    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)

    expect(
      await screen.findByText(
        'This browser build is attached to a single Hermes backend. Registering remote, SSH, or Cloud gateways requires the Hermes Desktop app.'
      )
    ).toBeTruthy()
  })
})
