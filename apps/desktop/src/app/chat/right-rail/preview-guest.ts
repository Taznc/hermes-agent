/**
 * PREVIEW GUEST CAPABILITY — is there an embedded browser engine behind the
 * preview pane at all?
 *
 * The pane's live pages render in Electron's proprietary `<webview>` guest.
 * The web build serves the same renderer to an ordinary browser, where
 * `<webview>` is an inert unknown custom element: it never navigates, and
 * `loadURL` / `sendInputEvent` / `executeJavaScript` / `getWebContentsId` do
 * not exist. Everything the agent's preview tools do goes through that guest,
 * so without one they are not "slow" or "not ready" — they are absent.
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

/** read_preview's refusal: name the tool that CAN get the page's text. */
export const PREVIEW_NO_GUEST_READ =
  'This build has no embedded browser engine, so the in-app browser cannot render or read a page here. ' +
  'Fetch the page with web_extract, or drive a real browser with the browser toolset.'

/** drive_preview / annotate_preview's refusal — both act on the guest page. */
export const PREVIEW_NO_GUEST_DRIVE =
  'The in-app browser has no controllable guest in this build, so clicking, typing, scrolling and ' +
  'annotating the page are unavailable. Drive a real browser with the browser toolset instead.'
