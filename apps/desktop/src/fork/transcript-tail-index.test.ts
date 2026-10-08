import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import {
  $transcriptTailBySessionId,
  clearTranscriptTail,
  clearTranscriptTailPaging,
  recordTranscriptBackfillPage,
  recordTranscriptTail,
  rewindTranscriptTail,
  transcriptTailState,
  type TranscriptTailState
} from '@/store/transcript-tail'
import type { SessionMessagesResponse } from '@/types/hermes'

import { indexedTailEntries } from './transcript-tail-index'

it('declines unsupported record shapes rather than caching mutable accessors', () => {
  expect(indexedTailEntries([] as never, 's')).toBeUndefined()
  expect(indexedTailEntries(null as never, 's')).toBeUndefined()
  expect(indexedTailEntries(Object.create({ s: {} }), 's')).toBeUndefined()
  const accessor = Object.defineProperty({}, 's', { enumerable: true, get: () => ({ nextOffset: 2 }) })
  expect(indexedTailEntries(accessor, 's')).toBeUndefined()
})

const page = (offset = 0, count = 2, limit = 2): SessionMessagesResponse => ({
  session_id: 'fixture',
  messages: Array.from({ length: count }, () => ({ role: 'user', content: 'fixture' })),
  pagination: { offset, limit, returned: count, order: 'latest' }
})

beforeEach(clearTranscriptTailPaging)
afterEach(() => vi.restoreAllMocks())

it('reuses a warmed snapshot without parsing scoped keys on every lookup', () => {
  for (let i = 0; i < 128; i++) {
    recordTranscriptTail(`s${i}`, page(), { connectionId: 'c', profile: 'p' })
  }

  expect(transcriptTailState('s63')?.nextOffset).toBe(2)
  const parse = vi.spyOn(JSON, 'parse')

  for (let i = 0; i < 50; i++) {
    expect(transcriptTailState('s63')?.nextOffset).toBe(2)
  }

  expect(parse).not.toHaveBeenCalled()
})

it('retains original key/state iteration order and exact bare-key equality', () => {
  const state = (nextOffset: number): TranscriptTailState => ({ nextOffset, possiblyTruncated: true })
  const jsonId = '["gateway","profile","inner"]'

  const record = Object.freeze({
    '["g1","p","s"]': state(1),
    '2': state(2),
    s: state(3),
    '[42,null,"s"]': state(4), // legacy scan checks only arity and the third part
    '["g2","p","s"]': state(5),
    '["g","s"]': state(6),
    '{"2":"s"}': state(7),
    [jsonId]: state(8),
    [JSON.stringify(['other', 'p', jsonId])]: state(9),
    '["g","p",7]': state(10)
  })

  const scan = (id: string) =>
    Object.entries(record).filter(([key]) => {
      if (key === id) {
        return true
      }

      try {
        const parsed: unknown = JSON.parse(key)

        return Array.isArray(parsed) && parsed.length === 3 && parsed[2] === id
      } catch {
        return false
      }
    })

  for (const id of ['s', '2', jsonId, 'inner', 'missing', '7']) {
    expect(indexedTailEntries(record, id)).toEqual(scan(id))
    indexedTailEntries(record, id)?.forEach(([key, value]) => expect(value).toBe(record[key as keyof typeof record]))
  }

  expect(indexedTailEntries(record, jsonId)?.map(([key]) => key)).toEqual([
    jsonId,
    JSON.stringify(['other', 'p', jsonId])
  ])
  expect(indexedTailEntries(record, 7 as never)).toBeUndefined()
})

it('does not let mutation of the returned entry array corrupt the cached index', () => {
  const state = { nextOffset: 2, possiblyTruncated: true }
  const record = { s: state }
  const entries = indexedTailEntries(record, 's')!
  entries[0][0] = 'poisoned'
  entries.splice(0)
  expect(indexedTailEntries(record, 's')).toEqual([['s', state]])
})

it('invalidates by .set(newRecord), including externally replaced snapshots', () => {
  recordTranscriptTail('s', page())
  const first = $transcriptTailBySessionId.get()
  expect(transcriptTailState('s')?.nextOffset).toBe(2)
  $transcriptTailBySessionId.set({ s: { nextOffset: 9, possiblyTruncated: false } })
  const parse = vi.spyOn(JSON, 'parse')
  expect(transcriptTailState('s')?.nextOffset).toBe(9)
  expect(parse).toHaveBeenCalledTimes(1)
  parse.mockClear()
  expect(transcriptTailState('s')?.nextOffset).toBe(9)
  expect(parse).not.toHaveBeenCalled()
  expect(first.s.nextOffset).toBe(2)
  $transcriptTailBySessionId.set({})
  expect(transcriptTailState('s')).toBeUndefined()
})

it('falls back to the original scan for unsupported ids and accessors', () => {
  const state = { nextOffset: 7, possiblyTruncated: true }
  $transcriptTailBySessionId.set({ '["c","p",7]': state })
  expect(transcriptTailState(7 as never)).toBe(state)
  let calls = 0

  const record = Object.defineProperty({}, 's', {
    enumerable: true,
    get: () => ({ nextOffset: ++calls, possiblyTruncated: true })
  })

  $transcriptTailBySessionId.set(record)
  expect(transcriptTailState('s')?.nextOffset).toBe(1)
  expect(transcriptTailState('s')?.nextOffset).toBe(2)
})

it('preserves connection/profile ambiguity and owner-versus-route backfill semantics', () => {
  const a = { connectionId: 'c1', profile: 'p' }
  const b = { connectionId: 'c2', profile: 'p' }
  recordTranscriptTail('same', page(), undefined, a)
  recordTranscriptTail('same', page(0, 3, 3), { profile: 'p' }, b)
  expect(transcriptTailState('same')).toBeUndefined()
  expect(transcriptTailState('same', a)?.profile).toBeUndefined()
  expect(rewindTranscriptTail('same', 1)).toBe(false)
  recordTranscriptBackfillPage('same', page(99))
  expect(transcriptTailState('same', a)?.nextOffset).toBe(2)
  expect(rewindTranscriptTail('same', 1, a)).toBe(true)
  expect(transcriptTailState('same', a)?.nextOffset).toBe(1)
  recordTranscriptTail('same', page(0, 0), undefined, a)
  expect(transcriptTailState('same', a)?.possiblyTruncated).toBe(true)
  const before = $transcriptTailBySessionId.get()
  recordTranscriptTail('same', page(0, 0), undefined, a)
  expect($transcriptTailBySessionId.get()).toBe(before)
  clearTranscriptTail('same', b)
  expect(transcriptTailState('same')?.nextOffset).toBe(1)
  recordTranscriptBackfillPage('same', page(1, 1))
  expect(transcriptTailState('same')).toEqual({ nextOffset: 2, possiblyTruncated: false, profile: undefined })
  clearTranscriptTail('same')
  expect(transcriptTailState('same')).toBeUndefined()
})

it('keeps profiles on the same gateway distinct and clears only the specified owner', () => {
  const a = { connectionId: 'shared', profile: 'one' }
  const b = { connectionId: 'shared', profile: 'two' }
  recordTranscriptTail('s', page(), undefined, a)
  recordTranscriptTail('s', page(0, 3, 3), { profile: 'two' }, b)
  expect(transcriptTailState('s')).toBeUndefined()
  expect(transcriptTailState('s', a)?.nextOffset).toBe(2)
  expect(transcriptTailState('s', b)?.nextOffset).toBe(3)
  clearTranscriptTail('s', a)
  expect(transcriptTailState('s')?.profile).toEqual({ profile: 'two' })
})

it('does not arm backfill when an older backend omits the latest-order echo', () => {
  const legacy = page()
  delete legacy.pagination!.order
  recordTranscriptTail('legacy', legacy)
  expect(transcriptTailState('legacy')?.possiblyTruncated).toBe(false)
})

it('all current production writers preserve the prior record identity and values', () => {
  const scope = { connectionId: 'c', profile: 'p' }
  recordTranscriptTail('s', page(), scope)
  const snapshots: Array<Record<string, TranscriptTailState>> = []
  const copies: Array<Record<string, TranscriptTailState>> = []

  const capture = () => {
    snapshots.push($transcriptTailBySessionId.get())
    copies.push(structuredClone($transcriptTailBySessionId.get()))
    transcriptTailState('s') // warm the snapshot's index before the next writer
  }

  capture()
  recordTranscriptBackfillPage('s', page(2))
  capture()
  rewindTranscriptTail('s', 1)
  capture()
  recordTranscriptTail('other', page())
  capture()
  clearTranscriptTail('s')
  capture()
  clearTranscriptTailPaging()
  snapshots.forEach((snapshot, i) => {
    expect(snapshot).toEqual(copies[i])
    expect(snapshot).not.toBe($transcriptTailBySessionId.get())
  })
})

it('keeps the existing 256-entry MRU/eviction behavior after the index is warmed', () => {
  recordTranscriptTail('mru', page())

  for (let i = 0; i < 255; i++) {
    recordTranscriptTail(`e${i}`, page())
  }

  expect(transcriptTailState('mru')?.nextOffset).toBe(2)
  const before = $transcriptTailBySessionId.get()
  recordTranscriptTail('mru', page())
  expect($transcriptTailBySessionId.get()).toBe(before)
  recordTranscriptTail('overflow', page())
  expect(Object.keys($transcriptTailBySessionId.get())).toHaveLength(256)
  expect(transcriptTailState('mru')?.nextOffset).toBe(2)
  expect(transcriptTailState('e0')).toBeUndefined()
  clearTranscriptTailPaging()
  expect(transcriptTailState('mru')).toBeUndefined()
})
