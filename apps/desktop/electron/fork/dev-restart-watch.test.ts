/**
 * Behavior contract for the fork dev-restart-watch module: the dev-only
 * staleness watchers and the hermes:dev:* IPC handlers it registers. The
 * pure filtering/state logic is covered by ../dev-backend-watch.test.ts;
 * this file proves the wiring — handler registration, packaged/dev gating,
 * the sentinel-exit restart contract, and watcher cleanup on before-quit —
 * against fake app/ipcMain/fs deps (no Electron runtime).
 */

import { EventEmitter } from 'node:events'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, expect, test } from 'vitest'

import { DEV_RESTART_EXIT_CODE, registerDevRestartWatch } from './dev-restart-watch'

interface FakeHarness {
  handlers: Map<string, (...args: any[]) => unknown>
  quitListeners: Array<() => void>
  exits: number[]
  relaunches: number
  watch: ReturnType<typeof registerDevRestartWatch>
  watchedTargets: string[]
  openWatchers: FakeWatcher[]
  teardowns: number
}

class FakeWatcher extends EventEmitter {
  closed = false

  close() {
    this.closed = true
  }
}

let tmpDir: string

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-dev-restart-watch-'))
})

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true })
})

function harness(overrides: { isPackaged?: boolean; devServer?: string; env?: NodeJS.ProcessEnv } = {}): FakeHarness {
  const state: FakeHarness = {
    handlers: new Map(),
    quitListeners: [],
    exits: [],
    relaunches: 0,
    watch: null as any,
    watchedTargets: [],
    openWatchers: [],
    teardowns: 0
  }

  // A real main bundle on disk so watchDevMainBundle finds a signature.
  fs.mkdirSync(path.join(tmpDir, 'dist'), { recursive: true })
  fs.writeFileSync(path.join(tmpDir, 'dist', 'electron-main.mjs'), 'bundle')

  state.watch = registerDevRestartWatch({
    appRoot: tmpDir,
    isPackaged: overrides.isPackaged ?? false,
    devServer: overrides.devServer ?? 'http://127.0.0.1:5174',
    sourceRepoRoot: path.join(tmpDir, 'repo'),
    env: overrides.env ?? {},
    app: {
      on: (_event, listener) => state.quitListeners.push(listener),
      exit: code => state.exits.push(code ?? 0),
      relaunch: () => {
        state.relaunches += 1
      }
    },
    ipcMain: {
      handle: (channel, listener) => state.handlers.set(channel, listener)
    },
    getAllWindows: () => [],
    isHermesSourceRoot: () => false,
    directoryExists: dir => fs.existsSync(dir),
    primaryBackendIsRemote: () => false,
    teardownPrimaryBackend: async () => {
      state.teardowns += 1
    },
    fsApi: {
      statSync: fs.statSync,
      watch: ((target: string) => {
        state.watchedTargets.push(target)

        const watcher = new FakeWatcher()

        state.openWatchers.push(watcher)

        return watcher as unknown as fs.FSWatcher
      }) as typeof fs.watch
    }
  })

  return state
}

test('registers all four hermes:dev IPC handlers', () => {
  const h = harness()

  for (const channel of [
    'hermes:dev:main-bundle-stale',
    'hermes:dev:restart',
    'hermes:dev:backend-stale',
    'hermes:dev:backend-restart'
  ]) {
    expect(h.handlers.has(channel), channel).toBe(true)
  }
})

test('main-bundle-stale reports supported only for an unpackaged dev-server build', async () => {
  const dev = harness()

  expect(await dev.handlers.get('hermes:dev:main-bundle-stale')!()).toEqual({ stale: false, supported: true })

  const packaged = harness({ isPackaged: true })

  expect(await packaged.handlers.get('hermes:dev:main-bundle-stale')!()).toEqual({ stale: false, supported: false })
})

test('watchDevMainBundle watches only in dev; packaged builds never watch', () => {
  const dev = harness()

  dev.watch.watchDevMainBundle()
  expect(dev.watchedTargets).toEqual([path.join(tmpDir, 'dist', 'electron-main.mjs')])

  const packaged = harness({ isPackaged: true })

  packaged.watch.watchDevMainBundle()
  expect(packaged.watchedTargets).toEqual([])
})

test('hermes:dev:restart exits with the sentinel code under the dev watcher, relaunches otherwise', async () => {
  const supervised = harness({ env: { HERMES_DEV_WATCH: '1' } })

  expect(await supervised.handlers.get('hermes:dev:restart')!()).toEqual({ ok: true })
  expect(supervised.exits).toEqual([DEV_RESTART_EXIT_CODE])
  expect(supervised.relaunches).toBe(0)

  const bare = harness()

  expect(await bare.handlers.get('hermes:dev:restart')!()).toEqual({ ok: true })
  expect(bare.exits).toEqual([0])
  expect(bare.relaunches).toBe(1)

  const packaged = harness({ isPackaged: true })

  expect(await packaged.handlers.get('hermes:dev:restart')!()).toEqual({ ok: false, reason: 'not-a-dev-build' })
  expect(packaged.exits).toEqual([])
})

test('hermes:dev:backend-restart refuses packaged builds and refuses when nothing is stale', async () => {
  const packaged = harness({ isPackaged: true })

  expect(await packaged.handlers.get('hermes:dev:backend-restart')!()).toEqual({
    ok: false,
    reason: 'not-a-dev-build'
  })
  expect(packaged.teardowns).toBe(0)

  // Offer, don't act: with no stale backend marked, the restart is a no-op
  // refusal and the primary backend is never torn down.
  const dev = harness()
  const result = (await dev.handlers.get('hermes:dev:backend-restart')!()) as { ok: boolean; reason?: string }

  expect(result).toEqual({ ok: false, reason: 'not-stale' })
  expect(dev.teardowns).toBe(0)
})

test('before-quit closes every open bundle watcher', () => {
  const h = harness()

  h.watch.watchDevMainBundle()
  expect(h.openWatchers.length).toBeGreaterThan(0)

  for (const listener of h.quitListeners) {
    listener()
  }

  expect(h.openWatchers.every(w => w.closed)).toBe(true)
})
