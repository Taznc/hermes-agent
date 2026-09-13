import type * as HermesSdk from '@hermes/plugin-sdk'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  completeMcpOAuth: vi.fn(),
  request: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()

  return {
    ...sdk,
    host: {
      ...sdk.host,
      completeMcpOAuth: mocks.completeMcpOAuth,
      request: mocks.request,
      state: {
        ...sdk.host.state,
        connectionId: { get: () => 'local' },
        profile: { get: () => 'default' }
      }
    }
  }
})

const { McpSetupButton } = await import('./mcp-setup')

function deferred<T>() {
  let reject!: (error: Error) => void
  let resolve!: (value: T) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

function popup() {
  return {
    close: vi.fn(),
    closed: false,
    location: { href: '' },
    opener: undefined as unknown
  }
}

beforeEach(() => {
  mocks.request.mockResolvedValue({})
  mocks.completeMcpOAuth.mockResolvedValue({ status: 'approved' })
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { isWebBuild: true }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

describe('McpSetupButton browser OAuth popup ownership', () => {
  it('opens synchronously before delayed profile resolution and reuses that popup for OAuth', async () => {
    const profile = deferred<null | string>()
    const authWindow = popup()
    const open = vi.fn(() => authWindow)
    const ensureProfile = vi.fn(() => profile.promise)
    vi.stubGlobal('open', open)

    render(
      <McpSetupButton
        ensureProfile={ensureProfile}
        entry={{ auth: 'oauth', fromCatalog: true, installed: false, name: 'reports' }}
        profile={null}
      />
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in…' }))

    expect(open).toHaveBeenCalledWith('about:blank', '_blank')
    expect(ensureProfile).toHaveBeenCalledTimes(1)
    expect(mocks.completeMcpOAuth).not.toHaveBeenCalled()

    await act(async () => profile.resolve('writer'))

    await waitFor(() =>
      expect(mocks.completeMcpOAuth).toHaveBeenCalledWith(
        expect.objectContaining({
          catalogPreset: 'reports',
          popupWindow: authWindow,
          profile: { connectionId: 'local', profile: 'writer' },
          serverName: 'reports'
        })
      )
    )
  })

  it('closes the synchronously opened popup when profile prework rejects', async () => {
    const profile = deferred<null | string>()
    const authWindow = popup()
    vi.stubGlobal('open', vi.fn(() => authWindow))

    render(
      <McpSetupButton
        ensureProfile={() => profile.promise}
        entry={{ auth: 'oauth', fromCatalog: true, installed: false, name: 'reports' }}
        profile={null}
      />
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in…' }))

    await act(async () => profile.reject(new Error('profile creation failed')))

    await waitFor(() => expect(authWindow.close).toHaveBeenCalledTimes(1))
    expect(mocks.completeMcpOAuth).not.toHaveBeenCalled()
    expect(await screen.findByText(/profile creation failed/)).toBeTruthy()
  })
})
