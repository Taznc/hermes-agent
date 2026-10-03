import { afterEach, expect, it, vi } from 'vitest'

// Exercise the public bridge member, not just the request factory.
it('removes a standalone plugin through the authenticated backend route and reports failures', async () => {
  const fetchMock = vi.fn(async (_input: URL | string, _init: RequestInit) =>
    new Response(JSON.stringify({ ok: true, path: '/home/desktop-plugins/demo' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    })
  )
  vi.stubGlobal('fetch', fetchMock)
  vi.resetModules()
  await import('../web-bridge-shim')
  const bridge = window.hermesDesktop!
  expect(await bridge.removeDesktopPlugin?.({ name: 'demo' })).toMatchObject({ ok: true })
  const [url, request] = fetchMock.mock.calls.find(([input]) => String(input).includes('desktop-remove'))!
  expect(new URL(String(url)).pathname).toBe('/api/plugins/fork-web-desktop-bridge/desktop-remove')
  expect(request.method).toBe('POST')
  expect(JSON.parse(String(request.body))).toEqual({ name: 'demo' })

  fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Not Found' }), { status: 404 }))
  expect(await bridge.removeDesktopPlugin?.({ name: 'demo' })).toMatchObject({ ok: false })
})

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(window, 'hermesDesktop')
})
