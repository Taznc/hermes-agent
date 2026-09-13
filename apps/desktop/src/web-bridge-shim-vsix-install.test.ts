/**
 * Covers web-bridge-shim.ts's themes.fetchMarketplace() member — the browser-
 * native theme INSTALL path (t_cef12883), companion to the search member
 * covered in web-bridge-shim-themes.test.ts.
 *
 * Both hosts are third-party and CORS-open (verified live): the gallery query
 * API resolves the `.vsix` URL, and `<publisher>.gallerycdn.vsassets.io`
 * serves the archive with `Access-Control-Allow-Origin: *`. No backend route
 * is involved, so every assertion here is about the two outbound fetches and
 * the archive parsing between them.
 *
 * web-bridge-shim.ts installs `window.hermesDesktop` as a side effect of being
 * imported, so each test gets a fresh module via vi.resetModules().
 */
import zlib from 'node:zlib'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopMarketplaceThemeResult } from './global'

interface HermesDesktopLike {
  themes?: {
    fetchMarketplace?: (id: string) => Promise<DesktopMarketplaceThemeResult>
    searchMarketplace: (query: string) => Promise<unknown[]>
  }
}

async function loadShim(): Promise<HermesDesktopLike> {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (window as unknown as { hermesDesktop: HermesDesktopLike }).hermesDesktop
}

const GALLERY_URL = 'https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery'

const VSIX_URL =
  'https://dracula-theme.gallerycdn.vsassets.io/extensions/dracula-theme/theme-dracula/2.25.1/1/Microsoft.VisualStudio.Services.VSIXPackage'

const VSIX_ASSET_TYPE = 'Microsoft.VisualStudio.Services.VSIXPackage'

const DARK_THEME = JSON.stringify({ colors: { 'editor.background': '#282a36' }, name: 'Dracula', type: 'dark' })

function galleryResponse(overrides: Record<string, unknown> = {}) {
  return {
    results: [
      {
        extensions: [
          {
            displayName: 'Dracula Theme Official',
            extensionName: 'theme-dracula',
            publisher: { displayName: 'Dracula Theme', publisherName: 'dracula-theme' },
            versions: [{ files: [{ assetType: VSIX_ASSET_TYPE, source: VSIX_URL }], version: '2.25.1' }],
            ...overrides
          }
        ]
      }
    ]
  }
}

/** Assemble a real (tiny) `.vsix`: deflated manifest + deflated theme JSON. */
function buildVsix(entries: Array<{ name: string; text: string }>): Uint8Array {
  const encoder = new TextEncoder()
  const locals: Uint8Array[] = []
  const centrals: Uint8Array[] = []
  let offset = 0

  for (const entry of entries) {
    const name = encoder.encode(entry.name)
    const raw = encoder.encode(entry.text)
    const payload = new Uint8Array(zlib.deflateRawSync(Buffer.from(raw)))

    const local = new Uint8Array(30 + name.length + payload.length)
    const localView = new DataView(local.buffer)
    localView.setUint32(0, 0x04034b50, true)
    localView.setUint16(8, 8, true)
    localView.setUint32(18, payload.length, true)
    localView.setUint32(22, raw.length, true)
    localView.setUint16(26, name.length, true)
    local.set(name, 30)
    local.set(payload, 30 + name.length)
    locals.push(local)

    const central = new Uint8Array(46 + name.length)
    const centralView = new DataView(central.buffer)
    centralView.setUint32(0, 0x02014b50, true)
    centralView.setUint16(10, 8, true)
    centralView.setUint32(20, payload.length, true)
    centralView.setUint32(24, raw.length, true)
    centralView.setUint16(28, name.length, true)
    centralView.setUint32(42, offset, true)
    central.set(name, 46)
    centrals.push(central)

    offset += local.length
  }

  const centralSize = centrals.reduce((sum, part) => sum + part.length, 0)
  const eocd = new Uint8Array(22)
  const eocdView = new DataView(eocd.buffer)
  eocdView.setUint32(0, 0x06054b50, true)
  eocdView.setUint16(8, entries.length, true)
  eocdView.setUint16(10, entries.length, true)
  eocdView.setUint32(12, centralSize, true)
  eocdView.setUint32(16, offset, true)

  const parts = [...locals, ...centrals, eocd]
  const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0))
  let cursor = 0

  for (const part of parts) {
    out.set(part, cursor)
    cursor += part.length
  }

  return out
}

function draculaVsix(): Uint8Array {
  return buildVsix([
    {
      name: 'extension/package.json',
      text: JSON.stringify({
        contributes: { themes: [{ label: 'Dracula', path: './themes/dracula.json', uiTheme: 'vs-dark' }] },
        displayName: 'Dracula Theme',
        name: 'theme-dracula'
      })
    },
    { name: 'extension/themes/dracula.json', text: DARK_THEME }
  ])
}

function jsonResponse(body: unknown, init?: { status?: number }) {
  const status = init?.status ?? 200

  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body)
  } as Response)
}

/** A cross-origin CDN response: exposed Content-Length + a streaming body. */
function binaryResponse(bytes: Uint8Array, options: { contentLength?: string | null; status?: number } = {}) {
  const status = options.status ?? 200
  const declared = options.contentLength === undefined ? String(bytes.length) : options.contentLength

  return Promise.resolve({
    arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
    body: {
      getReader() {
        let sent = false

        return {
          cancel: async () => undefined,
          read: async () => (sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: bytes }))
        }
      }
    },
    headers: { get: (name: string) => (name.toLowerCase() === 'content-length' ? declared : null) },
    ok: status >= 200 && status < 300,
    status
  } as unknown as Response)
}

/** The shim also probes /api/local-models/status at load; ignore that call. */
function marketplaceCalls(mock: ReturnType<typeof vi.fn>): string[] {
  return mock.mock.calls.map(call => String(call[0])).filter(url => !url.includes('/api/'))
}

describe('web-bridge-shim themes.fetchMarketplace', () => {
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('is defined and returns the extension id, display name, and extracted theme JSON', async () => {
    const vsix = draculaVsix()

    fetchMock.mockImplementation((url: unknown) =>
      String(url) === GALLERY_URL ? jsonResponse(galleryResponse()) : binaryResponse(vsix)
    )

    const { themes } = await loadShim()

    expect(typeof themes?.fetchMarketplace).toBe('function')

    const result = await themes!.fetchMarketplace!('dracula-theme.theme-dracula')

    expect(result.extensionId).toBe('dracula-theme.theme-dracula')
    expect(result.displayName).toBe('Dracula Theme Official')
    expect(result.themes).toHaveLength(1)
    expect(result.themes[0].label).toBe('Dracula')
    expect(result.themes[0].uiTheme).toBe('vs-dark')
    expect(JSON.parse(result.themes[0].contents).colors['editor.background']).toBe('#282a36')

    // The gallery query resolves the URL; the CDN download is a plain GET.
    expect(marketplaceCalls(fetchMock)).toEqual([GALLERY_URL, VSIX_URL])
  })

  it('rejects a malformed id before making any Marketplace network call', async () => {
    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('not-an-extension-id')).rejects.toThrow(/publisher\.extension/)
    expect(marketplaceCalls(fetchMock)).toEqual([])
  })

  it('rejects when the gallery knows no such extension', async () => {
    fetchMock.mockImplementation(() => jsonResponse({ results: [{ extensions: [] }] }))

    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('nobody.nothing')).rejects.toThrow(/not found/i)
  })

  it('rejects when the resolved version publishes no .vsix asset', async () => {
    fetchMock.mockImplementation(() => jsonResponse(galleryResponse({ versions: [{ files: [], version: '1.0.0' }] })))

    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('dracula-theme.theme-dracula')).rejects.toThrow(/downloadable package/i)
  })

  it('refuses an oversized download from its Content-Length before reading the body', async () => {
    const bodyRead = vi.fn()

    fetchMock.mockImplementation((url: unknown) => {
      if (String(url) === GALLERY_URL) {
        return jsonResponse(galleryResponse())
      }

      return Promise.resolve({
        arrayBuffer: bodyRead,
        body: { getReader: bodyRead },
        headers: { get: () => String(64 * 1024 * 1024) },
        ok: true,
        status: 200
      } as unknown as Response)
    })

    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('dracula-theme.theme-dracula')).rejects.toThrow(/size limit/i)
    expect(bodyRead).not.toHaveBeenCalled()
  })

  it('refuses a download that streams past the cap while lying about Content-Length', async () => {
    fetchMock.mockImplementation((url: unknown) => {
      if (String(url) === GALLERY_URL) {
        return jsonResponse(galleryResponse())
      }

      let sent = 0

      return Promise.resolve({
        body: {
          getReader: () => ({
            cancel: async () => undefined,
            read: async () => {
              sent += 8 * 1024 * 1024

              return { done: false, value: new Uint8Array(8 * 1024 * 1024) }
            }
          })
        },
        headers: { get: () => null },
        ok: true,
        status: 200
      } as unknown as Response)
    })

    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('dracula-theme.theme-dracula')).rejects.toThrow(/size limit/i)
  })

  it('rejects a corrupt download instead of resolving to a silently empty theme list', async () => {
    fetchMock.mockImplementation((url: unknown) =>
      String(url) === GALLERY_URL
        ? jsonResponse(galleryResponse())
        : binaryResponse(new TextEncoder().encode('<html>404 from a caching proxy</html>'))
    )

    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('dracula-theme.theme-dracula')).rejects.toThrow(/zip/i)
  })

  it('rejects a non-200 from the CDN asset host', async () => {
    fetchMock.mockImplementation((url: unknown) =>
      String(url) === GALLERY_URL
        ? jsonResponse(galleryResponse())
        : binaryResponse(new Uint8Array(0), { status: 403 })
    )

    const { themes } = await loadShim()

    await expect(themes!.fetchMarketplace!('dracula-theme.theme-dracula')).rejects.toThrow(/403/)
  })

  it('produces a result the renderer install path actually consumes', async () => {
    fetchMock.mockImplementation((url: unknown) =>
      String(url) === GALLERY_URL ? jsonResponse(galleryResponse()) : binaryResponse(draculaVsix())
    )

    const { themes } = await loadShim()
    const result = await themes!.fetchMarketplace!('dracula-theme.theme-dracula')

    // The consumer that src/themes/install.ts feeds the bridge result into.
    // If the bridge's shape drifted from DesktopMarketplaceThemeResult this
    // throws rather than silently producing an unusable theme.
    const { buildThemeFromMarketplace } = await import('./themes/install')
    const theme = buildThemeFromMarketplace(result)

    expect(theme.label).toBe('Dracula Theme Official')
    expect(theme.description).toContain('dracula-theme.theme-dracula')
    expect(theme.darkColors).toBeDefined()
  })
})
