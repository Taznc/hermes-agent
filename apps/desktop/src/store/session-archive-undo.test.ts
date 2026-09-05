import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patchArchived = vi.fn<(id: string, archived: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(
  () => Promise.resolve({ ok: true })
)

vi.mock('@/hermes', () => ({
  // The store only needs the REST mutation; keep the mock minimal (same
  // pattern as session-unread-remote.test.ts / session-pin-sync.test.ts).
  setApiRequestProfile: () => {},
  setSessionArchived: (id: string, archived: boolean, profile?: null | string) => patchArchived(id, archived, profile)
}))

import { $pinnedSessionIds } from '@/store/layout'
import { $sessions, setSessions } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'

import {
  $pendingArchiveUndos,
  ARCHIVE_UNDO_WINDOW_MS,
  captureArchiveNeighbors,
  commitPendingArchive,
  discardPendingArchiveUndo,
  isArchiveUndoPending,
  registerPendingArchiveUndo,
  resetArchiveUndos,
  undoArchive
} from './session-archive-undo'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

/** Mimics the relevant slice of `archiveSession({ withUndo: true })` in
 *  use-session-actions/index.ts: capture the row's neighbors BEFORE the
 *  optimistic removal, remove it, kick off the archive write, and register
 *  the pending undo entry against that SAME write promise. Reimplemented
 *  here (rather than imported, which would drag in the whole React hook)
 *  so this suite can exercise session-archive-undo.ts's own contract in
 *  isolation. On rejection it mirrors the real rollback: drop the pending
 *  entry and restore the row. */
async function archiveViaStore(storedSessionId: string): Promise<void> {
  const session = $sessions.get().find(s => s.id === storedSessionId)

  if (!session) {
    throw new Error(`test setup: no such session ${storedSessionId}`)
  }

  const { nextPinId, prevPinId } = captureArchiveNeighbors(storedSessionId)
  const previousPinned = $pinnedSessionIds.get()
  const wasPinned = previousPinned.includes(storedSessionId)

  setSessions(prev => prev.filter(s => s.id !== storedSessionId))
  $pinnedSessionIds.set(previousPinned.filter(id => id !== storedSessionId))

  const writePromise = patchArchived(storedSessionId, true, undefined)

  registerPendingArchiveUndo({ nextPinId, prevPinId, session, storedSessionId, wasPinned, writePromise })

  try {
    await writePromise
  } catch (err) {
    discardPendingArchiveUndo(storedSessionId)
    setSessions(prev => [session, ...prev])
    $pinnedSessionIds.set(previousPinned)
    throw err
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  setSessions([])
  $pinnedSessionIds.set([])
  $removedSessionIds.set(new Set())
  resetArchiveUndos()
  patchArchived.mockReset()
  patchArchived.mockResolvedValue({ ok: true })
})

afterEach(() => {
  resetArchiveUndos()
  vi.useRealTimers()
  setSessions([])
  $pinnedSessionIds.set([])
})

describe('archive-with-undo bookkeeping', () => {
  it('removes the session from the active list instantly and persists the archive', async () => {
    setSessions([row('a'), row('b')])

    await archiveViaStore('a')

    expect($sessions.get().map(s => s.id)).toEqual(['b'])
    expect(patchArchived).toHaveBeenCalledWith('a', true, undefined)
    expect(isArchiveUndoPending('a')).toBe(true)
  })

  it('records a pending entry keyed by stable neighbor ids, not an absolute index', async () => {
    setSessions([row('a'), row('b'), row('c')])

    await archiveViaStore('b')

    const entry = $pendingArchiveUndos.get().b

    expect(entry).toBeDefined()
    expect(entry?.prevPinId).toBe('a')
    expect(entry?.nextPinId).toBe('c')
    expect(entry?.session.id).toBe('b')
  })

  it('rolls back the optimistic archive if the backend rejects it', async () => {
    setSessions([row('a')])
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    await expect(archiveViaStore('a')).rejects.toThrow('network down')

    expect($sessions.get().map(s => s.id)).toEqual(['a'])
    expect(isArchiveUndoPending('a')).toBe(false)
  })
})

describe('undoArchive', () => {
  it('fully restores the session to its original list position within the window', async () => {
    setSessions([row('a'), row('b'), row('c')])

    await archiveViaStore('b')
    expect($sessions.get().map(s => s.id)).toEqual(['a', 'c'])

    await undoArchive('b')

    expect($sessions.get().map(s => s.id)).toEqual(['a', 'b', 'c'])
    expect(patchArchived).toHaveBeenCalledWith('b', false, undefined)
    expect(isArchiveUndoPending('b')).toBe(false)
  })

  it('restores a pin that was dropped on archive', async () => {
    setSessions([row('a')])
    $pinnedSessionIds.set(['a'])

    await archiveViaStore('a')
    expect($pinnedSessionIds.get()).toEqual([])

    await undoArchive('a')
    expect($pinnedSessionIds.get()).toEqual(['a'])
  })

  it('is a safe no-op after the 10s window expires', async () => {
    setSessions([row('a')])

    await archiveViaStore('a')
    vi.advanceTimersByTime(ARCHIVE_UNDO_WINDOW_MS + 1)

    expect(isArchiveUndoPending('a')).toBe(false)

    await expect(undoArchive('a')).resolves.toBeUndefined()
    expect($sessions.get()).toEqual([])
    // Only the original archive call landed — expiry and the no-op undo
    // never touched the backend.
    expect(patchArchived).toHaveBeenCalledTimes(1)
  })

  it('checks expiresAt at invocation time rather than trusting timer-callback ordering', async () => {
    setSessions([row('a')])
    await archiveViaStore('a')

    // Simulate a call that lands one tick after expiry without the timer
    // callback having fired yet (e.g. it was queued behind other work) —
    // undoArchive must still treat the window as closed.
    const entry = $pendingArchiveUndos.get().a

    expect(entry).toBeDefined()

    vi.setSystemTime((entry?.expiresAt ?? 0) + 1)

    await expect(undoArchive('a')).resolves.toBeUndefined()
    expect(patchArchived).toHaveBeenCalledTimes(1)
  })

  it('is a safe no-op for an id that was never archived through this path', async () => {
    await expect(undoArchive('never-archived')).resolves.toBeUndefined()
    expect(patchArchived).not.toHaveBeenCalled()
  })

  it('never double-restores when called twice concurrently', async () => {
    setSessions([row('a')])
    await archiveViaStore('a')

    await Promise.all([undoArchive('a'), undoArchive('a')])

    expect($sessions.get().map(s => s.id)).toEqual(['a'])
    expect(patchArchived).toHaveBeenCalledTimes(2) // archive + exactly one undo
  })

  it('keeps two concurrent pending archives fully independent when timers differ', async () => {
    setSessions([row('a'), row('b')])

    await archiveViaStore('a')
    vi.advanceTimersByTime(5_000)
    await archiveViaStore('b')

    expect(Object.keys($pendingArchiveUndos.get()).sort()).toEqual(['a', 'b'])

    // a's timer (started first) expires; b still has time left.
    vi.advanceTimersByTime(5_001)
    expect(isArchiveUndoPending('a')).toBe(false)
    expect(isArchiveUndoPending('b')).toBe(true)

    await undoArchive('b')
    expect($sessions.get().map(s => s.id)).toEqual(['b'])

    // 'a' already expired — undoing it must not resurrect it or touch 'b'.
    await undoArchive('a')
    expect($sessions.get().map(s => s.id)).toEqual(['b'])
  })

  it('rolls the optimistic restore back if the backend rejects the undo', async () => {
    setSessions([row('a')])
    await archiveViaStore('a')
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    await expect(undoArchive('a')).rejects.toThrow('network down')

    expect($sessions.get().map(s => s.id)).toEqual([])
  })

  // --- Review t_548d0d33, blocking issue 1: concurrent-undo ordering -------
  //
  // list [a,b,c,d]; archive b (idx 1), then archive c (now idx 1 in the
  // reduced list [a,c,d]). Undoing in EITHER order must land back on
  // [a,b,c,d] — an absolute-index scheme produces [a,c,b,d] when undone
  // b-then-c. Covering both orderings pins the order-independence.
  it('restores concurrent archives correctly when undone in the same order they were archived (b then c)', async () => {
    setSessions([row('a'), row('b'), row('c'), row('d')])

    await archiveViaStore('b')
    await archiveViaStore('c')
    expect($sessions.get().map(s => s.id)).toEqual(['a', 'd'])

    await undoArchive('b')
    await undoArchive('c')

    expect($sessions.get().map(s => s.id)).toEqual(['a', 'b', 'c', 'd'])
  })

  it('restores concurrent archives correctly when undone in the OPPOSITE order (c then b)', async () => {
    setSessions([row('a'), row('b'), row('c'), row('d')])

    await archiveViaStore('b')
    await archiveViaStore('c')
    expect($sessions.get().map(s => s.id)).toEqual(['a', 'd'])

    await undoArchive('c')
    await undoArchive('b')

    expect($sessions.get().map(s => s.id)).toEqual(['a', 'b', 'c', 'd'])
  })

  // --- Review t_548d0d33, blocking issue 3: serialized writes --------------
  it("queues the undo PATCH behind a still in-flight archive PATCH instead of racing it", async () => {
    setSessions([row('a')])

    const order: string[] = []

    let resolveArchiveWrite: (value: { ok: boolean }) => void = () => {}

    const archiveWrite = new Promise<{ ok: boolean }>(resolve => {
      resolveArchiveWrite = resolve
    })

    patchArchived.mockImplementation((_id, archived) => {
      if (archived) {
        order.push('archive:sent')

        return archiveWrite.then(value => {
          order.push('archive:settled')

          return value
        })
      }

      order.push('undo:sent')

      return Promise.resolve({ ok: true })
    })

    // Not awaited: the archive write is still in flight when undo fires.
    const archivePromise = archiveViaStore('a')
    const undoPromise = undoArchive('a')

    // Flush microtasks without letting the archive write settle — the
    // undo's own PATCH must not have gone out yet.
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    expect(order).toEqual(['archive:sent'])

    resolveArchiveWrite({ ok: true })
    await archivePromise
    await undoPromise

    expect(order).toEqual(['archive:sent', 'archive:settled', 'undo:sent'])
  })

  it('surfaces (rejects with) an undo failure instead of dropping it silently', async () => {
    setSessions([row('a')])
    await archiveViaStore('a')
    patchArchived.mockRejectedValueOnce(new Error('undo backend rejected'))

    // The caller (wiring.tsx) is the one that turns this rejection into a
    // notifyError toast; this asserts the rejection actually propagates
    // instead of being swallowed the way the pre-fix `void undoArchive(...)`
    // call site did.
    await expect(undoArchive('a')).rejects.toThrow('undo backend rejected')
  })

  // --- Review t_548d0d33 round 2: deferred archive-rejection case ----------
  //
  // Undo clicked while the archive PATCH is still pending, and that archive
  // PATCH then rejects. Before this fix, `pendingWrites` swallowed the
  // rejection (`writePromise.catch(() => undefined)`), so `undoArchive`
  // proceeded as if the archive had succeeded: it optimistically restored
  // the row and fired the inverse `setSessionArchived(id, false)` PATCH —
  // racing/duplicating the canonical archive action's own catch-block
  // rollback, and (if that inverse PATCH also rejected) re-removing the row
  // the canonical rollback had just restored.
  it('does not issue an inverse PATCH and leaves the row restored when Undo is invoked before a deferred archive rejection settles', async () => {
    setSessions([row('a')])

    let rejectArchiveWrite: (err: Error) => void = () => {}

    const archiveWrite = new Promise<{ ok: boolean }>((_resolve, reject) => {
      rejectArchiveWrite = reject
    })

    patchArchived.mockImplementation((_id, archived) => (archived ? archiveWrite : Promise.resolve({ ok: true })))

    // Neither awaited yet: Undo fires while the archive write is still in
    // flight, mirroring a user clicking Undo on a slow network.
    const archivePromise = archiveViaStore('a')
    const undoPromise = undoArchive('a')

    rejectArchiveWrite(new Error('archive backend rejected'))

    await expect(archivePromise).rejects.toThrow('archive backend rejected')
    // undoArchive treats a failed archive write as "nothing to undo" — it
    // must resolve quietly rather than reject or act.
    await expect(undoPromise).resolves.toBeUndefined()

    // No inverse PATCH was ever sent for a write that never actually
    // archived anything — only the original (failed) archive attempt hit
    // the backend.
    expect(patchArchived).toHaveBeenCalledTimes(1)
    expect(patchArchived).toHaveBeenCalledWith('a', true, undefined)
    // The canonical archive's own rollback (mirrored by archiveViaStore's
    // catch) restored the row — undoArchive must not have disturbed it.
    expect($sessions.get().map(s => s.id)).toEqual(['a'])
    expect(isArchiveUndoPending('a')).toBe(false)
  })

  // Combination case: the archive itself DOES land, but the deferred undo's
  // own inverse PATCH then fails too. This must still roll back exactly as
  // the non-deferred case does (see "rolls the optimistic restore back if
  // the backend rejects the undo" above) — the fix for the case above must
  // not accidentally suppress a genuine inverse-PATCH failure.
  it('rolls back the optimistic restore when a deferred undo issues its inverse PATCH and that one also fails', async () => {
    setSessions([row('a')])

    let resolveArchiveWrite: (value: { ok: boolean }) => void = () => {}

    const archiveWrite = new Promise<{ ok: boolean }>(resolve => {
      resolveArchiveWrite = resolve
    })

    patchArchived.mockImplementation((_id, archived) =>
      archived ? archiveWrite : Promise.reject(new Error('inverse PATCH rejected'))
    )

    const archivePromise = archiveViaStore('a')
    const undoPromise = undoArchive('a')

    resolveArchiveWrite({ ok: true })
    await archivePromise

    await expect(undoPromise).rejects.toThrow('inverse PATCH rejected')

    // Undo's own rollback re-removes the row it had optimistically
    // restored, leaving the UI in the archived state the backend agrees on.
    expect($sessions.get().map(s => s.id)).toEqual([])
  })

  // --- Review t_548d0d33, blocking issue 4: notification-cap eviction ------
  it('commitPendingArchive (eviction path) drops the pending entry and cancels its timer without touching $sessions', async () => {
    setSessions([row('a'), row('b')])
    await archiveViaStore('a')

    expect(isArchiveUndoPending('a')).toBe(true)
    expect($sessions.get().map(s => s.id)).toEqual(['b'])

    // Simulates the notification stack's 4-item cap evicting this toast.
    commitPendingArchive('a')

    expect(isArchiveUndoPending('a')).toBe(false)
    // The archive itself is untouched — committing just forgets the undo
    // window, it is not another backend write.
    expect($sessions.get().map(s => s.id)).toEqual(['b'])

    // A no-op undo after commit must not resurrect the row or call the API
    // again.
    await undoArchive('a')
    expect($sessions.get().map(s => s.id)).toEqual(['b'])
    expect(patchArchived).toHaveBeenCalledTimes(1)
  })

  it('is safe to evict an id with no pending entry', () => {
    expect(() => commitPendingArchive('not-pending')).not.toThrow()
  })
})
