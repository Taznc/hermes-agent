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
import { $removedSessionIds } from '@/store/projects'
import { $selectedStoredSessionId, $sessions, setSelectedStoredSessionId, setSessions } from '@/store/session'

import {
  $pendingArchiveUndos,
  ARCHIVE_UNDO_WINDOW_MS,
  archiveSessionWithUndo,
  isArchiveUndoPending,
  resetArchiveUndos,
  undoArchive
} from './session-archive-undo'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

beforeEach(() => {
  vi.useFakeTimers()
  setSessions([])
  $pinnedSessionIds.set([])
  $removedSessionIds.set(new Set())
  $selectedStoredSessionId.set(null)
  resetArchiveUndos()
  patchArchived.mockReset()
  patchArchived.mockResolvedValue({ ok: true })
})

afterEach(() => {
  resetArchiveUndos()
  vi.useRealTimers()
  setSessions([])
  $pinnedSessionIds.set([])
  $selectedStoredSessionId.set(null)
})

describe('archiveSessionWithUndo', () => {
  it('removes the session from the active list instantly and persists the archive', async () => {
    setSessions([row('a'), row('b')])

    await archiveSessionWithUndo('a')

    expect($sessions.get().map(s => s.id)).toEqual(['b'])
    expect(patchArchived).toHaveBeenCalledWith('a', true, undefined)
    expect(isArchiveUndoPending('a')).toBe(true)
  })

  it('records a pending entry with the archived row and its list position', async () => {
    setSessions([row('a'), row('b'), row('c')])

    await archiveSessionWithUndo('b')

    const entry = $pendingArchiveUndos.get().b

    expect(entry).toBeDefined()
    expect(entry?.index).toBe(1)
    expect(entry?.session.id).toBe('b')
  })

  it('deselects a currently-open session it archives', async () => {
    setSessions([row('a')])
    setSelectedStoredSessionId('a')

    await archiveSessionWithUndo('a')

    expect($selectedStoredSessionId.get()).toBeNull()
  })

  it('rolls back the optimistic archive if the backend rejects it', async () => {
    setSessions([row('a')])
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    await expect(archiveSessionWithUndo('a')).rejects.toThrow('network down')

    expect($sessions.get().map(s => s.id)).toEqual(['a'])
    expect(isArchiveUndoPending('a')).toBe(false)
  })
})

describe('undoArchive', () => {
  it('fully restores the session to its original list position within the window', async () => {
    setSessions([row('a'), row('b'), row('c')])

    await archiveSessionWithUndo('b')
    expect($sessions.get().map(s => s.id)).toEqual(['a', 'c'])

    await undoArchive('b')

    expect($sessions.get().map(s => s.id)).toEqual(['a', 'b', 'c'])
    expect(patchArchived).toHaveBeenCalledWith('b', false, undefined)
    expect(isArchiveUndoPending('b')).toBe(false)
  })

  it('restores a pin that was dropped on archive', async () => {
    setSessions([row('a')])
    $pinnedSessionIds.set(['a'])

    await archiveSessionWithUndo('a')
    expect($pinnedSessionIds.get()).toEqual([])

    await undoArchive('a')
    expect($pinnedSessionIds.get()).toEqual(['a'])
  })

  it('is a safe no-op after the 10s window expires', async () => {
    setSessions([row('a')])

    await archiveSessionWithUndo('a')
    vi.advanceTimersByTime(ARCHIVE_UNDO_WINDOW_MS + 1)

    expect(isArchiveUndoPending('a')).toBe(false)

    await expect(undoArchive('a')).resolves.toBeUndefined()
    expect($sessions.get()).toEqual([])
    // Only the original archive call landed — expiry and the no-op undo
    // never touched the backend.
    expect(patchArchived).toHaveBeenCalledTimes(1)
  })

  it('is a safe no-op for an id that was never archived through this path', async () => {
    await expect(undoArchive('never-archived')).resolves.toBeUndefined()
    expect(patchArchived).not.toHaveBeenCalled()
  })

  it('never double-restores when called twice concurrently', async () => {
    setSessions([row('a')])
    await archiveSessionWithUndo('a')

    await Promise.all([undoArchive('a'), undoArchive('a')])

    expect($sessions.get().map(s => s.id)).toEqual(['a'])
    expect(patchArchived).toHaveBeenCalledTimes(2) // archive + exactly one undo
  })

  it('keeps two concurrent pending archives fully independent', async () => {
    setSessions([row('a'), row('b')])

    await archiveSessionWithUndo('a')
    vi.advanceTimersByTime(5_000)
    await archiveSessionWithUndo('b')

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
    await archiveSessionWithUndo('a')
    patchArchived.mockRejectedValueOnce(new Error('network down'))

    await expect(undoArchive('a')).rejects.toThrow('network down')

    expect($sessions.get().map(s => s.id)).toEqual([])
  })
})
