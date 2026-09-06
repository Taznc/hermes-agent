/**
 * The AGENT-facing preview tools are wired through Electron's `<webview>`
 * guest. The web build serves the same renderer to an ordinary browser, where
 * that element is inert — so every registration below has to be gated on the
 * guest actually existing, and the tools have to say so instead of answering
 * with a plausible-looking blank page or "retry in a moment".
 *
 * Both branches are exercised against the real PreviewPane: the discriminating
 * fact is only ever `document.createElement('webview').loadURL`.
 */

import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $rightRailActiveTabId } from '@/store/layout'
import { closeRightRail, openPreview, type PreviewTarget } from '@/store/preview'
import { $connection, $selectedStoredSessionId } from '@/store/session'

import { actOnActivePreview } from './preview-act'
import { activePreviewGuestMissing, openInBrowserTab, previewGuestSupported } from './preview-guest'
import { activePreviewInput } from './preview-input'
import { PreviewPane } from './preview-pane'
import { readActivePreview } from './preview-reader'
import { activePreviewScriptRunner } from './preview-script-runner'

vi.mock('./real-profile-consent-dialog', () => ({
  RealProfileConsentDialog: () => null
}))

const PAGE_URL = 'http://localhost:5174'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

function urlTarget(): PreviewTarget {
  return { kind: 'url', label: 'Browser', source: PAGE_URL, url: PAGE_URL }
}

/** Give every `<webview>` this document creates the Electron guest API. The
 *  pane's element and the capability probe's throwaway element both come from
 *  `document.createElement`, so one stub speaks for both — which is the point:
 *  the probe must describe the same engine the pane would get. */
function stubElectronGuest(sendInputEvent = vi.fn()) {
  /* eslint-disable no-restricted-globals -- the factory itself is under test:
     the capability probe and the pane both get their element from
     document.createElement, and one stub has to speak for both. */
  const create = document.createElement.bind(document)

  vi.spyOn(document, 'createElement').mockImplementation(((tag: string, options?: ElementCreationOptions) => {
    const element = create(tag, options)
    /* eslint-enable no-restricted-globals */

    if (String(tag).toLowerCase() === 'webview') {
      Object.assign(element, {
        canGoBack: () => false,
        canGoForward: () => false,
        executeJavaScript: vi.fn(async () => 'rendered page text'),
        getTitle: () => 'Dev server',
        getURL: () => `${PAGE_URL}/app`,
        getWebContentsId: () => 7,
        loadURL: vi.fn(),
        sendInputEvent
      })
    }

    return element
  }) as typeof document.createElement)

  return sendInputEvent
}

/** Mount the pane the way the rail does, so its registration effects run. The
 *  pane must carry the SAME tab id the store minted, or the registries it
 *  writes to are keyed off the tab the tools resolve. */
async function mountBrowserPane() {
  openPreview(urlTarget(), 'tool-result')

  const tabId = $rightRailActiveTabId.get()!

  await act(async () => {
    render(<PreviewPane tabId={tabId} target={urlTarget()} />)
  })

  return tabId
}

describe('preview agent tools without an Electron guest (web build)', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(Date.now()), 0)
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
    closeRightRail()
    window.localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    closeRightRail()
    $connection.set(null)
    $selectedStoredSessionId.set(null)
  })

  // jsdom is the web build's own situation: `<webview>` is an unknown element
  // with no loadURL. If this ever reports true here, every assertion below is
  // measuring nothing.
  it('reports no guest support in a plain browser document', () => {
    expect(previewGuestSupported()).toBe(false)

    openPreview(urlTarget(), 'tool-result')
    expect(activePreviewGuestMissing()).toBe(true)
  })

  it('registers no reader, script runner or input channel for a page it cannot host', async () => {
    await mountBrowserPane()

    // The bug this replaces: registering handles that call methods the element
    // does not have, so drive_preview clicks were silent misses.
    expect(activePreviewInput()).toBeNull()
    expect(activePreviewScriptRunner()).toBeNull()
  })

  it('read_preview refuses honestly instead of answering with an empty page', async () => {
    await mountBrowserPane()

    const result = await readActivePreview()

    expect(result?.text).toBe('')
    // The old answer was "the page has not finished loading — retry in a
    // moment", which is a lie about a capability that will never arrive.
    expect(result?.note).not.toContain('retry')
    expect(result?.note).toContain('web_extract')
    expect(result?.note).toContain('browser')
  })

  it('drive_preview names the missing capability rather than asking for another open_preview', async () => {
    await mountBrowserPane()

    for (const action of [{ kind: 'click', ref: '@e1' }, { kind: 'elements' }, { kind: 'back' }]) {
      const result = await actOnActivePreview(action)

      expect(result.success).toBe(false)
      expect(result.error).toContain('no controllable guest')
      // "open one with open_preview first" sends the agent round the same loop
      // for a tab that IS open.
      expect(result.error).not.toContain('open_preview')
    }
  })

  it('refuses annotate_preview through the same bridge', async () => {
    await mountBrowserPane()

    // annotate_preview is not a separate renderer path: the tool wires to
    // `drive_preview_callback` and arrives here as pin/unpin/hold. Its capture
    // step additionally needs `getWebContentsId`, which no guest ever provides
    // here — so it must refuse for the same stated reason, not fail later at
    // the capture.
    for (const action of [{ kind: 'pin', ref: '@e1', text: 'here' }, { kind: 'unpin' }, { kind: 'hold' }]) {
      expect(await actOnActivePreview(action)).toMatchObject({
        error: expect.stringContaining('no controllable guest') as string,
        success: false
      })
    }
  })

  it('leaves the empty rail answering "nothing is open", not "no guest"', async () => {
    // Discrimination in the other direction: the refusal is about a tab that
    // wants a page, so an empty rail must keep its own (correct) copy.
    expect(activePreviewGuestMissing()).toBe(false)
    expect((await actOnActivePreview({ kind: 'elements' })).error).toContain('open_preview')
  })
})

describe('preview agent tools with an Electron guest', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(Date.now()), 0)
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
    closeRightRail()
    window.localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    closeRightRail()
    $connection.set(null)
    $selectedStoredSessionId.set(null)
  })

  it('sees the guest capability when the element can navigate', () => {
    stubElectronGuest()

    expect(previewGuestSupported()).toBe(true)

    openPreview(urlTarget(), 'tool-result')
    expect(activePreviewGuestMissing()).toBe(false)
  })

  it('still registers the page reader and the input handle', async () => {
    const send = stubElectronGuest()

    await mountBrowserPane()

    expect(activePreviewScriptRunner()).not.toBeNull()

    const input = activePreviewInput()

    expect(input).not.toBeNull()
    input?.send({ type: 'mouseMove', x: 10, y: 20 })
    expect(send).toHaveBeenCalledWith({ type: 'mouseMove', x: 10, y: 20 })

    // The reader answers with the LIVE page, not the identity fallback (whose
    // whole signature is empty text plus an explanatory note).
    const page = await readActivePreview()

    expect(page).toMatchObject({
      text: 'rendered page text',
      title: 'Dev server',
      url: `${PAGE_URL}/app`
    })
    expect(page?.note).toBeUndefined()
  })
})

/**
 * The predicate and the tab-opener as UNITS — the two cases above prove them
 * through the mounted pane, these pin the module's own contract (what the probe
 * refuses to look at, and each rung of the open ladder including its failure).
 */
describe('previewGuestSupported', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    delete desktopWindow.hermesDesktop
  })

  // The probe answers "can this document host a guest", never "who launched
  // me" — a UA/hostname/platform test would go stale the day the web build
  // gains a real guest, and lies today for anyone spoofing either.
  it('ignores the user agent and the hostname', () => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (X11; Linux x86_64) HermesDesktop/1.0 Electron/38.0.0'
    )
    desktopWindow.hermesDesktop = { platform: 'darwin' } as unknown as Window['hermesDesktop']

    expect(previewGuestSupported()).toBe(false)
  })
})

describe('openInBrowserTab', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    delete desktopWindow.hermesDesktop
  })

  it('prefers the app bridge, which is the top-level-tab route in both builds', async () => {
    const openExternal = vi.fn(async () => undefined)
    const open = vi.spyOn(window, 'open')
    desktopWindow.hermesDesktop = { openExternal } as unknown as Window['hermesDesktop']

    await openInBrowserTab('https://google.com', 'blocked')

    expect(openExternal).toHaveBeenCalledWith('https://google.com')
    expect(open).not.toHaveBeenCalled()
  })

  it('falls back to window.open with the opener severed when no bridge is installed', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue({} as Window)

    await openInBrowserTab('https://google.com', 'blocked')

    expect(open).toHaveBeenCalledWith('https://google.com', '_blank', 'noopener,noreferrer')
  })

  // A popup blocker returns null. That is a real failure the user has to see,
  // not something to swallow into another silent no-op.
  it('rejects with the caller-supplied copy when the browser blocks the tab', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)

    await expect(openInBrowserTab('https://google.com', 'Allow pop-ups.')).rejects.toThrow('Allow pop-ups.')
  })
})
