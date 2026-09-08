/**
 * Covers src/lib/vsix-archive.ts — the browser-native `.vsix` (zip) reader
 * that backs `themes.fetchMarketplace` on the web bridge (t_cef12883).
 *
 * The fixtures below are REAL zip archives assembled byte-by-byte in-test
 * (stored + raw-deflate entries, a genuine central directory, a genuine
 * EOCD), so the parser is exercised against the same wire format the
 * Marketplace CDN serves rather than against a mock of itself.
 */
import zlib from 'node:zlib'

import { describe, expect, it, vi } from 'vitest'

import { extractVsixThemes, MAX_ENTRY_BYTES, readCentralDirectory } from './vsix-archive'

interface FixtureEntry {
  /** Replace the deflate payload with bytes that are not valid deflate. */
  corruptDeflate?: boolean
  /** Force `stored` (method 0) instead of deflate. */
  stored?: boolean
  name: string
  text: string
}

const LOCAL_SIG = 0x04034b50
const CENTRAL_SIG = 0x02014b50
const EOCD_SIG = 0x06054b50

/** Assemble a real zip archive from `entries`. */
function buildZip(entries: FixtureEntry[], options: { entryCount?: number } = {}): Uint8Array {
  const encoder = new TextEncoder()
  const locals: Uint8Array[] = []
  const centrals: Uint8Array[] = []
  let offset = 0

  for (const entry of entries) {
    const name = encoder.encode(entry.name)
    const raw = encoder.encode(entry.text)
    const method = entry.stored ? 0 : 8
    const deflated = entry.stored ? raw : new Uint8Array(zlib.deflateRawSync(Buffer.from(raw)))
    const payload = entry.corruptDeflate ? deflated.map(byte => byte ^ 0xff) : deflated

    const local = new Uint8Array(30 + name.length + payload.length)
    const localView = new DataView(local.buffer)
    localView.setUint32(0, LOCAL_SIG, true)
    localView.setUint16(8, method, true)
    localView.setUint32(18, payload.length, true)
    localView.setUint32(22, raw.length, true)
    localView.setUint16(26, name.length, true)
    local.set(name, 30)
    local.set(payload, 30 + name.length)
    locals.push(local)

    const central = new Uint8Array(46 + name.length)
    const centralView = new DataView(central.buffer)
    centralView.setUint32(0, CENTRAL_SIG, true)
    centralView.setUint16(10, method, true)
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
  eocdView.setUint32(0, EOCD_SIG, true)
  eocdView.setUint16(8, options.entryCount ?? entries.length, true)
  eocdView.setUint16(10, options.entryCount ?? entries.length, true)
  eocdView.setUint32(12, centralSize, true)
  eocdView.setUint32(16, offset, true)

  const parts = [...locals, ...centrals, eocd]
  const total = parts.reduce((sum, part) => sum + part.length, 0)
  const out = new Uint8Array(total)
  let cursor = 0

  for (const part of parts) {
    out.set(part, cursor)
    cursor += part.length
  }

  return out
}

const DARK_THEME = JSON.stringify({
  colors: { 'editor.background': '#282a36', 'editor.foreground': '#f8f8f2' },
  name: 'Dracula',
  type: 'dark'
})

const LIGHT_THEME = JSON.stringify({
  colors: { 'editor.background': '#ffffff', 'editor.foreground': '#000000' },
  name: 'Dracula Light',
  type: 'light'
})

function manifest(themes: Array<Record<string, unknown>>, extra: Record<string, unknown> = {}): string {
  return JSON.stringify({ contributes: { themes }, displayName: 'Dracula Theme', name: 'theme-dracula', ...extra })
}

function themeVsix(): Uint8Array {
  return buildZip([
    {
      name: 'extension/package.json',
      text: manifest([
        { label: 'Dracula', path: './themes/dracula.json', uiTheme: 'vs-dark' },
        { label: 'Dracula Light', path: 'themes/dracula-light.json', uiTheme: 'vs' }
      ])
    },
    { name: 'extension/themes/dracula.json', text: DARK_THEME },
    { name: 'extension/themes/dracula-light.json', stored: true, text: LIGHT_THEME }
  ])
}

describe('readCentralDirectory', () => {
  it('maps every entry name to its record', () => {
    const records = readCentralDirectory(themeVsix())

    expect([...records.keys()]).toEqual([
      'extension/package.json',
      'extension/themes/dracula.json',
      'extension/themes/dracula-light.json'
    ])
    expect(records.get('extension/themes/dracula.json')?.method).toBe(8)
    expect(records.get('extension/themes/dracula-light.json')?.method).toBe(0)
  })

  it('rejects a buffer with no end-of-central-directory record', () => {
    expect(() => readCentralDirectory(new TextEncoder().encode('not a zip at all, just bytes'))).toThrow(
      /not a valid zip/i
    )
  })

  it('rejects a zip64 archive rather than mis-parsing its 32-bit sentinels', () => {
    const zip = themeVsix()
    // 0xffff in the EOCD entry count is zip64's "look in the zip64 record".
    new DataView(zip.buffer, zip.byteOffset, zip.byteLength).setUint16(zip.length - 22 + 10, 0xffff, true)

    expect(() => readCentralDirectory(zip)).toThrow(/zip64/i)
  })

  it('rejects a central directory offset that points outside the buffer', () => {
    const zip = themeVsix()
    new DataView(zip.buffer, zip.byteOffset, zip.byteLength).setUint32(zip.length - 22 + 16, zip.length + 500, true)

    expect(() => readCentralDirectory(zip)).toThrow(/corrupt|outside|bounds/i)
  })
})

describe('extractVsixThemes', () => {
  it('extracts every contributed color theme, inflating deflate and stored entries alike', async () => {
    const themes = await extractVsixThemes(themeVsix())

    expect(themes).toHaveLength(2)
    expect(themes[0]).toEqual({ contents: DARK_THEME, label: 'Dracula', uiTheme: 'vs-dark' })
    expect(themes[1]).toEqual({ contents: LIGHT_THEME, label: 'Dracula Light', uiTheme: 'vs' })
    expect(JSON.parse(themes[0].contents).colors['editor.background']).toBe('#282a36')
  })

  it('rejects an archive with no extension/package.json', async () => {
    const zip = buildZip([{ name: 'extension/themes/dracula.json', text: DARK_THEME }])

    await expect(extractVsixThemes(zip)).rejects.toThrow(/manifest/i)
  })

  it('skips a contributed path missing from the archive instead of returning a silent empty list', async () => {
    const zip = buildZip([
      {
        name: 'extension/package.json',
        text: manifest([
          { label: 'Ghost', path: './themes/absent.json', uiTheme: 'vs-dark' },
          { label: 'Dracula', path: './themes/dracula.json', uiTheme: 'vs-dark' }
        ])
      },
      { name: 'extension/themes/dracula.json', text: DARK_THEME }
    ])

    const themes = await extractVsixThemes(zip)

    expect(themes.map(theme => theme.label)).toEqual(['Dracula'])
  })

  it('returns an empty list when the manifest contributes no themes', async () => {
    await expect(extractVsixThemes(buildZip([{ name: 'extension/package.json', text: manifest([]) }]))).resolves.toEqual(
      []
    )
  })

  it('refuses an entry whose declared inflated size exceeds the per-entry cap', async () => {
    const zip = buildZip([
      { name: 'extension/package.json', text: manifest([{ label: 'Bomb', path: './themes/bomb.json' }]) },
      { name: 'extension/themes/bomb.json', text: DARK_THEME }
    ])

    // Lie about the uncompressed size in the central record: a zip bomb's
    // whole trick. The renderer must refuse before allocating.
    const view = new DataView(zip.buffer, zip.byteOffset, zip.byteLength)
    const bombCentralOffset = zip.length - 22 - (46 + 'extension/themes/bomb.json'.length)
    view.setUint32(bombCentralOffset + 24, MAX_ENTRY_BYTES + 1, true)

    // The oversized entry is skipped, not silently inflated.
    await expect(extractVsixThemes(zip)).resolves.toEqual([])
  })

  it('surfaces a clear error when the browser has no DecompressionStream', async () => {
    const original = globalThis.DecompressionStream
    // @ts-expect-error — deliberately removing the global to model an old browser.
    delete globalThis.DecompressionStream

    try {
      await expect(extractVsixThemes(themeVsix())).rejects.toThrow(/browser/i)
    } finally {
      globalThis.DecompressionStream = original
    }
  })

  it('surfaces the same clear error when DecompressionStream exists but rejects deflate-raw', async () => {
    // The real shape of a mid-vintage browser: DecompressionStream shipped
    // with gzip/deflate before 'deflate-raw' existed, so the constructor is
    // present and throws a raw TypeError on the format we need. Feature-
    // detecting the global alone leaks that TypeError to the user.
    const original = globalThis.DecompressionStream

    globalThis.DecompressionStream = class {
      constructor(format: string) {
        if (format === 'deflate-raw') {
          throw new TypeError(`Unsupported compression format: ${format}`)
        }

        return new original(format as CompressionFormat)
      }
    } as unknown as typeof DecompressionStream

    try {
      await expect(extractVsixThemes(themeVsix())).rejects.toThrow(/this browser cannot unpack marketplace themes/i)
      await expect(extractVsixThemes(themeVsix())).rejects.not.toThrow(/unsupported compression format/i)
    } finally {
      globalThis.DecompressionStream = original
    }
  })

  it('keeps a real decompression failure distinct from the browser-capability error', async () => {
    // Corrupt deflate bytes with the constructor working normally: this is a
    // bad archive, not an old browser, and must not be relabelled as one.
    const zip = buildZip([
      {
        name: 'extension/package.json',
        text: manifest([{ label: 'Dracula', path: './themes/dracula.json', uiTheme: 'vs-dark' }])
      },
      { corruptDeflate: true, name: 'extension/themes/dracula.json', text: DARK_THEME }
    ])

    await expect(extractVsixThemes(zip)).rejects.toThrow()
    await expect(extractVsixThemes(zip)).rejects.not.toThrow(/this browser cannot unpack marketplace themes/i)
  })

  it('never executes archive content — only package.json and the paths it names are read', async () => {
    const readNames: string[] = []

    const zip = buildZip([
      {
        name: 'extension/package.json',
        text: manifest([{ label: 'Dracula', path: './themes/dracula.json', uiTheme: 'vs-dark' }])
      },
      { name: 'extension/themes/dracula.json', text: DARK_THEME },
      { name: 'extension/extension.js', text: 'globalThis.__pwned = true' },
      { name: 'extension/package.nls.json', text: '{"x":"y"}' }
    ])

    const records = readCentralDirectory(zip)
    const decoder = TextDecoder.prototype.decode

    vi.spyOn(TextDecoder.prototype, 'decode').mockImplementation(function decode(
      this: TextDecoder,
      input?: AllowSharedBufferSource
    ) {
      const text = decoder.call(this, input)
      readNames.push(text.slice(0, 40))

      return text
    })

    try {
      await extractVsixThemes(zip)
    } finally {
      vi.restoreAllMocks()
    }

    expect(records.has('extension/extension.js')).toBe(true)
    expect((globalThis as Record<string, unknown>).__pwned).toBeUndefined()
    expect(readNames.some(text => text.includes('__pwned'))).toBe(false)
  })
})
