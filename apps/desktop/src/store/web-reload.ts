/**
 * Web-build-only reload state.
 *
 * In the browser build (apps/desktop served as a plain web page via
 * vite.config.web.ts / index-web.html), Vite's own HMR client calls
 * `window.location.reload()` directly whenever an edited module can't Fast
 * Refresh (any file that also exports a non-component value — a nanostore,
 * an i18n locale object, a helper) or when the dev-server WebSocket
 * reconnects. That silently destroys in-progress composer/chat state.
 * `vite:beforeFullReload` listeners cannot cancel the reload — Vite notifies
 * them and proceeds regardless — so `src/web-bridge-shim.ts` traps the
 * `location.reload` call itself (DEV-only) and flips `$webReloadPending`
 * instead of navigating. `use-statusbar-items.tsx` renders a manual blue
 * "Refresh" affordance (mirroring the Electron dev app's "Restart to apply"
 * button) so the user decides when to actually reload.
 *
 * See docs/web-ui-hard-refresh-diagnosis.md for the full investigation.
 */
import { atom } from 'nanostores'

export const $webReloadPending = atom(false)

export function markWebReloadPending(): void {
  $webReloadPending.set(true)
}

// The real `location.reload`, captured by web-bridge-shim.ts before it
// overrides `window.location.reload`. Stays null under Electron (and in any
// build where the shim never runs) — performWebReload() is a no-op there.
let nativeReload: (() => void) | null = null

export function registerNativeWebReload(fn: () => void): void {
  nativeReload = fn
}

/** Performs the real page reload the button promises. Called only from the
 *  user-initiated "Refresh" statusbar item — never automatically. */
export function performWebReload(): void {
  nativeReload?.()
}

