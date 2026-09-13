import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { guardWatcherErrors, PreviewWatcherRegistry } from './preview-watchers'

// A minimal stand-in for fs.FSWatcher: an EventEmitter with .close(). The
// real crash bug this file exists to prevent only reproduces when 'error' is
// actually emitted through Node's EventEmitter machinery (an unhandled
// 'error' event throws synchronously), so tests emit through a real
// EventEmitter rather than hand-rolling a fake with an on()/close() shape.
function fakeWatcher() {
  const emitter = new EventEmitter() as EventEmitter & { closed: boolean; close(): void }
  emitter.closed = false
  emitter.close = () => {
    emitter.closed = true
  }

  return emitter
}

test('guardWatcherErrors closes the watcher and forwards the error, never throwing', () => {
  const watcher = fakeWatcher()
  const seen: unknown[] = []

  guardWatcherErrors(watcher, error => seen.push(error))

  // Without guardWatcherErrors, this emit would throw (Node's default
  // behavior for an unhandled 'error' on an EventEmitter) — which is exactly
  // the Windows EPERM-on-delete crash this hardening fixes.
  assert.doesNotThrow(() => watcher.emit('error', new Error('EPERM: operation not permitted, watch')))

  assert.equal(watcher.closed, true)
  assert.equal(seen.length, 1)
  assert.equal((seen[0] as Error).message, 'EPERM: operation not permitted, watch')
})

test('guardWatcherErrors tolerates close() throwing (already-closed watcher)', () => {
  const watcher = fakeWatcher()
  watcher.close = () => {
    throw new Error('already closed')
  }
  let handlerRan = false

  guardWatcherErrors(watcher, () => {
    handlerRan = true
  })

  assert.doesNotThrow(() => watcher.emit('error', new Error('EPERM')))
  assert.equal(handlerRan, true)
})

test('registry.stop() closes and forgets a watcher, and is idempotent', () => {
  const registry = new PreviewWatcherRegistry()
  let closed = 0

  registry.register('w1', { close: () => (closed += 1) })
  assert.equal(registry.has('w1'), true)

  assert.equal(registry.stop('w1'), true)
  assert.equal(closed, 1)
  assert.equal(registry.has('w1'), false)

  // Second stop (e.g. explicit stop IPC racing an error-path close) must be
  // a safe no-op, not a double-close or a throw.
  assert.equal(registry.stop('w1'), false)
  assert.equal(closed, 1)
})

test('registry.stop() survives close() throwing', () => {
  const registry = new PreviewWatcherRegistry()
  registry.register('w1', {
    close: () => {
      throw new Error('boom')
    }
  })

  assert.doesNotThrow(() => registry.stop('w1'))
  assert.equal(registry.has('w1'), false)
})

test('stopForWebContents closes only that webContents\' watchers — reload does not orphan the rest', () => {
  const registry = new PreviewWatcherRegistry()
  const closedIds: string[] = []
  const closer = (id: string) => () => closedIds.push(id)

  registry.register('a', { close: closer('a') }, 1)
  registry.register('b', { close: closer('b') }, 1)
  registry.register('c', { close: closer('c') }, 2)

  registry.stopForWebContents(1)

  assert.deepEqual(closedIds.sort(), ['a', 'b'])
  assert.equal(registry.has('a'), false)
  assert.equal(registry.has('b'), false)
  assert.equal(registry.has('c'), true)
  assert.equal(registry.size, 1)
})

test('watchers with no owner (null webContentsId) are untouched by owner cleanup', () => {
  const registry = new PreviewWatcherRegistry()
  registry.register('unowned', { close: () => {} }, null)

  registry.stopForWebContents(1)

  assert.equal(registry.has('unowned'), true)
})

test('stopAll closes every watcher regardless of owner', () => {
  const registry = new PreviewWatcherRegistry()
  registry.register('a', { close: () => {} }, 1)
  registry.register('b', { close: () => {} }, 2)
  registry.register('c', { close: () => {} }, null)

  registry.stopAll()

  assert.equal(registry.size, 0)
})

test('simulated renderer reload: watcher count is stable, not growing, across repeated re-homes', () => {
  const registry = new PreviewWatcherRegistry()
  const WEB_CONTENTS_ID = 7

  for (let reload = 0; reload < 5; reload += 1) {
    // Each "page" opens a couple of preview/directory watches under the same
    // webContents id (Electron keeps the id across a same-window reload).
    registry.register(`preview-${reload}`, { close: () => {} }, WEB_CONTENTS_ID)
    registry.register(`dir-${reload}`, { close: () => {} }, WEB_CONTENTS_ID)

    // The owner-cleanup hook (did-start-navigation / destroyed) fires before
    // the next page's watches are registered.
    registry.stopForWebContents(WEB_CONTENTS_ID)
  }

  assert.equal(registry.size, 0)
})
