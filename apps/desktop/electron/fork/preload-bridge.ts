import type { IpcRenderer } from 'electron'

// Fork-owned half of the Desktop preload bridge.
//
// Upstream owns `electron/preload.ts`; the members this fork ADDS to the
// `hermesDesktop` bridge are built here and spread in at a single anchor, so an
// upstream sync never merges two sets of appended bridge members.
//
// This is the runtime counterpart of `src/fork/desktop-api.d.ts`
// (`ForkDesktopApi`) — a bridge method cannot move on only one side, so the two
// files are one contract and change together.
//
// The main-process IPC handlers these call stay registered in main.ts: channel
// routing and capability checks are security authority and belong upstream. All
// this module owns is the renderer-facing shape.

/** HUD windowing capabilities, as computed once by main and passed on argv. */
export interface ForkHudWindowing {
  clientPlacement?: boolean
  controlDrag?: boolean
  nativeDrag?: boolean
  solid?: boolean
  workspaceTransfer?: boolean
}

export interface ForkWindowCaps {
  glass?: boolean
  hud?: ForkHudWindowing
  translucency?: boolean
}

/**
 * Reads the window capabilities main passed as `--hermes-window-caps=<json>`.
 *
 * Which translucency the OS can back, and the HUD's windowing capabilities, are
 * process-constant (decided once from process.platform / os.release() / argv,
 * never per-window), so main computes them a single time and hands them to
 * every window through `webPreferences.additionalArguments` (see
 * withWindowCapsArgument() in main.ts). Reading them off argv means the
 * renderer's first paint never blocks on a synchronous IPC round-trip — the
 * whole reason this used to be `ipcRenderer.sendSync`, which stalled every
 * window in preload whenever main was busy (cold boot, a slow backend probe).
 *
 * A sandboxed preload may only require electron, events, timers and url, but
 * `process.argv` is part of the limited process object Electron still exposes
 * to sandboxed preload scripts, so this parse needs neither node:os nor
 * node:process.
 *
 * Falls back to `{}` on a missing or malformed argument: no caps degrades to an
 * ordinary opaque window, never to a broken bridge. A preload throw would take
 * the ENTIRE bridge down (window.hermesDesktop undefined => "Desktop IPC bridge
 * is unavailable"), so this must not throw.
 *
 * `argv` is a parameter so the parse is testable without an Electron process.
 */
export function parseForkWindowCaps(argv: readonly string[] = process.argv): ForkWindowCaps {
  const prefix = '--hermes-window-caps='
  const arg = argv.find(entry => entry.startsWith(prefix))

  if (!arg) {
    return {}
  }

  try {
    return (JSON.parse(decodeURIComponent(arg.slice(prefix.length))) as ForkWindowCaps) || {}
  } catch {
    return {}
  }
}

/**
 * Builds the fork-added members of the `hermesDesktop` bridge.
 *
 * Spread into the object passed to `contextBridge.exposeInMainWorld` at the
 * anchor in preload.ts. The returned shape must stay assignable to
 * `ForkDesktopApi` in `src/fork/desktop-api.d.ts`; contextBridge serializes
 * across the isolated-world boundary, so only functions and plain data may
 * appear here.
 */
export function createForkPreloadApi(ipcRenderer: IpcRenderer) {
  return {
    readFileChunkForAttach: (filePath: string, offset: number) =>
      ipcRenderer.invoke('hermes:readFileChunkForAttach', filePath, offset),

    // Dev-only: is the built main-process bundle newer than the running one?
    // `supported` is false in a packaged build so the UI stays hidden there.
    getDevMainBundleStale: () => ipcRenderer.invoke('hermes:dev:main-bundle-stale'),
    restartForDevBundle: () => ipcRenderer.invoke('hermes:dev:restart'),
    onDevMainBundleStale: (callback: (payload: { stale: boolean }) => void) => {
      const listener = (_event: unknown, payload: { stale: boolean }) => callback(payload)
      ipcRenderer.on('hermes:dev:main-bundle-stale', listener)

      return () => ipcRenderer.removeListener('hermes:dev:main-bundle-stale', listener)
    },

    // Dev-only: has backend Python source the running `hermes serve` child
    // already imported changed on disk (agent/ tui_gateway/ tools/ hermes_cli/)?
    // `supported` is false in a packaged build and against a remote primary.
    getDevBackendStale: () => ipcRenderer.invoke('hermes:dev:backend-stale'),
    restartDevBackend: () => ipcRenderer.invoke('hermes:dev:backend-restart'),
    onDevBackendStale: (callback: (payload: { state: 'fresh' | 'stale' | 'restarting' | 'failed' }) => void) => {
      const listener = (_event: unknown, payload: { state: 'fresh' | 'stale' | 'restarting' | 'failed' }) =>
        callback(payload)

      ipcRenderer.on('hermes:dev:backend-stale', listener)

      return () => ipcRenderer.removeListener('hermes:dev:backend-stale', listener)
    }
  }
}
