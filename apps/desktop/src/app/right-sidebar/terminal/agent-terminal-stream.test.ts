import { beforeEach, describe, expect, it, vi } from 'vitest'

async function loadStream() {
  vi.resetModules()

  return import('./agent-terminal-stream')
}

describe('agent-terminal-stream: chunked backlog', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('replays buffered chunks joined in order to a newly attached writer', async () => {
    const { registerAgentTerminalWriter, writeAgentTerminalChunk } = await loadStream()

    writeAgentTerminalChunk('proc-1', 'hello ')
    writeAgentTerminalChunk('proc-1', 'world')

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', chunk => received.push(chunk))

    expect(received).toEqual(['hello world'])
  })

  it('streams live chunks straight to a mounted writer without replaying the backlog again', async () => {
    const { registerAgentTerminalWriter, writeAgentTerminalChunk } = await loadStream()

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', chunk => received.push(chunk))

    writeAgentTerminalChunk('proc-1', 'a')
    writeAgentTerminalChunk('proc-1', 'b')

    expect(received).toEqual(['a', 'b'])
  })

  it('caps total backlog bytes by dropping the oldest chunks, not by copying the whole string per chunk', async () => {
    const { debugAgentTerminalStreamSizes, registerAgentTerminalWriter, writeAgentTerminalChunk } = await loadStream()

    // Push well past MAX_BACKLOG (256_000) in small chunks.
    const chunk = 'x'.repeat(1000)

    for (let i = 0; i < 300; i += 1) {
      writeAgentTerminalChunk('proc-1', chunk)
    }

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', text => received.push(text))

    expect(received).toHaveLength(1)
    expect(received[0]!.length).toBeLessThanOrEqual(256_000)
    // Should be near the cap, not empty or wildly under it.
    expect(received[0]!.length).toBeGreaterThan(250_000)
    expect(debugAgentTerminalStreamSizes().backlogs).toBe(1)
  })

  it('tail-trims a single oversized chunk to the cap', async () => {
    const { registerAgentTerminalWriter, writeAgentTerminalChunk } = await loadStream()

    const huge = 'y'.repeat(300_000)
    writeAgentTerminalChunk('proc-1', huge)

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', text => received.push(text))

    expect(received[0]!.length).toBe(256_000)
    expect(received[0]).toBe(huge.slice(-256_000))
  })
})

describe('agent-terminal-stream: bounded map lifecycle', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('does not free a still-running process on tab close', async () => {
    const { debugAgentTerminalStreamSizes, releaseAgentTerminal, writeAgentTerminalChunk } = await loadStream()

    writeAgentTerminalChunk('proc-1', 'hi')
    releaseAgentTerminal('proc-1') // never marked exited

    expect(debugAgentTerminalStreamSizes().backlogs).toBe(1)
  })

  it('frees all per-proc state once BOTH exited and tab-closed fire', async () => {
    const { debugAgentTerminalStreamSizes, markAgentTerminalExited, releaseAgentTerminal, seedAgentTerminalCommand, writeAgentTerminalChunk } =
      await loadStream()

    seedAgentTerminalCommand('proc-1', 'npm run build')
    writeAgentTerminalChunk('proc-1', 'building...')
    markAgentTerminalExited('proc-1')

    let sizes = debugAgentTerminalStreamSizes()
    expect(sizes.backlogs).toBe(1)
    expect(sizes.commandHeaders).toBe(1)
    expect(sizes.seededCommands).toBe(1)

    releaseAgentTerminal('proc-1')

    sizes = debugAgentTerminalStreamSizes()
    expect(sizes.backlogs).toBe(0)
    expect(sizes.commandHeaders).toBe(0)
    expect(sizes.seededCommands).toBe(0)
    expect(sizes.exitedProcs).toBe(0)
    expect(sizes.procOrder).toBe(0)
  })

  it('a reopened tab after exit-without-close still replays what it missed', async () => {
    const { markAgentTerminalExited, registerAgentTerminalWriter, writeAgentTerminalChunk } = await loadStream()

    writeAgentTerminalChunk('proc-1', 'done')
    markAgentTerminalExited('proc-1')
    // No releaseAgentTerminal — tab was never closed.

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', chunk => received.push(chunk))

    expect(received).toEqual(['done'])
  })

  it('evicts the oldest untouched process past the tracked-proc cap (LRU)', async () => {
    const { debugAgentTerminalStreamSizes, registerAgentTerminalWriter, writeAgentTerminalChunk } = await loadStream()

    // MAX_TRACKED_PROCS is 16 — push 20 distinct never-closed, never-exited procs.
    for (let i = 0; i < 20; i += 1) {
      writeAgentTerminalChunk(`proc-${i}`, 'output')
    }

    const sizes = debugAgentTerminalStreamSizes()
    expect(sizes.procOrder).toBe(16)
    expect(sizes.backlogs).toBe(16)

    // The earliest procs (0..3) were evicted — a late attach gets no replay.
    const received: string[] = []
    registerAgentTerminalWriter('proc-0', chunk => received.push(chunk))
    expect(received).toEqual([])

    // The most recent ones are still there.
    const receivedLatest: string[] = []
    registerAgentTerminalWriter('proc-19', chunk => receivedLatest.push(chunk))
    expect(receivedLatest).toEqual(['output'])
  })

  it('touching an old proc again (write or replay) keeps it alive past newer untouched ones', async () => {
    const { debugAgentTerminalStreamSizes, writeAgentTerminalChunk } = await loadStream()

    for (let i = 0; i < 15; i += 1) {
      writeAgentTerminalChunk(`proc-${i}`, 'output')
    }

    // Touch proc-0 again — it should now be the most-recently-used.
    writeAgentTerminalChunk('proc-0', 'more')

    // Push one more new proc, tipping over the 16-proc cap.
    writeAgentTerminalChunk('proc-15', 'output')
    expect(debugAgentTerminalStreamSizes().procOrder).toBe(16)

    // proc-0 (re-touched) should have survived; proc-1 (never re-touched, oldest) should be gone.
    const receivedZero: string[] = []
    const { registerAgentTerminalWriter } = await import('./agent-terminal-stream')
    registerAgentTerminalWriter('proc-0', chunk => receivedZero.push(chunk))
    expect(receivedZero).toEqual(['outputmore'])
  })
})

describe('agent-terminal-stream: full-snapshot sync', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('resets the backlog to a single chunk and clears the terminal on a divergent snapshot', async () => {
    const { debugAgentTerminalStreamSizes, registerAgentTerminalWriter, syncAgentTerminalSnapshot, writeAgentTerminalChunk } =
      await loadStream()

    writeAgentTerminalChunk('proc-1', 'stale')
    // Seed lastSnapshots with a real previous value first — an empty `previous`
    // trivially satisfies `output.startsWith(previous)` and takes the delta
    // path instead of the reset path, which is what this test wants to exercise.
    syncAgentTerminalSnapshot('proc-1', 'stale')

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', chunk => received.push(chunk))
    received.length = 0 // drop the initial replay

    syncAgentTerminalSnapshot('proc-1', 'a completely different snapshot')

    expect(received).toEqual(['\x1bca completely different snapshot'])
    expect(debugAgentTerminalStreamSizes().backlogs).toBe(1)
  })

  it('appends only the delta when the snapshot extends the current backlog', async () => {
    const { registerAgentTerminalWriter, syncAgentTerminalSnapshot, writeAgentTerminalChunk } = await loadStream()

    writeAgentTerminalChunk('proc-1', 'line1')
    syncAgentTerminalSnapshot('proc-1', 'line1') // seed a real previous snapshot

    const received: string[] = []
    registerAgentTerminalWriter('proc-1', chunk => received.push(chunk))
    received.length = 0

    syncAgentTerminalSnapshot('proc-1', 'line1line2')

    expect(received).toEqual(['line2'])
  })
})
