// web-bridge-shim's saveGatewayFile(): the web build's stand-in for
// Electron's saveGatewayFile (electron/main.ts), which fetches
// /api/fs/download from the main process, prompts a native save dialog, and
// streams the response to the chosen destination. A browser tab has no save
// dialog, so the shim triggers a real browser download instead (object URL +
// `<a download>`) — see the `Interpretation:` comment on t_a017ac79.
//
// jsdom does not implement URL.createObjectURL/revokeObjectURL or
// HTMLAnchorElement.click() navigation, so every test stubs exactly the
// surface the implementation touches, same pattern as
// web-bridge-shim-notify.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type SaveGatewayFileBridge = {
  saveGatewayFile: NonNullable<Window['hermesDesktop']['saveGatewayFile']>
}

async function loadShim(): Promise<SaveGatewayFileBridge> {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (window as unknown as { hermesDesktop: SaveGatewayFileBridge }).hermesDesktop
}

// The shim also fires a fire-and-forget GET /api/local-models/status on
// import (correctLocalModelsEnabledFlag) — unrelated to saveGatewayFile but
// sharing the same fetchMock, so every assertion below finds ITS call rather
// than assuming index 0.
function downloadCall(mock: ReturnType<typeof vi.fn>) {
  const call = mock.mock.calls.find(([url]) => String(url).includes('/api/fs/download'))

  if (!call) {
    throw new Error('no /api/fs/download call recorded')
  }

  return call as [URL, RequestInit]
}

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    text: async () => JSON.stringify(payload)
  }
}

function fileResponse(
  bytes: Uint8Array,
  { contentDisposition, ok = true, status = 200 }: { contentDisposition?: string; ok?: boolean; status?: number } = {}
) {
  return {
    ok,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === 'content-disposition' ? (contentDisposition ?? null) : null)
    },
    blob: async () => new Blob([bytes.slice().buffer]),
    text: async () => 'error body'
  }
}

describe('web-bridge-shim saveGatewayFile', () => {
  let fetchMock: ReturnType<typeof vi.fn>
  let createObjectURL: ReturnType<typeof vi.fn>
  let revokeObjectURL: ReturnType<typeof vi.fn>
  let clickedAnchors: HTMLAnchorElement[]

  beforeEach(() => {
    localStorage.setItem('hermes-web-spike-token', 'test-session-token')
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    createObjectURL = vi.fn(() => 'blob:mock-object-url')
    revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL }))

    clickedAnchors = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clickedAnchors.push(this)
    })

    vi.useFakeTimers()
  })

  afterEach(() => {
    localStorage.removeItem('hermes-web-spike-token')
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('downloads real bytes and resolves saved:true with no path (browsers cannot report one)', async () => {
    const bytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]) // arbitrary binary payload

    fetchMock.mockImplementation((url: URL) =>
      Promise.resolve(
        String(url).includes('/api/fs/download')
          ? fileResponse(bytes, { contentDisposition: 'attachment; filename="report.png"' })
          : jsonResponse({ enabled: false })
      )
    )

    const { saveGatewayFile } = await loadShim()

    const result = await saveGatewayFile({
      path: '/home/u/project/report.png',
      profile: 'docker-gw',
      sessionId: 'sess-1'
    })

    expect(result).toEqual({ saved: true })
    expect(createObjectURL).toHaveBeenCalledTimes(1)

    // Bytes reach the Blob unmangled — proves this isn't a text-mode/base64
    // round trip that would corrupt a binary file. Compare the VIEWS, not the
    // ArrayBuffers: vitest's toEqual reports two ArrayBuffers equal regardless
    // of length or contents, so `resolves.toEqual(bytes.buffer)` asserts
    // nothing (t_b398a331).
    const blobArg = createObjectURL.mock.calls[0][0] as Blob

    expect(new Uint8Array(await blobArg.arrayBuffer())).toEqual(bytes)

    const [url, init] = downloadCall(fetchMock)

    const parsedUrl = new URL(String(url))

    expect(parsedUrl.pathname).toBe('/api/fs/download')
    expect(parsedUrl.searchParams.get('path')).toBe('/home/u/project/report.png')
    expect(parsedUrl.searchParams.get('profile')).toBe('docker-gw')
    expect(parsedUrl.searchParams.get('session_id')).toBe('sess-1')
    expect(init.credentials).toBe('include')
    expect((init.headers as Record<string, string>)['X-Hermes-Session-Token']).toBe('test-session-token')
  })

  it('uses server, suggested, then path filenames and revokes every object URL', async () => {
    fetchMock.mockImplementation((url: URL) => {
      if (!String(url).includes('/api/fs/download')) {
        return Promise.resolve(jsonResponse({ enabled: false }))
      }

      const path = new URL(String(url)).searchParams.get('path')

      const options =
        path === '/tmp/server.txt' ? { contentDisposition: 'attachment; filename="server-name.txt"' } : undefined

      return Promise.resolve(fileResponse(new Uint8Array([1]), options))
    })

    const { saveGatewayFile } = await loadShim()

    await saveGatewayFile({ path: '/tmp/server.txt', suggestedName: 'ignored.txt' })
    await saveGatewayFile({ path: '/tmp/internal.txt', suggestedName: 'suggested-name.txt' })
    await saveGatewayFile({ path: '/tmp/path-name.txt' })

    expect(clickedAnchors.map(anchor => anchor.download)).toEqual([
      'server-name.txt',
      'suggested-name.txt',
      'path-name.txt'
    ])
    expect(revokeObjectURL).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(30_000)

    expect(revokeObjectURL).toHaveBeenCalledTimes(3)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-object-url')
  })

  it('rejects on a non-2xx response instead of silently doing nothing', async () => {
    fetchMock.mockImplementation((url: URL) =>
      Promise.resolve(
        String(url).includes('/api/fs/download')
          ? jsonResponse({ detail: 'Access to sensitive files is not allowed' }, false, 403)
          : jsonResponse({ enabled: false })
      )
    )

    const { saveGatewayFile } = await loadShim()

    await expect(saveGatewayFile({ path: '/etc/shadow' })).rejects.toThrow('403')
    expect(createObjectURL).not.toHaveBeenCalled()
    expect(clickedAnchors).toHaveLength(0)
  })
})
