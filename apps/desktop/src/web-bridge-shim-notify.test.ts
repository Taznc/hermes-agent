// web-bridge-shim's notify() implementation: the Web Notifications API + a
// Service Worker for action buttons, standing in for Electron's IPC-backed
// `new Notification()` + click/action wiring in electron/main.ts.
//
// jsdom does not implement `Notification`/`ServiceWorkerRegistration` at all,
// so every test here stubs the exact global surface the shim reads
// (`Notification.permission`, `.requestPermission`, the constructor, and
// `navigator.serviceWorker`) rather than relying on jsdom to provide it —
// this is the shim's OWN logic under test, not a jsdom feature.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

async function loadShim() {
  vi.resetModules()
  await import('./web-bridge-shim')

  return (
    window as unknown as {
      hermesDesktop: {
        getNotificationPermission: () => Promise<string>
        notify: (payload: Record<string, unknown>) => Promise<boolean>
        onFocusSession: (cb: (id: string) => void) => () => void
        onNotificationAction: (cb: (payload: { actionId: string; sessionId?: string }) => void) => () => void
        onNotificationActivate: (
          cb: (payload: { actionId?: string; activate?: string; notifyId?: string; tag?: string }) => void
        ) => () => void
      }
    }
  ).hermesDesktop
}

function stubNotificationGlobal(permission: 'default' | 'denied' | 'granted') {
  const requestPermission = vi.fn().mockResolvedValue(permission)
  const instances: { title: string; options: Record<string, unknown>; onclick?: () => void }[] = []

  class FakeNotification {
    static permission = permission
    static requestPermission = requestPermission
    onclick: (() => void) | null = null
    close = vi.fn()

    constructor(title: string, options: Record<string, unknown> = {}) {
      instances.push({ title, options })
    }
  }

  vi.stubGlobal('Notification', FakeNotification)

  return { instances, requestPermission }
}

function stubServiceWorker(registration: { showNotification?: ReturnType<typeof vi.fn> } | null) {
  const register = vi.fn().mockResolvedValue(registration)
  const addEventListener = vi.fn()

  vi.stubGlobal('navigator', {
    ...window.navigator,
    serviceWorker: { register, addEventListener }
  })

  return { register, addEventListener }
}

describe('web-bridge-shim notify()', () => {
  afterEach(() => {
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.unstubAllGlobals()
  })

  it('returns false when the browser has no Notification API at all (e.g. some mobile browsers)', async () => {
    vi.stubGlobal('Notification', undefined)
    const bridge = await loadShim()

    await expect(bridge.notify({ title: 'Hermes' })).resolves.toBe(false)
  })

  it('requests permission on first real notify() and shows via the service worker when granted', async () => {
    stubNotificationGlobal('default').requestPermission.mockResolvedValueOnce('granted')
    const showNotification = vi.fn().mockResolvedValue(undefined)
    stubServiceWorker({ showNotification })

    const bridge = await loadShim()
    const ok = await bridge.notify({ body: 'a command needs approval', kind: 'approval', title: 'Approval needed' })

    expect(ok).toBe(true)
    expect(showNotification).toHaveBeenCalledWith(
      'Approval needed',
      expect.objectContaining({ body: 'a command needs approval' })
    )
  })

  it('returns false without showing anything when permission is denied', async () => {
    stubNotificationGlobal('denied')
    const showNotification = vi.fn()
    stubServiceWorker({ showNotification })

    const bridge = await loadShim()
    const ok = await bridge.notify({ title: 'Hermes' })

    expect(ok).toBe(false)
    expect(showNotification).not.toHaveBeenCalled()
  })

  it('does not re-prompt once permission has already been decided', async () => {
    const { requestPermission } = stubNotificationGlobal('granted')
    stubServiceWorker({ showNotification: vi.fn().mockResolvedValue(undefined) })

    const bridge = await loadShim()
    await bridge.notify({ title: 'one' })
    await bridge.notify({ title: 'two', sessionId: 'other' })

    expect(requestPermission).not.toHaveBeenCalled()
  })

  it('forwards actions to showNotification in the {action,title} shape', async () => {
    stubNotificationGlobal('granted')
    const showNotification = vi.fn().mockResolvedValue(undefined)
    stubServiceWorker({ showNotification })

    const bridge = await loadShim()
    await bridge.notify({
      actions: [
        { id: 'approve', text: 'Approve' },
        { id: 'reject', text: 'Reject' }
      ],
      title: 'Approval needed'
    })

    expect(showNotification).toHaveBeenCalledWith(
      'Approval needed',
      expect.objectContaining({
        actions: [
          { action: 'approve', title: 'Approve' },
          { action: 'reject', title: 'Reject' }
        ]
      })
    )
  })

  it('falls back to a plain Notification (no actions) when the service worker registration fails', async () => {
    stubNotificationGlobal('granted')
    stubServiceWorker(null)

    const bridge = await loadShim()
    const ok = await bridge.notify({ title: 'Hermes', body: 'still works' })

    expect(ok).toBe(true)
  })

  it('collapses a duplicate kind+session within the dedupe window (mirrors Electron isDuplicateNotification)', async () => {
    stubNotificationGlobal('granted')
    const showNotification = vi.fn().mockResolvedValue(undefined)
    stubServiceWorker({ showNotification })

    const bridge = await loadShim()
    await bridge.notify({ kind: 'turnDone', sessionId: 'abc', title: 'first' })
    await bridge.notify({ kind: 'turnDone', sessionId: 'abc', title: 'second' })

    expect(showNotification).toHaveBeenCalledTimes(1)
  })
})

describe('web-bridge-shim getNotificationPermission()', () => {
  afterEach(() => {
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.unstubAllGlobals()
  })

  it('reports "unsupported" when Notification does not exist', async () => {
    vi.stubGlobal('Notification', undefined)
    const bridge = await loadShim()

    await expect(bridge.getNotificationPermission()).resolves.toBe('unsupported')
  })

  it('reports the live Notification.permission value otherwise', async () => {
    stubNotificationGlobal('denied')
    const bridge = await loadShim()

    await expect(bridge.getNotificationPermission()).resolves.toBe('denied')
  })
})

describe('web-bridge-shim service worker click relay', () => {
  let messageListener: ((event: { data: unknown }) => void) | undefined

  beforeEach(() => {
    stubNotificationGlobal('granted')

    const addEventListener = vi.fn((type: string, cb: (event: { data: unknown }) => void) => {
      if (type === 'message') {messageListener = cb}
    })

    vi.stubGlobal('navigator', {
      ...window.navigator,
      serviceWorker: {
        register: vi.fn().mockResolvedValue({ showNotification: vi.fn().mockResolvedValue(undefined) }),
        addEventListener
      }
    })
  })

  afterEach(() => {
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.unstubAllGlobals()
    messageListener = undefined
  })

  it('routes a plain body click (no actionId) to onFocusSession', async () => {
    const bridge = await loadShim()
    await bridge.notify({ sessionId: 'sess-1', title: 'done' })

    const onFocusSession = vi.fn()
    bridge.onFocusSession(onFocusSession)

    messageListener?.({
      data: { source: 'hermes-notification-sw', data: { sessionId: 'sess-1' } }
    })

    expect(onFocusSession).toHaveBeenCalledWith('sess-1')
  })

  it('routes an approval action (sessionId, no notifyId/activate) to onNotificationAction', async () => {
    const bridge = await loadShim()
    await bridge.notify({ sessionId: 'sess-1', title: 'Approval needed' })

    const onNotificationAction = vi.fn()
    bridge.onNotificationAction(onNotificationAction)

    messageListener?.({
      data: { source: 'hermes-notification-sw', actionId: 'approve', data: { sessionId: 'sess-1' } }
    })

    expect(onNotificationAction).toHaveBeenCalledWith({ actionId: 'approve', sessionId: 'sess-1' })
  })

  it('routes a plugin action (notifyId present) to onNotificationActivate with the resolved activate path', async () => {
    const bridge = await loadShim()
    await bridge.notify({
      actions: [{ activate: 'hermes://index-network/intent/1', id: 'open', text: 'Open' }],
      notifyId: 'plugin:1',
      title: 'Opportunity'
    })

    const onNotificationActivate = vi.fn()
    bridge.onNotificationActivate(onNotificationActivate)

    messageListener?.({
      data: {
        source: 'hermes-notification-sw',
        actionId: 'open',
        data: {
          actions: [{ activate: 'hermes://index-network/intent/1', id: 'open', text: 'Open' }],
          notifyId: 'plugin:1'
        }
      }
    })

    expect(onNotificationActivate).toHaveBeenCalledWith(
      expect.objectContaining({ actionId: 'open', activate: 'hermes://index-network/intent/1', notifyId: 'plugin:1' })
    )
  })

  it('ignores messages that are not from the Hermes notification service worker', async () => {
    const bridge = await loadShim()
    const onFocusSession = vi.fn()
    bridge.onFocusSession(onFocusSession)

    messageListener?.({ data: { source: 'some-other-sw' } })

    expect(onFocusSession).not.toHaveBeenCalled()
  })
})
