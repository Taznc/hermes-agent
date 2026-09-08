/**
 * vsix-archive.ts — dependency-free `.vsix` reader for the browser.
 *
 * A `.vsix` is a plain zip. Electron's main process reads one with `zlib`
 * (`electron/vscode-marketplace.ts`); this is the renderer's equivalent, used
 * by web-bridge-shim.ts's `themes.fetchMarketplace` so theme install works on
 * the web-served Desktop build. Same protocol, same security posture, no zip
 * library pulled into the bundle for a feature this small.
 *
 * SECURITY POSTURE (ported deliberately, do not relax):
 * - Nothing is executed. Only `extension/package.json` and the entries its
 *   `contributes.themes[].path` names are ever decoded; every other archive
 *   member — including `extension/extension.js` — is left as bytes.
 * - Size is capped twice, because this runs in the user's tab rather than a
 *   short-lived main process: the caller caps the DOWNLOAD (MAX_VSIX_BYTES)
 *   and this module caps each entry's declared INFLATED size
 *   (MAX_ENTRY_BYTES). A zip bomb declaring a 4 GB theme file is skipped, not
 *   allocated.
 * - Every offset read out of the archive is bounds-checked against the buffer
 *   before use; a record pointing outside it is a corrupt archive, not a read.
 *
 * ZIP64 is rejected rather than half-supported: its sentinels (0xffff /
 * 0xffffffff) live in the very fields this parser reads, so silently treating
 * them as literal values would mis-parse. No color-theme extension is
 * anywhere near those limits.
 */

const LOCAL_FILE_SIG = 0x04034b50
const CENTRAL_DIR_SIG = 0x02014b50
const EOCD_SIG = 0x06054b50
const EOCD_MIN_SIZE = 22
const ZIP64_U16 = 0xffff
const ZIP64_U32 = 0xffffffff

/** Per-entry inflated-size ceiling. Color theme JSON is tens of KB. */
export const MAX_ENTRY_BYTES = 8 * 1024 * 1024

/**
 * Shared prefix for every older-browser capability failure, so `install.ts`
 * surfaces one recognizable sentence however the platform falls short.
 */
const UNSUPPORTED_BROWSER = 'This browser cannot unpack Marketplace themes '

export interface VsixThemeFile {
  contents: string
  label: string
  uiTheme?: string
}

export interface ZipEntry {
  compressedSize: number
  localOffset: number
  method: number
  uncompressedSize: number
}

function decodeUtf8(bytes: Uint8Array): string {
  return new TextDecoder('utf-8').decode(bytes)
}

/** Locate the end-of-central-directory record, scanning back from the end. */
function findEndOfCentralDirectory(view: DataView, length: number): number {
  for (let i = length - EOCD_MIN_SIZE; i >= 0; i--) {
    if (view.getUint32(i, true) === EOCD_SIG) {
      return i
    }
  }

  throw new Error('Not a valid zip archive (no end-of-central-directory record).')
}

/**
 * Parse the central directory into a `name -> record` map. Exported so the
 * archive can be inspected without decoding anything.
 */
export function readCentralDirectory(buffer: Uint8Array): Map<string, ZipEntry> {
  const view = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength)
  const eocd = findEndOfCentralDirectory(view, buffer.byteLength)
  const count = view.getUint16(eocd + 10, true)
  const directoryOffset = view.getUint32(eocd + 16, true)

  if (count === ZIP64_U16 || directoryOffset === ZIP64_U32) {
    throw new Error('Zip64 archives are not supported.')
  }

  if (directoryOffset >= buffer.byteLength) {
    throw new Error('Corrupt zip: central directory starts outside the archive.')
  }

  const records = new Map<string, ZipEntry>()
  let offset = directoryOffset

  for (let i = 0; i < count; i++) {
    if (offset + 46 > buffer.byteLength || view.getUint32(offset, true) !== CENTRAL_DIR_SIG) {
      break
    }

    const method = view.getUint16(offset + 10, true)
    const compressedSize = view.getUint32(offset + 20, true)
    const uncompressedSize = view.getUint32(offset + 24, true)
    const nameLength = view.getUint16(offset + 28, true)
    const extraLength = view.getUint16(offset + 30, true)
    const commentLength = view.getUint16(offset + 32, true)
    const localOffset = view.getUint32(offset + 42, true)
    const name = decodeUtf8(buffer.subarray(offset + 46, offset + 46 + nameLength))

    records.set(name, { compressedSize, localOffset, method, uncompressedSize })
    offset += 46 + nameLength + extraLength + commentLength
  }

  return records
}

/**
 * Build the platform decompressor, or throw the browser-capability error.
 *
 * Two distinct ways an older browser fails here, and both must land on the
 * same clear message rather than a raw TypeError: the constructor can be
 * missing outright, OR it can exist while rejecting the newer `deflate-raw`
 * format (a Chrome 80-103 / Firefox 113-pre shape — `DecompressionStream`
 * shipped with gzip+deflate before deflate-raw was added). Only CONSTRUCTION
 * is guarded: a failure once bytes are flowing is a corrupt archive, not a
 * capability gap, and must keep its own error.
 */
function createDeflateRawDecompressor(): TransformStream<BufferSource, Uint8Array> {
  const Decompressor = globalThis.DecompressionStream

  if (typeof Decompressor !== 'function') {
    throw new Error(`${UNSUPPORTED_BROWSER}(DecompressionStream is unavailable).`)
  }

  try {
    return new Decompressor('deflate-raw')
  } catch (cause) {
    throw new Error(`${UNSUPPORTED_BROWSER}(DecompressionStream does not support 'deflate-raw').`, { cause })
  }
}

/** Inflate raw-deflate bytes with the platform's own decompressor. */
async function inflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const decompressor = createDeflateRawDecompressor()

  // Built from a ReadableStream rather than Blob.stream()/Response so the
  // path is identical in the browser and in the jsdom test environment.
  const source = new ReadableStream<BufferSource>({
    start(controller) {
      // Copy into a fresh buffer: `data` is a view onto the whole archive,
      // and the stream must not keep it alive (or hand a view of unrelated
      // bytes to the decompressor).
      controller.enqueue(new Uint8Array(data))
      controller.close()
    }
  })

  const reader = source.pipeThrough(decompressor).getReader()
  const chunks: Uint8Array[] = []
  let total = 0

  for (;;) {
    const { done, value } = await reader.read()

    if (done) {
      break
    }

    total += value.length

    if (total > MAX_ENTRY_BYTES) {
      await reader.cancel()

      throw new Error('Extension entry exceeded the size limit while decompressing.')
    }

    chunks.push(value)
  }

  const out = new Uint8Array(total)
  let cursor = 0

  for (const chunk of chunks) {
    out.set(chunk, cursor)
    cursor += chunk.length
  }

  return out
}

/**
 * Decode one entry to text. Returns null when the entry cannot be read
 * safely — an unsupported compression method, an out-of-bounds record, or a
 * declared inflated size past the cap — so one bad member never fails an
 * otherwise-valid install (matching Electron's per-entry skip).
 */
async function readEntryText(buffer: Uint8Array, record: ZipEntry): Promise<null | string> {
  if (record.uncompressedSize > MAX_ENTRY_BYTES || record.compressedSize > MAX_ENTRY_BYTES) {
    return null
  }

  if (record.localOffset + 30 > buffer.byteLength) {
    return null
  }

  const view = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength)

  if (view.getUint32(record.localOffset, true) !== LOCAL_FILE_SIG) {
    return null
  }

  // The local header's name/extra lengths can differ from the central
  // record's, so re-read them here to find the payload.
  const nameLength = view.getUint16(record.localOffset + 26, true)
  const extraLength = view.getUint16(record.localOffset + 28, true)
  const start = record.localOffset + 30 + nameLength + extraLength
  const end = start + record.compressedSize

  if (end > buffer.byteLength) {
    return null
  }

  const data = buffer.subarray(start, end)

  // 0 = stored, 8 = raw deflate. Theme files are one or the other.
  if (record.method === 0) {
    return decodeUtf8(data)
  }

  if (record.method !== 8) {
    return null
  }

  return decodeUtf8(await inflateRaw(data))
}

/** Normalize a package.json theme path to its zip entry name. */
export function themeEntryName(themePath: string): string {
  const clean = String(themePath)
    .replace(/^\.\//, '')
    .replace(/^\//, '')

  return `extension/${clean}`
}

/**
 * Extract every color theme a `.vsix` contributes. Throws on an archive that
 * is not readable at all (not a zip, no manifest, unsupported browser) so the
 * caller surfaces a real error instead of a silently empty install.
 */
export async function extractVsixThemes(buffer: Uint8Array): Promise<VsixThemeFile[]> {
  const records = readCentralDirectory(buffer)
  const manifestRecord = records.get('extension/package.json')

  if (!manifestRecord) {
    throw new Error('Package manifest missing from the extension.')
  }

  const manifestText = await readEntryText(buffer, manifestRecord)

  if (manifestText === null) {
    throw new Error('Package manifest could not be read from the extension.')
  }

  const manifest = JSON.parse(manifestText) as {
    contributes?: { themes?: Array<{ id?: string; label?: string; path?: string; uiTheme?: string }> }
    displayName?: string
    name?: string
  }

  const contributed = manifest.contributes?.themes

  if (!Array.isArray(contributed) || contributed.length === 0) {
    return []
  }

  const themes: VsixThemeFile[] = []

  for (const entry of contributed) {
    if (!entry?.path) {
      continue
    }

    const record = records.get(themeEntryName(entry.path))

    if (!record) {
      continue
    }

    const contents = await readEntryText(buffer, record)

    if (contents === null) {
      continue
    }

    themes.push({
      contents,
      label: entry.label || entry.id || manifest.displayName || manifest.name || 'VS Code Theme',
      ...(entry.uiTheme ? { uiTheme: entry.uiTheme } : {})
    })
  }

  return themes
}
