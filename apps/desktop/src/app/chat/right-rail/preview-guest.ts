/**
 * PREVIEW GUEST CAPABILITY — is there an embedded browser engine behind the
 * preview pane at all?
 *
 * The pane's live pages render in Electron's proprietary `<webview>` guest,
 * driven imperatively (`loadURL`, `did-navigate`, `getWebContentsId`,
 * `sendInputEvent`, `executeJavaScript`). Electron upgrades that element
 * synchronously at creation, so the element it hands back already carries
 * those methods. The web build serves the same renderer to an ordinary
 * browser, where `<webview>` is an inert unknown custom element: `src` never
 * navigates, none of those methods exist, and the pane paints an empty
 * rectangle forever. Two failures share that one cause — the blank Browser
 * pane the user sees, and the agent's preview tools, which are not "slow" or
 * "not ready" without a guest but absent.
 *
 * Probe the ELEMENT, not the shell. A hostname, a user-agent string, or
 * `desktopVersion.platform === 'web'` all answer a different question ("which
 * build is this?") and go stale the moment the answer changes; custom-element
 * upgrade is synchronous, so asking the element whether it can navigate is
 * both exact and still correct if the web build ever gains a real guest.
 *
 * The surface a session can reach is a property of that session's CLIENT (root
 * AGENTS.md, "Surface capability is a property of the SESSION"). The session
 * source says "desktop", which is true of the shell and says nothing about the
 * guest — so the guest gets its own named predicate rather than an `isWeb`
 * flag smuggled in beside it.
 */

import { $rightRailActiveTabId } from '@/store/layout'
import { $previewTabs } from '@/store/preview'

/** Whether this build can host a live page inside the preview pane. */
export function previewGuestSupported(): boolean {
  const element = document.createElement('webview')

  return typeof (element as { loadURL?: unknown }).loadURL === 'function'
}

/**
 * True when the user is looking at a tab that WANTS a live guest page but this
 * build cannot host one. Distinct from "nothing is open": the honest answer to
 * the agent is "there is a page tab, but no engine behind it", which must not
 * be confused with the ordinary empty-rail case (whose existing "open one with
 * open_preview" copy is already correct).
 *
 * The tab test mirrors the pane's own `isWebPreview`: a URL tab, or an HTML
 * file being RENDERED rather than read as source. A file peek or an artifact
 * never had a guest on any build, so it is not this defect.
 */
export function activePreviewGuestMissing(): boolean {
  const tabs = $previewTabs.get()
  const tab = tabs.find(t => t.id === $rightRailActiveTabId.get()) ?? tabs[0]
  const target = tab?.target

  if (!target || target.kind === 'artifact') {
    return false
  }

  const wantsGuest = target.kind === 'url' || (target.previewKind === 'html' && target.renderMode !== 'source')

  return wantsGuest && !previewGuestSupported()
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

/** read_preview's refusal: name the tool that CAN get the page's text. */
export const PREVIEW_NO_GUEST_READ =
  'This build has no embedded browser engine, so the in-app browser cannot render or read a page here. ' +
  'Fetch the page with web_extract, or drive a real browser with the browser toolset.'

/** drive_preview / annotate_preview's refusal — both act on the guest page. */
export const PREVIEW_NO_GUEST_DRIVE =
  'The in-app browser has no controllable guest in this build, so clicking, typing, scrolling and ' +
  'annotating the page are unavailable. Drive a real browser with the browser toolset instead.'
