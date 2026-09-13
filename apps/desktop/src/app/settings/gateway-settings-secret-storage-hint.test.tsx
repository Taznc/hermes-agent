/**
 * Renderer behavior tests for the "stored without OS keychain encryption"
 * hint in Settings → Gateway (review round 2, defect #3).
 *
 * The bug this file pins: `gateway-settings.tsx` used to clear the persisted
 * dismissal on EVERY mount/hydration that observed an unencrypted state, so a
 * dismissed hint reappeared on every plain app restart even though nothing
 * had changed. It also never refreshed `secretStorageState` after a
 * successful `setSecretStorageEncryption()` call, so enabling encryption left
 * a stale warning visible and disabling left it stale-hidden until an
 * unrelated config refresh.
 *
 * Fix under test: dismissal is now paired with a fingerprint of the exact
 * unencrypted shape (`policyOn:available`) that was dismissed, and both the
 * initial `getSecretStorageEncryption()` load and every `setSecretStorageEncryption()`
 * response now carry a fresh `secretStorageState` that the renderer applies
 * immediately.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()
const getSecretStorageEncryption = vi.fn()
const setSecretStorageEncryption = vi.fn()

const DISMISS_KEY = 'hermes-secret-storage-hint-dismissed-v1'

const baseConnection = {
  cloudOrg: '',
  envOverride: false,
  mode: 'local',
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: ''
}

function unencrypted(available: boolean, policyOn: boolean) {
  return { ...baseConnection, secretStorageState: { available, policyOn } }
}

function installDesktop() {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getConnectionConfig, saveConnectionConfig, getSecretStorageEncryption, setSecretStorageEncryption }
  })
}

beforeEach(() => {
  window.localStorage.clear()
  getSecretStorageEncryption.mockResolvedValue({ on: false })
  setSecretStorageEncryption.mockResolvedValue({ on: false })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  window.localStorage.clear()
})

describe('GatewaySettings secret storage hint', () => {
  it('shows the hint for an unencrypted policy-off state and dismissal persists across an identical remount', async () => {
    getConnectionConfig.mockResolvedValue(unencrypted(false, false))
    saveConnectionConfig.mockResolvedValue(unencrypted(false, false))
    installDesktop()

    const { GatewaySettings } = await import('./gateway-settings')

    const first = render(<GatewaySettings />)

    expect(await screen.findByText('Stored without OS keychain encryption')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Dismiss'))

    await waitFor(() => expect(screen.queryByText('Stored without OS keychain encryption')).toBeNull())
    expect(window.localStorage.getItem(DISMISS_KEY)).toBeTruthy()

    first.unmount()

    // Remount = a plain app restart. Observed state is IDENTICAL
    // (policyOn: false, available: false) to what was dismissed — the fixed
    // behavior must NOT resurface the hint just because of the remount.
    render(<GatewaySettings />)

    await waitFor(() => expect(getConnectionConfig).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Stored without OS keychain encryption')).toBeNull()
  })

  it('resurfaces the hint after a dismissal when the observed unencrypted shape actually changes', async () => {
    // First render: policy off, dismissed.
    getConnectionConfig.mockResolvedValueOnce(unencrypted(false, false))
    installDesktop()

    const { GatewaySettings } = await import('./gateway-settings')

    const first = render(<GatewaySettings />)

    expect(await screen.findByText('Stored without OS keychain encryption')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Dismiss'))
    await waitFor(() => expect(screen.queryByText('Stored without OS keychain encryption')).toBeNull())

    first.unmount()

    // Next mount observes a DIFFERENT unencrypted shape: the user turned
    // encryption ON elsewhere (or it was toggled and failed) — policyOn now
    // true but still unavailable. This is new, actionable information and
    // must resurface even though a hint was dismissed for the OLD shape,
    // because the persisted dismissal is keyed to that old fingerprint.
    getConnectionConfig.mockResolvedValueOnce(unencrypted(false, true))
    render(<GatewaySettings />)

    await waitFor(() => expect(screen.getByText('Stored without OS keychain encryption')).toBeTruthy())
  })

  it('refreshes secretStorageState immediately after a successful enable, hiding the hint without a config refresh', async () => {
    getConnectionConfig.mockResolvedValue(unencrypted(false, false))
    getSecretStorageEncryption.mockResolvedValue({ on: false, secretStorageState: { available: false, policyOn: false } })
    setSecretStorageEncryption.mockResolvedValue({ on: true, secretStorageState: { available: true, policyOn: true } })
    installDesktop()

    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)

    expect(await screen.findByText('Stored without OS keychain encryption')).toBeTruthy()

    fireEvent.click(screen.getByText('Enable encryption'))

    // The hint must disappear from the SAME setSecretStorageEncryption response
    // — no second getConnectionConfig call should be required.
    await waitFor(() => expect(screen.queryByText('Stored without OS keychain encryption')).toBeNull())
    expect(getConnectionConfig).toHaveBeenCalledTimes(1)
  })

  it('refreshes secretStorageState immediately after a disable, so the hint reflects the new plaintext state right away', async () => {
    getConnectionConfig.mockResolvedValue(unencrypted(true, true))
    getSecretStorageEncryption.mockResolvedValue({ on: true, secretStorageState: { available: true, policyOn: true } })
    setSecretStorageEncryption.mockResolvedValue({ on: false, secretStorageState: { available: false, policyOn: false } })
    installDesktop()

    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)

    // Fully encrypted state: no hint initially.
    await waitFor(() => expect(getConnectionConfig).toHaveBeenCalled())
    expect(screen.queryByText('Stored without OS keychain encryption')).toBeNull()

    // Toggle the keychain-encryption switch off via its accessible label
    // (ToggleRow renders a Switch with aria-label={label}).
    const toggle = await screen.findByRole('switch', { name: 'Encrypt saved secrets with the OS keychain' })

    fireEvent.click(toggle)

    await waitFor(() => expect(setSecretStorageEncryption).toHaveBeenCalledWith(false))
    await waitFor(() => expect(screen.getByText('Stored without OS keychain encryption')).toBeTruthy())
  })
})
