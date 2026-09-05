// Fork-owned: dev-only staleness watchers + explicit restart affordances.
//
// ── Dev: main-process bundle staleness ──────────────────────────────────────
// The renderer hot-reloads through Vite, but Electron cannot hot-swap an
// already-evaluated main process — an electron/ edit only lands on restart.
// Rather than restarting under the user (which would destroy whatever they were
// mid-way through), watch the built bundle and let the renderer offer an
// explicit "Restart to apply". Dev-only: a packaged build never watches and the
// renderer's affordance stays hidden, so release users see nothing.
//
// ── Dev: backend Python source staleness (Phase 2.9) ────────────────────────
// Sibling to the main-process bundle watcher above and to dev-electron-watch.mjs:
// the renderer hot-reloads via Vite and Electron's main process gets the
// "Restart to apply" affordance above, but nothing previously covered a
// backend Python edit under a running `hermes serve` child — the process keeps
// serving pre-edit code while both other layers look current. Reuses the exact
// same "watch, mark stale, let the renderer offer a restart" shape rather than
// inventing a new one. See dev-backend-watch.ts for the pure filtering/state
// logic this wires up.

import fs from 'node:fs'
import path from 'node:path'

import {
  createDevBackendStaleTracker,
  DEV_BACKEND_WATCH_DIRS,
  isRelevantBackendPythonChange,
  performDevBackendRestart,
  shouldSupportDevBackendRestart
} from '../dev-backend-watch'
import { guardWatcherErrors } from '../preview-watchers'

// Sentinel exit code meaning "the dev watcher should respawn me", as opposed to
// a real quit. Must match the value in scripts/dev-electron-watch.mjs.
export const DEV_RESTART_EXIT_CODE = 86

interface SendableWebContents {
  webContents?: { isDestroyed(): boolean; send(channel: string, payload: unknown): void } | null
}

export interface DevRestartWatchDeps {
  appRoot: string
  isPackaged: boolean
  devServer: string | undefined
  sourceRepoRoot: string
  env: NodeJS.ProcessEnv
  app: {
    on(event: 'before-quit', listener: () => void): unknown
    exit(code?: number): void
    relaunch(): void
  }
  ipcMain: { handle(channel: string, listener: (...args: any[]) => unknown): void }
  getAllWindows(): SendableWebContents[]
  isHermesSourceRoot(root: string): boolean
  directoryExists(dir: string): boolean
  primaryBackendIsRemote(): boolean
  /** Soft primary teardown + "connection applied" signal (see the anchor). */
  teardownPrimaryBackend(): Promise<void>
  fsApi?: Pick<typeof fs, 'statSync' | 'watch'>
}

export interface DevRestartWatch {
  watchDevMainBundle(): void
  watchDevBackendPython(): void
}

export function registerDevRestartWatch(deps: DevRestartWatchDeps): DevRestartWatch {
  const fsApi = deps.fsApi || fs
  const DEV_MAIN_BUNDLE = path.join(deps.appRoot, 'dist', 'electron-main.mjs')
  const DEV_PRELOAD_BUNDLE = path.join(deps.appRoot, 'dist', 'electron-preload.js')

  let devMainBundleStale = false
  let devBundleWatchers: fs.FSWatcher[] = []

  function broadcastDevBundleStale() {
    for (const win of deps.getAllWindows()) {
      const { webContents } = win

      if (webContents && !webContents.isDestroyed()) {
        webContents.send('hermes:dev:main-bundle-stale', { stale: devMainBundleStale })
      }
    }
  }

  function watchDevMainBundle() {
    if (deps.isPackaged || !deps.devServer) {
      return
    }

    // Signature at boot: anything different later is a rebuild we are not running.
    const signature = (target: string) => {
      try {
        const stat = fsApi.statSync(target)

        return `${stat.size}:${stat.mtimeMs}`
      } catch {
        return ''
      }
    }

    for (const target of [DEV_MAIN_BUNDLE, DEV_PRELOAD_BUNDLE]) {
      const original = signature(target)

      if (!original) {
        continue
      }

      try {
        // Debounced: esbuild writes in bursts, and a rebuild can touch both
        // bundles. Once stale we stay stale — only a restart clears it.
        let timer: NodeJS.Timeout | null = null

        const watcher = fsApi.watch(target, () => {
          if (devMainBundleStale) {
            return
          }

          if (timer) {
            clearTimeout(timer)
          }

          timer = setTimeout(() => {
            if (!devMainBundleStale && signature(target) !== original) {
              devMainBundleStale = true
              console.log('[hermes] main-process bundle changed on disk — restart to apply')
              broadcastDevBundleStale()
            }
          }, 150)
        })

        // The try/catch around fs.watch() only guards the SYNCHRONOUS call —
        // it does not cover the watcher's own async 'error' event (EPERM on
        // Windows if the dist/ dir is deleted/rebuilt mid-watch, ENOENT if a
        // build tool briefly unlinks-then-recreates the file). Unhandled,
        // that throws and crashes the main process same as any other
        // fs.watch() site; dev-only doesn't make it safe to skip.
        guardWatcherErrors(watcher, error => {
          if (timer) {
            clearTimeout(timer)
            timer = null
          }

          devBundleWatchers = devBundleWatchers.filter(w => w !== watcher)
          console.log(
            `[hermes] dev bundle watcher error on ${target}: ${error instanceof Error ? error.message : error}`
          )
        })

        devBundleWatchers.push(watcher)
      } catch {
        // Watching is a convenience; a platform that refuses it must not break dev.
      }
    }
  }

  deps.app.on('before-quit', () => {
    for (const watcher of devBundleWatchers) {
      try {
        watcher.close()
      } catch {
        void 0
      }
    }

    devBundleWatchers = []
  })

  deps.ipcMain.handle('hermes:dev:main-bundle-stale', async () => ({
    stale: devMainBundleStale,
    // The renderer must not render a restart affordance in a packaged build.
    supported: !deps.isPackaged && Boolean(deps.devServer)
  }))

  deps.ipcMain.handle('hermes:dev:restart', async () => {
    if (deps.isPackaged) {
      return { ok: false, reason: 'not-a-dev-build' }
    }

    // Exit with a sentinel code and let the DEV WATCHER respawn us.
    //
    // app.relaunch() is wrong here: it exits 0, which is indistinguishable from a
    // real quit, so the watcher tears itself down (and `concurrently -k` kills
    // Vite with it) while the relaunched window loads a dev server that no longer
    // exists — a permanent blank screen. Handing the restart to the supervisor
    // that owns the process keeps Vite up and the new window attached.
    //
    // Without a watcher (plain `npm run dev`), nothing respawns us, so fall back
    // to relaunch there.
    if (deps.env.HERMES_DEV_WATCH === '1') {
      deps.app.exit(DEV_RESTART_EXIT_CODE)

      return { ok: true }
    }

    deps.app.relaunch()
    deps.app.exit(0)

    return { ok: true }
  })

  const devBackendStaleTracker = createDevBackendStaleTracker()
  let devBackendPythonWatchers: fs.FSWatcher[] = []

  function broadcastDevBackendStale() {
    const stale = devBackendStaleTracker.state()

    for (const win of deps.getAllWindows()) {
      const { webContents } = win

      if (webContents && !webContents.isDestroyed()) {
        webContents.send('hermes:dev:backend-stale', { state: stale })
      }
    }
  }

  // Only meaningful when the desktop actually spawns the backend FROM this
  // source checkout (dev, local primary) — same precondition the main-process
  // bundle watcher and the dev-source backend resolution branch share.
  // A packaged build, or a desktop pointed at a remote/pool backend, has no
  // local `serve` process whose staleness this could describe.
  function watchDevBackendPython() {
    if (deps.isPackaged || !deps.devServer || !deps.isHermesSourceRoot(deps.sourceRepoRoot)) {
      return
    }

    for (const dir of DEV_BACKEND_WATCH_DIRS) {
      const target = path.join(deps.sourceRepoRoot, dir)

      if (!deps.directoryExists(target)) {
        continue
      }

      try {
        const watcher = fsApi.watch(target, { recursive: true }, (_eventType, filename) => {
          if (!isRelevantBackendPythonChange(filename ? String(filename) : null)) {
            return
          }

          if (devBackendStaleTracker.markStale()) {
            // Log-message text only (dev-only console.log), not a real fs
            // join — no functional effect from the missing separator
            // normalization on any OS.
            console.log(
              `[hermes] backend Python source changed on disk (${dir}/${filename}) — restart backend to apply` // windows-footgun: ok — dev-only console.log text, not an fs path
            )
            broadcastDevBackendStale()
          }
        })

        devBackendPythonWatchers.push(watcher)
      } catch (error) {
        // Recursive fs.watch is unsupported on some Linux configurations
        // (inotify-backed, no native recursive support pre-Node 22-on-Linux
        // parity). Watching is a convenience; a platform that refuses it must
        // not break dev — the affordance simply never lights up there.
        console.warn(`[hermes] backend Python watch unavailable for ${dir}: ${(error as any)?.message || error}`)
      }
    }
  }

  deps.app.on('before-quit', () => {
    for (const watcher of devBackendPythonWatchers) {
      try {
        watcher.close()
      } catch {
        void 0
      }
    }

    devBackendPythonWatchers = []
  })

  deps.ipcMain.handle('hermes:dev:backend-stale', async () => ({
    state: devBackendStaleTracker.state(),
    // The renderer must never render this in a packaged build, and a remote
    // primary backend isn't this process's to restart.
    supported: shouldSupportDevBackendRestart({
      isPackaged: deps.isPackaged,
      devServer: deps.devServer,
      primaryIsRemote: deps.primaryBackendIsRemote()
    })
  }))

  deps.ipcMain.handle('hermes:dev:backend-restart', async () => {
    if (deps.isPackaged) {
      return { ok: false, reason: 'not-a-dev-build' }
    }

    // Offer, don't act: this handler only ever runs in response to the
    // renderer's explicit IPC call, itself only reachable from the user
    // clicking the statusbar affordance — never from the watcher above, which
    // only marks state. Restart-in-place reuses the same soft primary teardown
    // + "connection applied" signal connection-config/profile-switch already
    // use (rehomePrimaryConnection / teardownPrimaryBackendAndWait +
    // sendConnectionApplied), so the renderer's existing softSwitch() listener
    // (desktop.onConnectionApplied) re-dials and restores connection, active
    // profile, and session view — no bespoke re-home logic needed here.
    return performDevBackendRestart({
      tracker: devBackendStaleTracker,
      teardownPrimaryBackend: deps.teardownPrimaryBackend,
      notifyStateChanged: broadcastDevBackendStale
    })
  })

  return { watchDevMainBundle, watchDevBackendPython }
}
