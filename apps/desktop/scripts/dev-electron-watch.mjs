#!/usr/bin/env node
// dev-electron-watch.mjs — rebuild the Electron main/preload bundles on every
// save, WITHOUT restarting the app.
//
// Why this exists
// ---------------
// `npm run dev` bundles electron/ ONCE and then launches. Vite hot-reloads the
// renderer, but anything under electron/ (main.ts, preload.ts, IPC handlers)
// lives in a prebuilt bundle — so a main-process edit is invisible until you
// manually rebuild and relaunch. That silently tests stale code, which is the
// most common false "I fixed it" in this app.
//
// Electron cannot hot-swap an already-evaluated main process, so applying a
// main-process edit fundamentally requires a restart. Rather than killing the
// window mid-task, this only rebuilds; the running app notices the new bundle
// on disk and offers a "Restart to apply" affordance the user clicks when they
// are ready. The renderer keeps its own Vite HMR and never needs a restart.
//
// Usage:  node scripts/dev-electron-watch.mjs
//         (wired up as `npm run dev:watch`)
import { context } from 'esbuild'
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const distDir = resolve(root, 'dist')
mkdirSync(distDir, { recursive: true })

const DEV_SERVER = process.env.HERMES_DESKTOP_DEV_SERVER || 'http://127.0.0.1:5174'
const external = ['electron', 'node-pty', 'get-windows', 'fs']

let child = null
let shuttingDown = false
// esbuild fires onEnd for the initial build too; only saves after launch are
// "changes the running app hasn't got yet".
let primed = false

function log(msg) {
  console.log(`\x1b[36m[dev:watch]\x1b[0m ${msg}`)
}

// Report failures instead of writing a broken bundle silently — a rebuild error
// must never look like a successful reload.
const notifyPlugin = {
  name: 'notify-rebuild',
  setup(build) {
    build.onEnd(result => {
      if (result.errors.length > 0) {
        log(`\x1b[31mbuild failed\x1b[0m (${result.errors.length} error(s)) — the app keeps the last good bundle`)

        return
      }

      if (primed) {
        log('\x1b[33mmain-process bundle rebuilt\x1b[0m — click "Restart to apply" in the app')
      }
    })
  }
}

const shared = {
  bundle: true,
  external,
  logLevel: 'silent',
  platform: 'node',
  plugins: [notifyPlugin],
  target: 'node20'
}

const mainCtx = await context({
  ...shared,
  banner: {
    js: "import { createRequire } from 'module'; const require = createRequire(import.meta.url);"
  },
  entryPoints: [resolve(root, 'electron/main.ts')],
  format: 'esm',
  outfile: resolve(distDir, 'electron-main.mjs')
})

const preloadCtx = await context({
  ...shared,
  entryPoints: [resolve(root, 'electron/preload.ts')],
  format: 'cjs',
  outfile: resolve(distDir, 'electron-preload.js')
})

await mainCtx.watch()
await preloadCtx.watch()

primed = true
log(`watching electron/ — renderer hot-reloads, main-process edits offer a restart (renderer: ${DEV_SERVER})`)

child = spawn(resolve(root, 'node_modules/.bin/electron'), ['.'], {
  cwd: root,
  stdio: 'inherit',
  env: { ...process.env, HERMES_DESKTOP_DEV_SERVER: DEV_SERVER, XCURSOR_SIZE: '24' }
})

// The app relaunches itself in place (app.relaunch), so a normal exit here means
// the user quit for real — tear the watcher down with it.
child.on('exit', code => {
  if (!shuttingDown) {
    log(`electron exited (code ${code}) — stopping watcher`)
    void stop(code ?? 0)
  }
})

async function stop(code = 0) {
  if (shuttingDown) {
    return
  }

  shuttingDown = true
  await mainCtx.dispose().catch(() => {})
  await preloadCtx.dispose().catch(() => {})

  if (child && child.exitCode === null) {
    child.kill('SIGTERM')
  }

  process.exit(code)
}

process.on('SIGINT', () => void stop(0))
process.on('SIGTERM', () => void stop(0))
