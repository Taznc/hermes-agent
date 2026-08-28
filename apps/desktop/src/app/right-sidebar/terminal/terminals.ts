import { atom, computed } from 'nanostores'

import { readKey, writeKey } from '@/lib/storage'
import { $currentCwd } from '@/store/session'

import { setTerminalTakeover } from '../store'

import { releaseAgentTerminal, seedAgentTerminalCommand } from './agent-terminal-stream'

/** One in-app terminal tab. `id` is the renderer-side handle (distinct from the
 *  PTY session id the main process mints); each instance owns its own shell.
 *
 *  `restoreCwd`/`reviveBuffer` live OUTSIDE this reactive entry now (see the
 *  `buffers` module map below) — they are high-frequency, high-volume fields
 *  (up to 48KB each) written on every throttled PTY snapshot. Keeping them on
 *  $terminals meant every keystroke of streaming shell output published a
 *  fresh $terminals array (re-rendering every consumer: the tab rail, the
 *  active-terminal computed, anything else subscribed) and re-JSON.stringified
 *  every OTHER open tab's buffer just to persist one tab's update.
 *  $terminals now only ever changes on real tab metadata events: add, close,
 *  rename, select, or a shell-name report. */
export interface TerminalEntry {
  id: string
  /** Display label. `auto` adopts the resolved shell name until the user renames. */
  title: string
  auto: boolean
  /** Working directory, snapshotted once at creation. Terminals live outside
   *  session/project state — the only thing they inherit is this initial cwd
   *  (the project root if opened in one, else the backend's default). Switching
   *  sessions never moves or recreates a terminal; at most it re-SELECTS a tab
   *  already pointed at the session's cwd (see the $currentCwd listener). */
  cwd: string
  /** `user` = interactive PTY shell. `agent` = read-only mirror of an agent
   *  background process (`terminal(background=true)`), keyed by `procId`. */
  kind: 'user' | 'agent'
  procId?: string
}

/** Per-tab scrollback/cwd state, kept in a plain module Map instead of the
 *  reactive $terminals atom. User tabs only. */
export interface TerminalBuffer {
  /** Last observed working directory of the live shell (tracked via the PTY
   *  cwd probe / OSC 7). Used to reopen the tab where the user last `cd`'d
   *  rather than the original launch dir. */
  restoreCwd?: string
  /** Serialized xterm scrollback from the last session, replayed on relaunch so
   *  the tab reopens with its recent history (VS Code parity). Processes are NOT
   *  revived — a fresh shell starts beneath the restored buffer. */
  reviveBuffer?: string
}

const buffers = new Map<string, TerminalBuffer>()

interface PersistedTerminalEntry {
  auto: boolean
  cwd: string
  id: string
  title: string
}

interface PersistedTerminalState {
  activeTerminalId: null | string
  terminals: PersistedTerminalEntry[]
}

const TERMINALS_STORAGE_KEY = 'hermes.desktop.terminals.v1'
// Per-tab buffer state lives under its own key so a burst of scrollback/cwd
// updates on one tab never touches the bytes of every other tab's entry.
const bufferStorageKey = (id: string) => `hermes.desktop.terminal-buffer.v1.${id}`

// Cap a single tab's replayed history so the persisted layout can't blow the
// localStorage quota. Roughly mirrors VS Code's persistentSessionScrollback
// default (100 lines) once the serialized escape codes are counted in.
const MAX_REVIVE_BUFFER_CHARS = 48_000

function sanitizePersistedTerminal(value: unknown): PersistedTerminalEntry | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const record = value as Record<string, unknown>
  const id = typeof record.id === 'string' ? record.id.trim() : ''
  const title = typeof record.title === 'string' ? record.title.trim() : ''
  const cwd = typeof record.cwd === 'string' ? record.cwd : ''

  if (!id) {
    return null
  }

  // Migration: older persisted layouts carried restoreCwd/reviveBuffer inline
  // on the terminal entry itself. Lift them into the per-tab buffer store (and
  // let the buffer's own throttled persist write them back out under the new
  // key) instead of dropping a user's scrollback/cwd on first load post-update.
  const legacyRestoreCwd = typeof record.restoreCwd === 'string' && record.restoreCwd ? record.restoreCwd : undefined
  const legacyReviveBuffer = typeof record.reviveBuffer === 'string' ? record.reviveBuffer : undefined

  if (legacyRestoreCwd || legacyReviveBuffer) {
    buffers.set(id, {
      ...(legacyRestoreCwd ? { restoreCwd: legacyRestoreCwd } : {}),
      ...(legacyReviveBuffer ? { reviveBuffer: legacyReviveBuffer } : {})
    })
  }

  return {
    auto: typeof record.auto === 'boolean' ? record.auto : true,
    cwd,
    id,
    title: title || 'Terminal'
  }
}

function sanitizePersistedBuffer(value: unknown): TerminalBuffer | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const record = value as Record<string, unknown>
  const restoreCwd = typeof record.restoreCwd === 'string' && record.restoreCwd ? record.restoreCwd : undefined
  const reviveBuffer = typeof record.reviveBuffer === 'string' ? record.reviveBuffer : undefined

  if (!restoreCwd && !reviveBuffer) {
    return null
  }

  return {
    ...(restoreCwd ? { restoreCwd } : {}),
    ...(reviveBuffer ? { reviveBuffer } : {})
  }
}

function loadPersistedTerminals(): PersistedTerminalState {
  const fallback: PersistedTerminalState = { activeTerminalId: null, terminals: [] }
  const raw = readKey(TERMINALS_STORAGE_KEY)

  if (!raw) {
    return fallback
  }

  try {
    const parsed = JSON.parse(raw) as unknown

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return fallback
    }

    const record = parsed as Record<string, unknown>

    const terminals = Array.isArray(record.terminals)
      ? record.terminals.map(sanitizePersistedTerminal).filter((term): term is PersistedTerminalEntry => Boolean(term))
      : []

    const active =
      typeof record.activeTerminalId === 'string' && terminals.some(term => term.id === record.activeTerminalId)
        ? record.activeTerminalId
        : (terminals[0]?.id ?? null)

    // Load each surviving tab's own buffer key (skip ids already seeded by the
    // legacy inline migration above — first write wins, and the dedicated key
    // is the newer source of truth if both somehow exist).
    for (const term of terminals) {
      if (buffers.has(term.id)) {
        continue
      }

      const buffer = sanitizePersistedBuffer((() => {
        const rawBuffer = readKey(bufferStorageKey(term.id))

        if (!rawBuffer) {
          return null
        }

        try {
          return JSON.parse(rawBuffer) as unknown
        } catch {
          return null
        }
      })())

      if (buffer) {
        buffers.set(term.id, buffer)
      }
    }

    return { activeTerminalId: active, terminals }
  } catch {
    return fallback
  }
}

// Persist synchronously on every change (the app-wide convention — see panes.ts
// / layout.ts). This list is now pure metadata (id/title/cwd/auto) — no
// per-tab scrollback — so a rename or a new tab is a tiny, cheap write
// regardless of how much history any open tab is carrying.
function persistTerminals(list: readonly TerminalEntry[], activeTerminalId: null | string) {
  const terminals = list
    .filter(term => term.kind === 'user')
    .map(term => ({ auto: term.auto, cwd: term.cwd, id: term.id, title: term.title }))

  if (!terminals.length) {
    writeKey(TERMINALS_STORAGE_KEY, null)

    return
  }

  const active = terminals.some(term => term.id === activeTerminalId) ? activeTerminalId : (terminals[0]?.id ?? null)
  writeKey(TERMINALS_STORAGE_KEY, JSON.stringify({ activeTerminalId: active, terminals }))
}

// Leading-edge-ish throttle for buffer persistence, mirroring the snapshot
// cadence already imposed upstream (use-terminal-session's SNAPSHOT_THROTTLE_MS)
// so we don't add a second competing timer — this just decouples the WRITE
// target (per-tab key, not the whole-list key) from the atom.
const pendingBufferWrites = new Map<string, ReturnType<typeof setTimeout>>()

function persistBufferNow(id: string) {
  pendingBufferWrites.delete(id)
  const buffer = buffers.get(id)

  if (!buffer || (!buffer.restoreCwd && !buffer.reviveBuffer)) {
    writeKey(bufferStorageKey(id), null)

    return
  }

  writeKey(bufferStorageKey(id), JSON.stringify(buffer))
}

function scheduleBufferPersist(id: string) {
  if (pendingBufferWrites.has(id)) {
    return
  }

  pendingBufferWrites.set(
    id,
    setTimeout(() => persistBufferNow(id), 250)
  )
}

function freeBuffer(id: string) {
  const timer = pendingBufferWrites.get(id)

  if (timer) {
    clearTimeout(timer)
    pendingBufferWrites.delete(id)
  }

  buffers.delete(id)
  writeKey(bufferStorageKey(id), null)
}

const restored = loadPersistedTerminals()

export const $terminals = atom<readonly TerminalEntry[]>(
  restored.terminals.map(term => ({ ...term, kind: 'user' as const }))
)
export const $activeTerminalId = atom<string | null>(restored.activeTerminalId)

$terminals.subscribe(list => persistTerminals(list, $activeTerminalId.get()))
$activeTerminalId.subscribe(active => persistTerminals($terminals.get(), active))

export const $activeTerminal = computed(
  [$terminals, $activeTerminalId],
  (list, id) => list.find(term => term.id === id) ?? null
)

/** Read a user tab's current buffer (scrollback + last observed cwd). Not
 *  reactive — callers that need it on render (mount props) read it once;
 *  live updates during a session flow through refs, not props (see
 *  useTerminalSession's initialReviveBufferRef/initialRestoreCwdRef). */
export function getTerminalBuffer(id: string): TerminalBuffer | undefined {
  return buffers.get(id)
}

const newId = () =>
  globalThis.crypto?.randomUUID?.() ?? `term-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`

/** Append a fresh terminal and focus it. Captures the current cwd once (its only
 *  tie to session/project state); pass an explicit cwd to override. Returns the id. */
export function createTerminal(cwd: string = $currentCwd.get()): string {
  const id = newId()
  $terminals.set([...$terminals.get(), { id, title: 'Terminal', auto: true, cwd, kind: 'user' }])
  $activeTerminalId.set(id)

  return id
}

// Procs we've already surfaced a tab for — so closing an agent tab doesn't
// resurrect it on the next poll while the process is still running.
const surfacedProcs = new Set<string>()

const findByProc = (procId: string) => $terminals.get().find(term => term.procId === procId)

/** Auto-surface an agent background process as a read-only tab — once. Returns
 *  the tab id, or null if it was already surfaced and the user has since closed it. */
export function ensureAgentTerminal(procId: string, title: string): string | null {
  const existing = findByProc(procId)

  if (existing) {
    return existing.id
  }

  if (surfacedProcs.has(procId)) {
    return null
  }

  surfacedProcs.add(procId)
  const id = newId()
  $terminals.set([...$terminals.get(), { id, title: title || 'agent', auto: false, cwd: '', kind: 'agent', procId }])

  return id
}

/** Open + focus an agent process's tab (the status-stack link), recreating it if
 *  the user had closed it. Opens the pane. */
export function openAgentTerminal(procId: string, title: string): void {
  surfacedProcs.add(procId)
  seedAgentTerminalCommand(procId, title)
  let id = findByProc(procId)?.id

  if (!id) {
    id = newId()
    $terminals.set([...$terminals.get(), { id, title: title || 'agent', auto: false, cwd: '', kind: 'agent', procId }])
  }

  $activeTerminalId.set(id)
  setTerminalTakeover(true)
}

/** Guarantee at least one tab exists when the pane opens.
 *  If a status-stack click already opened an agent tab, don't create a
 *  second, unrelated user shell just because the pane became visible. */
export function ensureTerminal(): void {
  if ($terminals.get().length === 0) {
    createTerminal()
  }
}

export function selectTerminal(id: string): void {
  if ($terminals.get().some(term => term.id === id)) {
    $activeTerminalId.set(id)
  }
}

// Compare-ready form of a directory path: trimmed, trailing separators dropped
// (keeping a bare root intact) so `/repo/` and `/repo` are the same place.
const normalizePath = (value: string) => {
  const trimmed = value.trim()

  return trimmed.length > 1 ? trimmed.replace(/[\\/]+$/, '') || trimmed : trimmed
}

/** The directory a tab points at right now — the live shell cwd once observed
 *  (survives a `cd`), else the launch dir. */
const terminalCwd = (term: TerminalEntry) => normalizePath(buffers.get(term.id)?.restoreCwd || term.cwd)

// Session ↔ terminal linking. Entering a session whose cwd already has a user
// terminal pointed at it re-selects that tab, so the terminal pane follows the
// workspace you're in. Selection ONLY — it never creates a shell, never closes
// one, and never reveals the pane; a detached session (empty cwd) or a cwd no
// tab lives in leaves the tabs exactly where they were. `listen` (not
// `subscribe`) so boot keeps the persisted active tab.
$currentCwd.listen(cwd => {
  const target = normalizePath(cwd)

  if (!target) {
    return
  }

  const list = $terminals.get()
  const active = list.find(term => term.id === $activeTerminalId.get())

  if (active?.kind === 'user' && terminalCwd(active) === target) {
    return
  }

  const match = list.find(term => term.kind === 'user' && terminalCwd(term) === target)

  if (match) {
    $activeTerminalId.set(match.id)
  }
})

/** Move the active tab by `direction` (+1 next / -1 prev), wrapping around. */
export function cycleTerminal(direction: 1 | -1): void {
  const list = $terminals.get()

  if (list.length < 2) {
    return
  }

  const current = Math.max(
    0,
    list.findIndex(term => term.id === $activeTerminalId.get())
  )

  $activeTerminalId.set(list[(current + direction + list.length) % list.length].id)
}

/** Drop a terminal. Focus slides to the neighbor that fills its slot; closing
 *  the last one closes the whole pane. Also frees the closed tab's buffer
 *  entry (localStorage key + module Map), and — for an agent mirror — releases
 *  its agent-terminal-stream state (backlog/header/snapshot) if the underlying
 *  process is already known-exited (see releaseAgentTerminal). */
export function closeTerminal(id: string): void {
  const list = $terminals.get()
  const index = list.findIndex(term => term.id === id)

  if (index < 0) {
    return
  }

  const closed = list[index]!

  if (closed.kind === 'agent' && closed.procId) {
    releaseAgentTerminal(closed.procId)
  }

  freeBuffer(id)

  const next = list.filter(term => term.id !== id)
  $terminals.set(next)

  if ($activeTerminalId.get() === id) {
    $activeTerminalId.set((next[index] ?? next[index - 1])?.id ?? null)
  }

  if (!next.length) {
    setTerminalTakeover(false)
  }
}

/** Close the read-only agent tab mirroring a background process. The agent
 *  drives this via the desktop-gated `close_terminal` tool → `terminal.close`.
 *  The process is NOT killed — only the view is dropped; `surfacedProcs` keeps
 *  it from auto-resurfacing, and the status-stack row can reopen it on demand.
 *  No-op when no such tab exists. */
export function closeAgentTerminalByProc(procId: string): boolean {
  const term = $terminals.get().find(t => t.kind === 'agent' && t.procId === procId)

  if (!term) {
    return false
  }

  closeTerminal(term.id)

  return true
}

export function closeActiveTerminal(): void {
  const id = $activeTerminalId.get()

  if (id) {
    closeTerminal(id)
  }
}

export function closeAllTerminals(): void {
  const list = $terminals.get()

  if (list.length === 0) {
    return
  }

  for (const term of list) {
    if (term.kind === 'agent' && term.procId) {
      releaseAgentTerminal(term.procId)
    }

    freeBuffer(term.id)
  }

  $terminals.set([])
  $activeTerminalId.set(null)
  setTerminalTakeover(false)
}

export function closeOtherTerminals(id: string): void {
  const list = $terminals.get()
  const keep = list.find(term => term.id === id)

  if (!keep) {
    return
  }

  for (const term of list) {
    if (term.id === id) {
      continue
    }

    if (term.kind === 'agent' && term.procId) {
      releaseAgentTerminal(term.procId)
    }

    freeBuffer(term.id)
  }

  $terminals.set([keep])
  $activeTerminalId.set(keep.id)
}

/** Record the latest serialized scrollback for a tab so it can be replayed on
 *  the next launch. Oversized buffers are tail-trimmed to stay under the storage
 *  budget; only user tabs ever carry one. Writes the module buffer map and
 *  schedules a throttled per-tab persist — this does NOT touch $terminals, so
 *  streaming shell output no longer republishes the terminal list or
 *  re-stringifies every other open tab on each snapshot. */
export function updateTerminalReviveBuffer(id: string, reviveBuffer: string): void {
  const term = $terminals.get().find(t => t.id === id)

  if (!term || term.kind !== 'user') {
    return
  }

  const capped =
    reviveBuffer.length > MAX_REVIVE_BUFFER_CHARS ? reviveBuffer.slice(-MAX_REVIVE_BUFFER_CHARS) : reviveBuffer

  const current = buffers.get(id)

  if (current?.reviveBuffer === capped) {
    return
  }

  buffers.set(id, { ...current, reviveBuffer: capped })
  scheduleBufferPersist(id)
}

/** Record the shell's latest working directory for a tab so the next launch can
 *  restart the PTY there instead of the original launch dir. User tabs only;
 *  no-ops when the value is empty or unchanged to avoid redundant persistence.
 *  Same buffer-map path as updateTerminalReviveBuffer — never touches $terminals. */
export function updateTerminalRestoreCwd(id: string, restoreCwd: string): void {
  const next = restoreCwd.trim()

  if (!next) {
    return
  }

  const term = $terminals.get().find(t => t.id === id)

  if (!term || term.kind !== 'user') {
    return
  }

  const current = buffers.get(id)

  if (current?.restoreCwd === next) {
    return
  }

  buffers.set(id, { ...current, restoreCwd: next })
  scheduleBufferPersist(id)
}

export function renameTerminal(id: string, title: string): void {
  const trimmed = title.trim()

  $terminals.set(
    $terminals.get().map(term => (term.id === id ? { ...term, title: trimmed || term.title, auto: false } : term))
  )
}

/** A live terminal reports its resolved shell; adopt it as the label only while
 *  the user hasn't named the tab themselves. */
export function reportTerminalShell(id: string, shell: string): void {
  const name = shell.trim()

  if (!name) {
    return
  }

  $terminals.set($terminals.get().map(term => (term.id === id && term.auto ? { ...term, title: name } : term)))
}
