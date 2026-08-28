// dev-backend-watch.ts
//
// Dev-only detection of backend Python source going stale under a running
// `hermes serve` child, plus the "offer, don't act" restart affordance that
// covers it. Sibling to dev-electron-watch.mjs / the devMainBundleStale
// machinery in main.ts, which already do the same job for the Electron
// main-process bundle — this closes the one surface neither that watcher nor
// Vite's renderer HMR covers: agent/, tui_gateway/, tools/, hermes_cli/.
//
// Pure and dependency-free (no electron, no fs) so the filtering and the
// state machine are unit-testable without a real filesystem watcher or a
// live backend process. main.ts owns the actual fs.watch() calls, IPC
// wiring, and the real restart (a soft primary-backend teardown — the same
// re-home path connection-config/profile switches already use).

/**
 * Backend Python source roots the running `hermes serve` process has
 * already imported. Deliberately the same four the roadmap card's evidence
 * names — NOT a general watch-everything: apps/desktop and the rest of the
 * repo are covered by Vite HMR / the Electron main-process watcher already.
 */
export const DEV_BACKEND_WATCH_DIRS = ['agent', 'tui_gateway', 'tools', 'hermes_cli'] as const

// Path segments that must never trip staleness even though they live inside
// a watched Python source tree. Keeps routine writes — bytecode caches, test
// artifacts, tool caches — from prompting a restart nobody asked for.
const IGNORED_PATH_SEGMENTS = new Set(['__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.git'])

/**
 * True when a raw fs.watch `filename` (relative to one of
 * DEV_BACKEND_WATCH_DIRS, possibly nested) is a real backend source edit —
 * i.e. one the running `serve` process needs a restart to pick up.
 *
 * Filters out everything routine: `__pycache__`/`.pyc`, test caches, dot
 * directories, and any non-`.py` file (logs, session/memory state under the
 * profile home are never inside these source directories to begin with, but
 * a stray non-Python write here — e.g. an editor swap file — must not count
 * either).
 */
export function isRelevantBackendPythonChange(filename: string | null | undefined): boolean {
  if (!filename) {
    return false
  }

  const normalized = String(filename).replace(/\\/g, '/')
  const segments = normalized.split('/')

  if (segments.some(segment => IGNORED_PATH_SEGMENTS.has(segment))) {
    return false
  }

  return normalized.endsWith('.py')
}

export type DevBackendStaleState = 'fresh' | 'stale' | 'restarting' | 'failed'

export interface DevBackendStaleTracker {
  /** Current state, for the status snapshot IPC handler. */
  state(): DevBackendStaleState
  /**
   * Record a relevant source change. Returns true when this call actually
   * transitioned into `stale` (the caller should broadcast); false when
   * already stale/restarting, so a burst of saves broadcasts once.
   */
  markStale(): boolean
  /**
   * Enter `restarting`. Returns false (no-op) unless the tracker is
   * currently `stale` or `failed` — a restart can only be offered once a
   * change was actually observed, and a second concurrent restart request
   * must not double-fire.
   */
  beginRestart(): boolean
  /** The restart completed and the backend is confirmed back up. */
  restartSucceeded(): void
  /** The restart attempt itself failed (backend never came back). */
  restartFailed(): void
}

/** Fresh, in-memory-only tracker. One per desktop process (module-level singleton in main.ts). */
export function createDevBackendStaleTracker(): DevBackendStaleTracker {
  let state: DevBackendStaleState = 'fresh'

  return {
    state: () => state,
    markStale: () => {
      if (state === 'stale' || state === 'restarting') {
        return false
      }

      state = 'stale'

      return true
    },
    beginRestart: () => {
      if (state !== 'stale' && state !== 'failed') {
        return false
      }

      state = 'restarting'

      return true
    },
    restartSucceeded: () => {
      state = 'fresh'
    },
    restartFailed: () => {
      state = 'failed'
    }
  }
}

/**
 * Whether the renderer should ever render the "Restart backend to apply"
 * affordance. Same shape as devMainBundleStale's `supported` gate
 * (`!IS_PACKAGED && Boolean(DEV_SERVER)`), plus: a remote primary backend
 * isn't this desktop's process to restart, so the affordance would be a lie.
 */
export function shouldSupportDevBackendRestart({
  isPackaged,
  devServer,
  primaryIsRemote
}: {
  isPackaged: boolean
  devServer: string | null | undefined
  primaryIsRemote: boolean
}): boolean {
  return !isPackaged && Boolean(devServer) && !primaryIsRemote
}

export interface PerformDevBackendRestartDeps {
  tracker: DevBackendStaleTracker
  /** Soft teardown of the primary backend child; the renderer re-dials and a fresh child spawns on the next connection request. */
  teardownPrimaryBackend: () => Promise<void>
  /** Broadcast the tracker's new state to every window (called before AND after the teardown so the UI shows "restarting…"). */
  notifyStateChanged: () => void
}

export interface PerformDevBackendRestartResult {
  ok: boolean
  reason?: string
}

/**
 * Restart-in-place: tear down the primary backend child and let the normal
 * soft-rehome path (connection-config-apply's `hermes:connection:applied` →
 * the renderer's softSwitch()) spawn a fresh one and reconnect, restoring
 * connection/profile/session view per the "switching context is a re-home"
 * invariant. Never called automatically — only from the explicit IPC handler
 * behind the user's statusbar click, so an in-flight turn is never torn down
 * out from under the user.
 */
export async function performDevBackendRestart(
  deps: PerformDevBackendRestartDeps
): Promise<PerformDevBackendRestartResult> {
  if (!deps.tracker.beginRestart()) {
    return { ok: false, reason: 'not-stale' }
  }

  deps.notifyStateChanged()

  try {
    await deps.teardownPrimaryBackend()
    deps.tracker.restartSucceeded()
    deps.notifyStateChanged()

    return { ok: true }
  } catch (error) {
    deps.tracker.restartFailed()
    deps.notifyStateChanged()

    return { ok: false, reason: error instanceof Error ? error.message : String(error) }
  }
}
