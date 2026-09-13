/**
 * Web-build-only reload state, plus the one safe way for ANY app code to
 * force a real navigation reload.
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
 * The trap intercepts the GLOBAL `window.location.reload`, so it can't tell
 * Vite's HMR client apart from our own app code — boot-failure recovery,
 * crash-boundary recovery, the command-palette "Reload Window" action, and
 * the ⌘R passthrough all call `window.location.reload()` too, and all of
 * those are supposed to always actually reload, in every build (Electron,
 * production web, DEV web). Those call sites must call `performWebReload()`
 * below instead of `window.location.reload()` directly — it always performs
 * a real reload and is never trapped, because it goes around
 * `window.location.reload` rather than through it.
 *
 * See docs/web-ui-hard-refresh-diagnosis.md for the full investigation.
 */
import { atom } from 'nanostores'

export const $webReloadPending = atom(false)

export function markWebReloadPending(): void {
  $webReloadPending.set(true)
}

// The real `location.reload`, captured by web-bridge-shim.ts before it
// overrides `window.location.reload`. Only set in the DEV web build (the one
// build where the trap installs). Everywhere else — Electron, a production
// web build — `window.location.reload` was never touched, so it's still the
// native implementation and performWebReload() falls through to calling it
// directly.
let nativeReload: (() => void) | null = null

export function registerNativeWebReload(fn: () => void): void {
  nativeReload = fn
}

/**
 * Performs a real, always-executing page reload. Safe to call from ANY app
 * code that means "reload now, unconditionally" — boot-failure recovery,
 * crash-boundary recovery, the command palette, the ⌘R passthrough, and the
 * user-initiated "Refresh" statusbar item all use this instead of
 * `window.location.reload()` directly, so none of them can be silently
 * swallowed by the DEV-web-only HMR trap above.
 *
 * - DEV web build (trap installed): calls the captured native reload,
 *   bypassing the trap entirely.
 * - Electron, or a production web build (trap never installed): `nativeReload`
 *   is null, so this falls through to `window.location.reload()`, which at
 *   that point is still the real, unmodified implementation.
 */
export function performWebReload(): void {
  if (nativeReload) {
    nativeReload()

    return
  }

  window.location.reload()
}

