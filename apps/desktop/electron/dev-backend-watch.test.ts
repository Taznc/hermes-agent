import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createDevBackendStaleTracker,
  DEV_BACKEND_WATCH_DIRS,
  isRelevantBackendPythonChange,
  performDevBackendRestart,
  shouldSupportDevBackendRestart
} from './dev-backend-watch'

test('DEV_BACKEND_WATCH_DIRS matches the roadmap card evidence exactly', () => {
  assert.deepEqual([...DEV_BACKEND_WATCH_DIRS], ['agent', 'tui_gateway', 'tools', 'hermes_cli'])
})

test('isRelevantBackendPythonChange accepts a real .py edit at the root', () => {
  assert.equal(isRelevantBackendPythonChange('background_review.py'), true)
})

test('isRelevantBackendPythonChange accepts a nested .py edit', () => {
  assert.equal(isRelevantBackendPythonChange('computer_use/cua_backend.py'), true)
})

test('isRelevantBackendPythonChange rejects __pycache__ writes', () => {
  assert.equal(isRelevantBackendPythonChange('__pycache__/server.cpython-312.pyc'), false)
})

test('isRelevantBackendPythonChange rejects a bare .pyc with no __pycache__ segment', () => {
  assert.equal(isRelevantBackendPythonChange('server.pyc'), false)
})

test('isRelevantBackendPythonChange rejects nested __pycache__ writes', () => {
  assert.equal(isRelevantBackendPythonChange('tui_gateway/__pycache__/server.cpython-312.pyc'), false)
})

test('isRelevantBackendPythonChange rejects test/tool caches', () => {
  assert.equal(isRelevantBackendPythonChange('.pytest_cache/v/cache/lastfailed'), false)
  assert.equal(isRelevantBackendPythonChange('.mypy_cache/3.12/tools/computer_use.data.json'), false)
  assert.equal(isRelevantBackendPythonChange('.ruff_cache/0.5.0/1'), false)
})

test('isRelevantBackendPythonChange rejects non-python files (logs, swap files)', () => {
  assert.equal(isRelevantBackendPythonChange('server.log'), false)
  assert.equal(isRelevantBackendPythonChange('server.py.swp'), false)
})

test('isRelevantBackendPythonChange rejects a null/empty filename', () => {
  assert.equal(isRelevantBackendPythonChange(null), false)
  assert.equal(isRelevantBackendPythonChange(''), false)
  assert.equal(isRelevantBackendPythonChange(undefined), false)
})

test('isRelevantBackendPythonChange normalizes Windows path separators', () => {
  assert.equal(isRelevantBackendPythonChange('computer_use\\cua_backend.py'), true)
  assert.equal(isRelevantBackendPythonChange('__pycache__\\server.cpython-312.pyc'), false)
})

// ── state machine: stale / offered / restarted / failed ────────────────────

test('tracker starts fresh', () => {
  const tracker = createDevBackendStaleTracker()
  assert.equal(tracker.state(), 'fresh')
})

test('markStale transitions fresh -> stale and reports the transition', () => {
  const tracker = createDevBackendStaleTracker()
  assert.equal(tracker.markStale(), true)
  assert.equal(tracker.state(), 'stale')
})

test('markStale is idempotent while already stale (no re-broadcast on a save burst)', () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()
  assert.equal(tracker.markStale(), false)
  assert.equal(tracker.state(), 'stale')
})

test('beginRestart is a no-op while fresh (nothing to restart for)', () => {
  const tracker = createDevBackendStaleTracker()
  assert.equal(tracker.beginRestart(), false)
  assert.equal(tracker.state(), 'fresh')
})

test('stale -> restarting -> fresh on success', () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()
  assert.equal(tracker.beginRestart(), true)
  assert.equal(tracker.state(), 'restarting')
  tracker.restartSucceeded()
  assert.equal(tracker.state(), 'fresh')
})

test('stale -> restarting -> failed, and failed can be retried', () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()
  tracker.beginRestart()
  tracker.restartFailed()
  assert.equal(tracker.state(), 'failed')
  assert.equal(tracker.beginRestart(), true)
  assert.equal(tracker.state(), 'restarting')
})

test('beginRestart rejects a concurrent second call while already restarting', () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()
  tracker.beginRestart()
  assert.equal(tracker.beginRestart(), false)
  assert.equal(tracker.state(), 'restarting')
})

// ── support gate: dev-only, never in a packaged build, never against a remote primary ──

test('shouldSupportDevBackendRestart is true in a normal local dev run', () => {
  assert.equal(
    shouldSupportDevBackendRestart({ isPackaged: false, devServer: 'http://127.0.0.1:5174', primaryIsRemote: false }),
    true
  )
})

test('shouldSupportDevBackendRestart is false in a packaged build', () => {
  assert.equal(
    shouldSupportDevBackendRestart({ isPackaged: true, devServer: 'http://127.0.0.1:5174', primaryIsRemote: false }),
    false
  )
})

test('shouldSupportDevBackendRestart is false without a dev server', () => {
  assert.equal(shouldSupportDevBackendRestart({ isPackaged: false, devServer: null, primaryIsRemote: false }), false)
})

test('shouldSupportDevBackendRestart is false when the primary backend is remote', () => {
  assert.equal(
    shouldSupportDevBackendRestart({ isPackaged: false, devServer: 'http://127.0.0.1:5174', primaryIsRemote: true }),
    false
  )
})

// ── performDevBackendRestart: offer-only orchestration ──────────────────────

test('performDevBackendRestart refuses when the tracker is not stale (fresh)', async () => {
  const tracker = createDevBackendStaleTracker()
  let teardownCalls = 0
  let notifyCalls = 0

  const result = await performDevBackendRestart({
    tracker,
    teardownPrimaryBackend: async () => {
      teardownCalls += 1
    },
    notifyStateChanged: () => {
      notifyCalls += 1
    }
  })

  assert.deepEqual(result, { ok: false, reason: 'not-stale' })
  assert.equal(teardownCalls, 0, 'must never tear down the backend when nothing is stale')
  assert.equal(notifyCalls, 0)
  assert.equal(tracker.state(), 'fresh')
})

test('performDevBackendRestart tears down and restores to fresh on success, notifying twice', async () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()
  const notifyStates: string[] = []
  let teardownCalls = 0

  const result = await performDevBackendRestart({
    tracker,
    teardownPrimaryBackend: async () => {
      teardownCalls += 1
    },
    notifyStateChanged: () => {
      notifyStates.push(tracker.state())
    }
  })

  assert.deepEqual(result, { ok: true })
  assert.equal(teardownCalls, 1)
  assert.equal(tracker.state(), 'fresh')
  // Restarting is broadcast before the teardown so the UI can show a spinner,
  // then fresh is broadcast after — both states must reach the renderer.
  assert.deepEqual(notifyStates, ['restarting', 'fresh'])
})

test('performDevBackendRestart marks failed and reports the error when teardown throws', async () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()
  const notifyStates: string[] = []

  const result = await performDevBackendRestart({
    tracker,
    teardownPrimaryBackend: async () => {
      throw new Error('backend refused to die')
    },
    notifyStateChanged: () => {
      notifyStates.push(tracker.state())
    }
  })

  assert.deepEqual(result, { ok: false, reason: 'backend refused to die' })
  assert.equal(tracker.state(), 'failed')
  assert.deepEqual(notifyStates, ['restarting', 'failed'])
})

test('performDevBackendRestart is offerable again after a failure (retry)', async () => {
  const tracker = createDevBackendStaleTracker()
  tracker.markStale()

  await performDevBackendRestart({
    tracker,
    teardownPrimaryBackend: async () => {
      throw new Error('nope')
    },
    notifyStateChanged: () => {}
  })

  assert.equal(tracker.state(), 'failed')

  const result = await performDevBackendRestart({
    tracker,
    teardownPrimaryBackend: async () => {},
    notifyStateChanged: () => {}
  })

  assert.deepEqual(result, { ok: true })
  assert.equal(tracker.state(), 'fresh')
})

// ── never-automatic invariant ────────────────────────────────────────────────

test('markStale alone never invokes a restart (offer, not act)', () => {
  const tracker = createDevBackendStaleTracker()
  // markStale has no teardown dependency at all -- there is no code path
  // from "file changed" to "backend killed" without an explicit beginRestart
  // call, which only main.ts's IPC handler (the user's click) makes.
  tracker.markStale()
  assert.equal(tracker.state(), 'stale')
})
