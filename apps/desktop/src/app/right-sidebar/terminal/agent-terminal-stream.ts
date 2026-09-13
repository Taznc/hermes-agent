// Live agent-terminal output, pushed from the backend as `agent.terminal.output`
// events (see tui_gateway `_wire_agent_terminal_output`). Chunks route straight
// to the matching read-only xterm, keyed by process id — no polling, no tail
// truncation. A capped per-proc backlog lets a tab opened mid-stream replay what
// it missed, and lets a closed-then-reopened tab restore its history.

type Writer = (chunk: string) => void

// Backlog is stored as an append-only chunk list (+ a running byte count)
// instead of one flat string. Streaming output previously did
// `(backlog.get(id) ?? '') + chunk` then a tail `slice()` on EVERY chunk — an
// O(current backlog size) copy per chunk, so a chatty build burned gigabytes
// of cumulative allocation. Pushing a chunk and trimming old chunks off the
// head is amortized O(chunk); the full string is only ever joined when a
// terminal actually needs to replay it (attach or full-snapshot reset).
interface ProcBacklog {
  chunks: string[]
  bytes: number
}

const writers = new Map<string, Writer>()
const backlogs = new Map<string, ProcBacklog>()
const commandHeaders = new Map<string, string>()
const lastSnapshots = new Map<string, string>()
const seededCommands = new Set<string>()

// Process ids we've observed as exited (from the composer status-stack feed).
// Bounded independently of the maps above: a process can exit long before its
// mirror tab is ever closed (or never be closed at all), so this must not grow
// forever either — trim the oldest entry past the cap (Set iteration order is
// insertion order).
const exitedProcs = new Set<string>()
const MAX_EXITED_TRACKED = 64

// Distinct process ids we keep backlog/header/snapshot state for, newest-touched
// last. Bounds total memory even for procs that never exit and whose tab is
// never closed (the exited+closed path below). Evicting a proc's backlog here
// only drops REPLAY history for a later reopen — a currently-mounted writer
// keeps receiving live chunks directly (see writeAgentTerminalChunk), so an
// active tab never visibly loses data.
const procOrder: string[] = []
const MAX_TRACKED_PROCS = 16

const MAX_BACKLOG = 256_000

function freeProc(procId: string): void {
  backlogs.delete(procId)
  commandHeaders.delete(procId)
  lastSnapshots.delete(procId)
  seededCommands.delete(procId)
  exitedProcs.delete(procId)
}

function touchProc(procId: string): void {
  const index = procOrder.indexOf(procId)

  if (index !== -1) {
    procOrder.splice(index, 1)
  }

  procOrder.push(procId)

  while (procOrder.length > MAX_TRACKED_PROCS) {
    const evicted = procOrder.shift()

    if (evicted) {
      freeProc(evicted)
    }
  }
}

function appendToBacklog(procId: string, chunk: string): void {
  let entry = backlogs.get(procId)

  if (!entry) {
    entry = { bytes: 0, chunks: [] }
    backlogs.set(procId, entry)
  }

  entry.chunks.push(chunk)
  entry.bytes += chunk.length

  while (entry.bytes > MAX_BACKLOG && entry.chunks.length > 1) {
    const dropped = entry.chunks.shift()!
    entry.bytes -= dropped.length
  }

  // A single chunk alone can exceed the cap (rare) — tail-trim just that chunk.
  if (entry.chunks.length === 1 && entry.bytes > MAX_BACKLOG) {
    entry.chunks[0] = entry.chunks[0]!.slice(-MAX_BACKLOG)
    entry.bytes = entry.chunks[0].length
  }
}

function joinBacklog(procId: string): string {
  return backlogs.get(procId)?.chunks.join('') ?? ''
}

function resetBacklog(procId: string, text: string): void {
  backlogs.set(procId, { bytes: text.length, chunks: text ? [text] : [] })
}

/** A live agent terminal registers its xterm write and replays the backlog.
 *  Returns an idempotent unregister. */
export function registerAgentTerminalWriter(procId: string, write: Writer): () => void {
  writers.set(procId, write)
  touchProc(procId)

  const history = joinBacklog(procId)

  if (history) {
    write(history)
  }

  return () => {
    if (writers.get(procId) === write) {
      writers.delete(procId)
    }
  }
}

/** Append a streamed chunk: buffer it (capped) for future opens and write it to
 *  the live terminal, if one is mounted. */
export function writeAgentTerminalChunk(procId: string, chunk: string): void {
  if (!procId || !chunk) {
    return
  }

  touchProc(procId)
  appendToBacklog(procId, chunk)
  writers.get(procId)?.(chunk)
}

/** Seed the tab with the command immediately, so an agent terminal never opens
 *  as an empty void while stdout is still pending or not yet observed. */
export function seedAgentTerminalCommand(procId: string, command: string): void {
  const trimmed = command.trim()

  if (!procId || !trimmed || seededCommands.has(procId)) {
    return
  }

  seededCommands.add(procId)
  const header = `$ ${trimmed}\r\n`
  commandHeaders.set(procId, header)
  writeAgentTerminalChunk(procId, header)
}

/** Ingest a full output snapshot from process.list/status-stack. This is the
 *  fallback for older/not-yet-restarted gateways and a seed for tabs opened
 *  after output already exists. If it extends our current backlog, append only
 *  the delta; if the registry's rolling tail slid, reset to that tail. */
export function syncAgentTerminalSnapshot(procId: string, output: string): void {
  if (!procId || !output) {
    return
  }

  touchProc(procId)

  const current = joinBacklog(procId)
  const header = commandHeaders.get(procId) ?? ''
  const body = header && current.startsWith(header) ? current.slice(header.length) : current
  const previous = lastSnapshots.get(procId) ?? ''

  if (output === previous || output === body || body.endsWith(output)) {
    lastSnapshots.set(procId, output)

    return
  }

  if (output.startsWith(previous)) {
    writeAgentTerminalChunk(procId, output.slice(previous.length))
    lastSnapshots.set(procId, output)

    return
  }

  if (output.startsWith(body)) {
    writeAgentTerminalChunk(procId, output.slice(body.length))
    lastSnapshots.set(procId, output)

    return
  }

  const next = `${header}${output}`.slice(-MAX_BACKLOG)
  lastSnapshots.set(procId, output)
  resetBacklog(procId, next)
  writers.get(procId)?.(`\x1bc${next}`)
}

/** Mark a background process as finished (from the composer status-stack feed).
 *  Idempotent bookkeeping only — does not free anything by itself. A process
 *  can exit while its mirror tab is still open (replay must keep working), so
 *  freeing happens only once BOTH this fires and the tab is closed
 *  (see releaseAgentTerminal). */
export function markAgentTerminalExited(procId: string): void {
  if (!procId || exitedProcs.has(procId)) {
    return
  }

  exitedProcs.add(procId)

  if (exitedProcs.size > MAX_EXITED_TRACKED) {
    const oldest = exitedProcs.values().next().value

    if (oldest !== undefined) {
      exitedProcs.delete(oldest)
    }
  }
}

/** Called when the mirror tab for `procId` closes. Frees its backlog/header/
 *  snapshot state ONLY if the process is already known-exited — a still-running
 *  process keeps streaming into its backlog (unwritten, since no writer is
 *  mounted) so a later reopen can replay what it missed while the tab was
 *  closed. No-op if the process never exited or was already freed. */
export function releaseAgentTerminal(procId: string): void {
  if (!procId || !exitedProcs.has(procId)) {
    return
  }

  freeProc(procId)

  const index = procOrder.indexOf(procId)

  if (index !== -1) {
    procOrder.splice(index, 1)
  }
}

/** Debug-only introspection for manual verification (devtools console) and
 *  tests: how many process ids each map is currently tracking. Not used by
 *  any production code path. */
export function debugAgentTerminalStreamSizes(): Record<string, number> {
  return {
    backlogs: backlogs.size,
    commandHeaders: commandHeaders.size,
    exitedProcs: exitedProcs.size,
    lastSnapshots: lastSnapshots.size,
    procOrder: procOrder.length,
    seededCommands: seededCommands.size,
    writers: writers.size
  }
}
