import { afterEach, describe, expect, it } from 'vitest'

import { connectionsManagedByHost, singleBackendHostLabel } from './host-connections'

const setBridge = (value: unknown) => {
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value })
}

afterEach(() => {
  setBridge(undefined)
})

describe('connectionsManagedByHost', () => {
  // The contract is the BRIDGE MEMBER, not a build flag: Electron's main
  // process owns the connection registry and exposes getConnectionConfig; the
  // web bridge shim deliberately omits it. Anything gating on an `isWeb` flag
  // instead would also mis-answer an Electron main too old to have the member.
  it('is true only when the host exposes getConnectionConfig', () => {
    setBridge({ getConnectionConfig: async () => ({}) })
    expect(connectionsManagedByHost()).toBe(true)

    setBridge({ saveConnectionConfig: async () => ({}) })
    expect(connectionsManagedByHost()).toBe(false)

    setBridge(undefined)
    expect(connectionsManagedByHost()).toBe(false)
  })

  it('rejects a non-callable member of the same name', () => {
    setBridge({ getConnectionConfig: true })
    expect(connectionsManagedByHost()).toBe(false)
  })
})

describe('singleBackendHostLabel', () => {
  it('reduces the descriptor to a host authority, dropping path and credentials', () => {
    expect(singleBackendHostLabel({ baseUrl: 'https://desk.example.com' } as never)).toBe('desk.example.com')
    expect(singleBackendHostLabel({ baseUrl: 'http://127.0.0.1:9219/app?token=secret' } as never)).toBe('127.0.0.1:9219')
  })

  it('returns an empty label rather than a placeholder when there is nothing to name', () => {
    expect(singleBackendHostLabel(null)).toBe('')
    expect(singleBackendHostLabel(undefined)).toBe('')
    expect(singleBackendHostLabel({ baseUrl: '' } as never)).toBe('')
    expect(singleBackendHostLabel({ baseUrl: 'not a url' } as never)).toBe('')
  })
})
