/**
 * PREVIEW GUEST CAPABILITY — can this renderer host an in-pane web guest?
 *
 * The browser pane drives Electron's proprietary `<webview>` tag imperatively
 * (`loadURL`, `did-navigate`, `getWebContentsId`, `sendInputEvent`). Electron
 * upgrades that element synchronously at creation, so the element it hands
 * back already carries those methods. A plain browser — the web-served build —
 * treats `<webview>` as an inert unknown element instead: `src` never
 * navigates, `loadURL` is `undefined`, and the pane paints an empty rectangle
 * forever. That blank pane is what this probe exists to prevent.
 *
 * Probe the ELEMENT, not the shell. Capability is a property of what this page
 * can actually do (root AGENTS.md, "Surface capability is a property of the
 * SESSION"), so `navigator.userAgent`, the hostname, or a `platform === 'web'`
 * flag are all the wrong question: they answer "who launched me", not "can
 * this document host a guest", and they go stale the day the web build gains a
 * real guest.
 *
 * Deliberately uncached: it is read a handful of times per render of one pane,
 * and staying cache-free keeps it honest under test (same reasoning as
 * `windowProfileOverride` in store/windows.ts).
 */
export function previewGuestSupported(): boolean {
  if (typeof document === 'undefined') {
    return false
  }

  try {
    const probe = document.createElement('webview') as { loadURL?: unknown }

    return typeof probe.loadURL === 'function'
  } catch {
    return false
  }
}

/**
 * Open an address as a normal top-level browser tab — what the pane does with
 * a URL when it has no guest to load it into.
 *
 * Two rungs, in precedence order: the app's own bridge (`openExternal`, which
 * Electron routes to the OS browser and the web shim implements as
 * `window.open`), then `window.open` directly for a renderer with no bridge
 * installed at all. Nothing below that — an address the page cannot open is a
 * real failure the caller surfaces, not something to swallow.
 *
 * `blockedMessage` is the localized copy for the one failure this can produce
 * on its own (a popup blocker refusing the tab); the caller owns it because
 * this module has no i18n context.
 */
export async function openInBrowserTab(url: string, blockedMessage: string): Promise<void> {
  const openExternal = window.hermesDesktop?.openExternal

  if (openExternal) {
    await openExternal(url)

    return
  }

  // `noopener` is what severs `window.opener`, so the tab we open cannot reach
  // back into this document.
  if (!window.open(url, '_blank', 'noopener,noreferrer')) {
    throw new Error(blockedMessage)
  }
}
