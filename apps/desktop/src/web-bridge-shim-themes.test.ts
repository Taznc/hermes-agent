/**
 * Covers web-bridge-shim.ts's themes.searchMarketplace() member and the
 * async $localModelsEnabled correction (t_d40923b6).
 *
 * web-bridge-shim.ts installs `window.hermesDesktop` as a side effect of
 * being imported and reads localStorage/the URL at module-eval time, so each
 * test gets a fresh module instance via vi.resetModules() + dynamic import,
 * matching how index-web.html loads it once before src/main.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

interface MarketplaceSearchItem {
  extensionId: string
  displayName: string
  publisher: string
  description: string
  installs: number
}

interface HermesDesktopLike {
  themes?: {
    searchMarketplace: (query: string) => Promise<MarketplaceSearchItem[]>
  }
  localModelsEnabled?: boolean
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

const GALLERY_URL = 'https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery'

const GALLERY_RESPONSE = {
  results: [
    {
      extensions: [
        {
          extensionName: 'dracula-theme',
          displayName: 'Dracula Official',
          publisher: { publisherName: 'dracula-theme', displayName: 'Dracula Theme' },
          shortDescription: 'Dark theme for many editors',
          tags: ['theme'],
          statistics: [{ statisticName: 'install', value: 12345 }]
        },
        {
          // Icon themes must be filtered out — not a color theme.
          extensionName: 'material-icon-theme',
          displayName: 'Material Icon Theme',
          publisher: { publisherName: 'pkief', displayName: 'Philipp Kief' },
          shortDescription: 'Material Design icons',
          tags: ['icon-theme'],
          statistics: [{ statisticName: 'install', value: 999 }]
        }
      ]
    }
  ]
}

describe('web-bridge-shim themes.searchMarketplace', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('is defined and returns Marketplace results with icon themes filtered out', async () => {
    fetchMock.mockImplementation((url: unknown, init?: { body?: string }) => {
      expect(String(url)).toBe(GALLERY_URL)
      expect(init?.body).toBeDefined()

      return jsonResponse(GALLERY_RESPONSE)
    })

    const { themes } = await loadShim()

    expect(typeof themes?.searchMarketplace).toBe('function')

    const results = await themes!.searchMarketplace('dracula')

    expect(results).toEqual([
      {
        extensionId: 'dracula-theme.dracula-theme',
        displayName: 'Dracula Official',
        publisher: 'Dracula Theme',
        description: 'Dark theme for many editors',
        installs: 12345
      }
    ])
  })

  it('an empty query still searches (most-installed themes)', async () => {
    fetchMock.mockImplementation(() => jsonResponse({ results: [{ extensions: [] }] }))

    const { themes } = await loadShim()
    const results = await themes!.searchMarketplace('')

    expect(results).toEqual([])
    expect(fetchMock).toHaveBeenCalled()
  })

  it('a rejecting fetch propagates as a rejection, not a false-empty array', async () => {
    fetchMock.mockImplementation(() => Promise.reject(new Error('network unreachable')))

    const { themes } = await loadShim()

    await expect(themes!.searchMarketplace('anything')).rejects.toThrow('network unreachable')
  })

  it('a non-2xx gallery response propagates as a rejection', async () => {
    fetchMock.mockImplementation(() => jsonResponse({}, { status: 500 }))

    const { themes } = await loadShim()

    await expect(themes!.searchMarketplace('anything')).rejects.toThrow()
  })
})

describe('web-bridge-shim localModelsEnabled', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('starts false on the bridge and corrects the store once /api/local-models/status reports enabled', async () => {
    fetchMock.mockImplementation((url: URL) => {
      expect(String(url)).toContain('/api/local-models/status')

      return jsonResponse({ enabled: true })
    })

    const desktop = await loadShim()

    // Synchronous field, matching Electron's launch-flag contract shape —
    // the shim has no launch flag, so it starts at the safe default.
    expect(desktop.localModelsEnabled).toBe(false)

    // The store itself is what the UI actually reads (@/store/local-models-flag
    // captures window.hermesDesktop?.localModelsEnabled ONCE at its own
    // import time — mutating the bridge field after the fact does nothing).
    // The async correction may already have resolved by the time this
    // dynamic import settles (mocked fetch resolves within a microtask), so
    // only the EVENTUAL value is asserted here.
    const { $localModelsEnabled } = await import('@/store/local-models-flag')

    await vi.waitFor(() => expect($localModelsEnabled.get()).toBe(true))
  })

  it('a rejecting fetch leaves the store at the safe default (false), never throws unhandled', async () => {
    fetchMock.mockImplementation(() => Promise.reject(new Error('offline')))

    await loadShim()
    const { $localModelsEnabled } = await import('@/store/local-models-flag')

    // Give the fire-and-forget correction a turn to (not) run.
    await new Promise(resolve => setTimeout(resolve, 0))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect($localModelsEnabled.get()).toBe(false)
  })

  it('a false status response leaves the store at the safe default', async () => {
    fetchMock.mockImplementation(() => jsonResponse({ enabled: false }))

    await loadShim()
    const { $localModelsEnabled } = await import('@/store/local-models-flag')

    await new Promise(resolve => setTimeout(resolve, 0))
    await new Promise(resolve => setTimeout(resolve, 0))

    expect($localModelsEnabled.get()).toBe(false)
  })
})
