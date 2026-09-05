import { describe, expect, it, vi } from 'vitest'

import { createForkPreloadApi, parseForkWindowCaps } from './preload-bridge'

// The fork half of the preload bridge. Its contract has two sides that fail
// differently, so both are pinned here:
//
//  - The renderer sees ONE object. A member missing from this module is a
//    method the renderer calls as undefined at runtime — tsc cannot catch it,
//    because `ForkDesktopApi` declares the shape rather than deriving it.
//  - `parseForkWindowCaps` runs in preload before contextBridge. If it ever
//    throws, the ENTIRE bridge is lost (window.hermesDesktop undefined =>
//    "Desktop IPC bridge is unavailable"), so malformed input must degrade to
//    no capabilities rather than an exception.

function fakeIpc() {
  return {
    invoke: vi.fn(async (...args: unknown[]) => args),
    on: vi.fn(),
    removeListener: vi.fn(),
    send: vi.fn()
  }
}

describe('parseForkWindowCaps', () => {
  it('reads the caps main passed on argv', () => {
    const caps = { glass: true, translucency: true, hud: { nativeDrag: true } }
    const argv = ['electron', `--hermes-window-caps=${encodeURIComponent(JSON.stringify(caps))}`]

    expect(parseForkWindowCaps(argv)).toEqual(caps)
  })

  it('degrades to no capabilities instead of throwing', () => {
    // Each of these would take the whole bridge down if it threw in preload.
    expect(parseForkWindowCaps([])).toEqual({})
    expect(parseForkWindowCaps(['electron'])).toEqual({})
    expect(parseForkWindowCaps(['--hermes-window-caps=not-json'])).toEqual({})
    expect(parseForkWindowCaps(['--hermes-window-caps=%E0%A4%A'])).toEqual({})
    expect(parseForkWindowCaps(['--hermes-window-caps=null'])).toEqual({})
  })
})

describe('createForkPreloadApi', () => {
  // The exact renderer-visible surface. A rename or drop here is a silent
  // runtime break in the renderer, so the list is spelled out rather than
  // derived from the implementation.
  const EXPECTED = [
    'getDevBackendStale',
    'getDevMainBundleStale',
    'onDevBackendStale',
    'onDevMainBundleStale',
    'readFileChunkForAttach',
    'restartDevBackend',
    'restartForDevBundle'
  ]

  it('exposes exactly the fork-added bridge members, all callable', () => {
    const api = createForkPreloadApi(fakeIpc() as never)

    expect(Object.keys(api).sort()).toEqual(EXPECTED)
    for (const key of EXPECTED) {
      expect(typeof (api as Record<string, unknown>)[key]).toBe('function')
    }
  })

  it('routes each member to its main-process channel', async () => {
    const ipc = fakeIpc()
    const api = createForkPreloadApi(ipc as never)

    await api.readFileChunkForAttach?.('/abs/report.txt', 4096)
    expect(ipc.invoke).toHaveBeenCalledWith('hermes:readFileChunkForAttach', '/abs/report.txt', 4096)

    await api.getDevMainBundleStale()
    expect(ipc.invoke).toHaveBeenCalledWith('hermes:dev:main-bundle-stale')

    await api.restartForDevBundle()
    expect(ipc.invoke).toHaveBeenCalledWith('hermes:dev:restart')

    await api.getDevBackendStale()
    expect(ipc.invoke).toHaveBeenCalledWith('hermes:dev:backend-stale')

    await api.restartDevBackend()
    expect(ipc.invoke).toHaveBeenCalledWith('hermes:dev:backend-restart')
  })

  it('subscriptions deliver payloads and unsubscribe the same listener', () => {
    const ipc = fakeIpc()
    const api = createForkPreloadApi(ipc as never)
    const seen: unknown[] = []

    const dispose = api.onDevBackendStale(payload => seen.push(payload))
    const [channel, listener] = ipc.on.mock.calls.at(-1) as [string, (e: unknown, p: unknown) => void]

    expect(channel).toBe('hermes:dev:backend-stale')
    listener(null, { state: 'stale' })
    expect(seen).toEqual([{ state: 'stale' }])

    dispose()
    expect(ipc.removeListener).toHaveBeenCalledWith('hermes:dev:backend-stale', listener)
  })
})
