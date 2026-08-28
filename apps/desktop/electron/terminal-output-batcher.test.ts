import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createTerminalOutputBatcher,
  DEFAULT_HIGH_WATER_MARK_BYTES,
  DEFAULT_LOW_WATER_MARK_BYTES
} from './terminal-output-batcher'

function makeTimers() {
  const pending = new Map<number, () => void>()
  let nextId = 1

  return {
    clearTimeout: (handle: unknown) => {
      pending.delete(handle as number)
    },
    fire() {
      const jobs = [...pending.values()]
      pending.clear()

      for (const job of jobs) {
        job()
      }
    },
    get pendingCount() {
      return pending.size
    },
    setTimeout: (fn: () => void, _ms: number) => {
      const id = nextId++
      pending.set(id, fn)

      return id
    }
  }
}

function makeSends() {
  const sent: string[] = []

  return { sent, send: (data: string) => sent.push(data) }
}

test('multiple push() calls inside one flush window coalesce into a single send', () => {
  const timers = makeTimers()
  const { sent, send } = makeSends()
  const batcher = createTerminalOutputBatcher({ pause: () => {}, resume: () => {}, send, timers })

  batcher.push('a')
  batcher.push('b')
  batcher.push('c')

  // Nothing sent yet — still inside the coalescing window.
  assert.deepEqual(sent, [])
  assert.equal(timers.pendingCount, 1)

  timers.fire()

  // One IPC send for the whole flood, not one per chunk.
  assert.deepEqual(sent, ['abc'])
})

test('a second flush window after the first produces a second, separate send', () => {
  const timers = makeTimers()
  const { sent, send } = makeSends()
  const batcher = createTerminalOutputBatcher({ pause: () => {}, resume: () => {}, send, timers })

  batcher.push('a')
  timers.fire()
  batcher.push('b')
  timers.fire()

  assert.deepEqual(sent, ['a', 'b'])
})

test('crossing the flush threshold flushes immediately without waiting for the timer', () => {
  const timers = makeTimers()
  const { sent, send } = makeSends()

  const batcher = createTerminalOutputBatcher({
    flushThresholdBytes: 8,
    pause: () => {},
    resume: () => {},
    send,
    timers
  })

  batcher.push('01234567')

  assert.deepEqual(sent, ['01234567'])
  // No dangling timer left behind by the threshold-triggered flush.
  assert.equal(timers.pendingCount, 0)
})

test('push() with empty data is a no-op (no send, no timer armed)', () => {
  const timers = makeTimers()
  const { sent, send } = makeSends()
  const batcher = createTerminalOutputBatcher({ pause: () => {}, resume: () => {}, send, timers })

  batcher.push('')

  assert.equal(timers.pendingCount, 0)
  timers.fire()
  assert.deepEqual(sent, [])
})

test('flush() forces out buffered data on demand (e.g. before an exit event)', () => {
  const timers = makeTimers()
  const { sent, send } = makeSends()
  const batcher = createTerminalOutputBatcher({ pause: () => {}, resume: () => {}, send, timers })

  batcher.push('tail output')
  batcher.flush()

  assert.deepEqual(sent, ['tail output'])
  assert.equal(timers.pendingCount, 0)

  // A second flush with nothing buffered must not send an empty chunk.
  batcher.flush()
  assert.deepEqual(sent, ['tail output'])
})

test('pauses the pty once unacked bytes cross the high-water mark', () => {
  const timers = makeTimers()
  const { send } = makeSends()
  let paused = false

  const batcher = createTerminalOutputBatcher({
    highWaterMarkBytes: 10,
    pause: () => {
      paused = true
    },
    resume: () => {},
    send,
    timers
  })

  batcher.push('12345')
  batcher.flush()
  assert.equal(paused, false)
  assert.equal(batcher.isPaused(), false)

  batcher.push('67890')
  batcher.flush()

  assert.equal(paused, true)
  assert.equal(batcher.isPaused(), true)
})

test('resumes once acks bring unacked bytes at/below the low-water mark, not before', () => {
  const timers = makeTimers()
  const { send } = makeSends()
  let resumed = false

  const batcher = createTerminalOutputBatcher({
    highWaterMarkBytes: 10,
    lowWaterMarkBytes: 4,
    pause: () => {},
    resume: () => {
      resumed = true
    },
    send,
    timers
  })

  batcher.push('1234567890')
  batcher.flush()
  assert.equal(batcher.isPaused(), true)

  // Acking down to 5 (above the low-water mark of 4) must not resume yet.
  batcher.ack(5)
  assert.equal(resumed, false)
  assert.equal(batcher.isPaused(), true)

  // Acking the rest crosses the low-water mark.
  batcher.ack(1)
  assert.equal(resumed, true)
  assert.equal(batcher.isPaused(), false)
})

test('ack is robust to acking more than outstanding (renderer reload mid-stream) — clamps, never throws', () => {
  const timers = makeTimers()
  const { send } = makeSends()
  const batcher = createTerminalOutputBatcher({ pause: () => {}, resume: () => {}, send, timers })

  assert.doesNotThrow(() => batcher.ack(999))
  assert.doesNotThrow(() => batcher.ack(0))
  assert.doesNotThrow(() => batcher.ack(-5))
})

test('dispose() clears a pending flush timer without sending', () => {
  const timers = makeTimers()
  const { sent, send } = makeSends()
  const batcher = createTerminalOutputBatcher({ pause: () => {}, resume: () => {}, send, timers })

  batcher.push('never flushed')
  batcher.dispose()

  assert.equal(timers.pendingCount, 0)
  timers.fire()
  assert.deepEqual(sent, [])
})

test('defaults export sane VS Code-style watermarks (low below high)', () => {
  assert.ok(DEFAULT_LOW_WATER_MARK_BYTES < DEFAULT_HIGH_WATER_MARK_BYTES)
  assert.equal(DEFAULT_HIGH_WATER_MARK_BYTES, 1024 * 1024)
})
