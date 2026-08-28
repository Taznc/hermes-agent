// PTY output batching + ack-based flow control for the embedded terminal.
//
// node-pty's onData fires once per OS-level read — during a build or `cat` of
// a large file that's thousands of tiny chunks a second, each previously
// forwarded as its own webContents.send. Every send is a structured-clone +
// IPC round trip that wakes the renderer; at that rate the renderer can't
// keep up, Electron's IPC channel queues unboundedly (there is no built-in
// backpressure), and the whole app stutters while memory balloons. This is
// the exact problem VS Code's ptyHost protocol solves with batched sends and
// an ack-driven pause/resume, and the shape here mirrors it:
//
//  - Output is coalesced into one send per short timer window (or sooner if
//    a size threshold is crossed), so a flood becomes O(flushes) IPC
//    messages instead of O(chunks).
//  - Bytes sent but not yet acked by the renderer are tracked; once that
//    backlog crosses a high-water mark the pty is paused (node-pty's own
//    pause()/resume(), which stops the OS read side) until the renderer acks
//    enough of the backlog to drop below the low-water mark.
//
// Pure and Electron/node-pty-free (the pty control + IPC send are injected
// callbacks, timers are injectable) so the coalescing and flow-control logic
// is unit-testable without spawning a real shell, mirroring stream-throttle.ts.

/** Flush on a short timer even if the threshold never fires — bounds added
 * latency for interactive output (a single `ls`) to something imperceptible,
 * while still coalescing a flood into far fewer sends. */
export const DEFAULT_FLUSH_INTERVAL_MS = 8

/** Flush early once buffered-but-unsent output crosses this size, so one
 * enormous chunk (or a fast burst) doesn't wait out the full timer window. */
export const DEFAULT_FLUSH_THRESHOLD_BYTES = 64 * 1024

/** Bytes sent-but-unacked above which the pty is paused. 1MB mirrors the
 * VS Code ptyHost default and bounds how far a slow renderer can fall behind
 * before the OS-level read is throttled. */
export const DEFAULT_HIGH_WATER_MARK_BYTES = 1024 * 1024

/** Resume once unacked bytes drop to/below this. Set below the high-water
 * mark (not equal to it) so a renderer acking in small increments right at
 * the boundary doesn't thrash pause()/resume() on every ack. */
export const DEFAULT_LOW_WATER_MARK_BYTES = DEFAULT_HIGH_WATER_MARK_BYTES / 2

export interface TerminalOutputBatcherTimers {
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(handle: unknown): void
}

export interface TerminalOutputBatcherOptions {
  /** Deliver one coalesced chunk to the renderer. */
  send: (data: string) => void
  /** Stop the pty's OS-level read side (node-pty's pause()). */
  pause: () => void
  /** Resume the pty's OS-level read side (node-pty's resume()). */
  resume: () => void
  /** Byte length of a chunk; defaults to UTF-8 byte length, matching what
   *  actually crosses the IPC wire. Injectable so tests don't need Buffer. */
  byteLength?: (data: string) => number
  flushIntervalMs?: number
  flushThresholdBytes?: number
  highWaterMarkBytes?: number
  lowWaterMarkBytes?: number
  timers?: TerminalOutputBatcherTimers
}

export interface TerminalOutputBatcher {
  /** Buffer a chunk from the pty; schedules or triggers a flush. */
  push(data: string): void
  /** Force out any buffered chunk right now (e.g. before an exit event, so
   *  trailing output isn't dropped or reordered after the exit message). */
  flush(): void
  /** Renderer reports it has processed `bytes` of previously-sent output;
   *  may resume a paused pty. Robust to acking more than is outstanding
   *  (clamped at zero) so a renderer reload mid-stream can't drive the
   *  counter negative or crash. */
  ack(bytes: number): void
  /** True while the pty is paused for flow control (test/debug visibility). */
  isPaused(): boolean
  /** Clears any pending flush timer without sending. Call on session
   *  teardown so a disposed session's timer never fires. */
  dispose(): void
}

const defaultTimers: TerminalOutputBatcherTimers = {
  clearTimeout: handle => clearTimeout(handle as never),
  setTimeout
}

export function createTerminalOutputBatcher({
  send,
  pause,
  resume,
  byteLength = data => Buffer.byteLength(data, 'utf8'),
  flushIntervalMs = DEFAULT_FLUSH_INTERVAL_MS,
  flushThresholdBytes = DEFAULT_FLUSH_THRESHOLD_BYTES,
  highWaterMarkBytes = DEFAULT_HIGH_WATER_MARK_BYTES,
  lowWaterMarkBytes = DEFAULT_LOW_WATER_MARK_BYTES,
  timers = defaultTimers
}: TerminalOutputBatcherOptions): TerminalOutputBatcher {
  let pending: string[] = []
  let pendingBytes = 0
  let flushTimer: unknown = null
  let unackedBytes = 0
  let paused = false

  function clearFlushTimer() {
    if (flushTimer !== null) {
      timers.clearTimeout(flushTimer)
      flushTimer = null
    }
  }

  function flush() {
    clearFlushTimer()

    if (pending.length === 0) {
      return
    }

    const chunk = pending.length === 1 ? pending[0] : pending.join('')
    const bytes = pendingBytes

    pending = []
    pendingBytes = 0

    send(chunk)
    unackedBytes += bytes

    if (!paused && unackedBytes >= highWaterMarkBytes) {
      paused = true
      pause()
    }
  }

  return {
    ack(bytes) {
      if (!(bytes > 0)) {
        return
      }

      unackedBytes = Math.max(0, unackedBytes - bytes)

      if (paused && unackedBytes <= lowWaterMarkBytes) {
        paused = false
        resume()
      }
    },

    dispose() {
      clearFlushTimer()
    },

    flush,

    isPaused: () => paused,

    push(data) {
      if (data.length === 0) {
        return
      }

      pending.push(data)
      pendingBytes += byteLength(data)

      if (pendingBytes >= flushThresholdBytes) {
        flush()

        return
      }

      if (flushTimer === null) {
        flushTimer = timers.setTimeout(() => {
          flushTimer = null
          flush()
        }, flushIntervalMs)
      }
    }
  }
}
