/**
 * Tests for the multi-board events socket seam in api.ts (t_f5d40ce4): the All Boards
 * (`ALL_BOARDS` sentinel) socket is primed from a `cursors` map, idempotently per selection,
 * and routes each incoming event to its OWN board (never the sentinel) for cache invalidation
 * and completion notification.
 *
 * `@hermes/plugin-sdk` is mocked with lightweight real primitives (nanostores' `atom`, a real
 * `QueryClient`) rather than `vi.importActual` — the real module re-exports the whole app
 * entry point and is far too heavy to reimport fresh in every test's `vi.resetModules()`
 * (api.ts is module-scoped state, so each test needs a clean module instance). Only
 * `./completion-notify` is mocked, so `onKanbanEventsFrame` calls can be asserted directly.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { atom, sdkQueryClient } = vi.hoisted(() => {
  const { QueryClient } = require('@tanstack/react-query')
  const { atom: realAtom } = require('nanostores')

  return { atom: realAtom, sdkQueryClient: new QueryClient() }
})

vi.mock('@hermes/plugin-sdk', () => ({ atom, queryClient: sdkQueryClient }))

const onKanbanEventsFrameMock = vi.fn().mockResolvedValue(false)

vi.mock('./completion-notify', () => ({
  bindCompletionNotify: vi.fn(),
  onKanbanEventsFrame: (...args: unknown[]) => onKanbanEventsFrameMock(...args)
}))

async function freshApi() {
  vi.resetModules()

  return import('./api')
}

/** A minimal storage stub: get() always returns the fallback (nothing persisted yet). */
function makeStorage() {
  return {
    get: <T>(_key: string, fallback: T) => fallback,
    remove: vi.fn(),
    set: vi.fn()
  }
}

describe('primeAllBoardsSocket', () => {
  let api: Awaited<ReturnType<typeof freshApi>>
  let socketCalls: Array<{ path: string; onMessage: (data: unknown) => void; dispose: () => void }>
  let socketMock: (path: string, onMessage: (data: unknown) => void) => () => void

  beforeEach(async () => {
    vi.clearAllMocks()
    api = await freshApi()
    socketCalls = []

    socketMock = (path, onMessage) => {
      const dispose = vi.fn()

      socketCalls.push({ dispose, onMessage, path })

      return dispose
    }
  })

  afterEach(() => {
    api.$boardSlug.set('')
  })

  it('does nothing outside All Boards mode', () => {
    api.bindApi(vi.fn(), makeStorage(), socketMock)
    socketCalls.length = 0 // bindApi's own open() already opened the default '' socket; ignore it
    api.$boardSlug.set('')

    api.primeAllBoardsSocket({ default: 3 })

    expect(socketCalls).toHaveLength(0)
  })

  it('opens the boards=* socket seeded from the cursors map', () => {
    api.bindApi(vi.fn(), makeStorage(), socketMock)
    api.$boardSlug.set(api.ALL_BOARDS)
    socketCalls.length = 0 // discard the initial '' socket bindApi opened before the sentinel was selected

    api.primeAllBoardsSocket({ homelab: 10, shipping: 5 })

    expect(socketCalls).toHaveLength(1)
    expect(socketCalls[0].path).toContain('/events?boards=*&cursors=')
    const encoded = socketCalls[0].path.split('cursors=')[1]
    expect(JSON.parse(decodeURIComponent(encoded))).toEqual({ homelab: 10, shipping: 5 })
  })

  it('is idempotent: a second call with different cursors does not reopen the socket', () => {
    api.bindApi(vi.fn(), makeStorage(), socketMock)
    api.$boardSlug.set(api.ALL_BOARDS)
    socketCalls.length = 0

    api.primeAllBoardsSocket({ shipping: 5 })
    api.primeAllBoardsSocket({ shipping: 999 }) // stale/later poll snapshot — must be ignored

    expect(socketCalls).toHaveLength(1)
    const encoded = socketCalls[0].path.split('cursors=')[1]
    expect(JSON.parse(decodeURIComponent(encoded))).toEqual({ shipping: 5 })
  })

  it('re-primes with a fresh socket after leaving and re-entering All Boards mode', () => {
    api.bindApi(vi.fn(), makeStorage(), socketMock)
    api.$boardSlug.set(api.ALL_BOARDS)
    socketCalls.length = 0

    api.primeAllBoardsSocket({ shipping: 5 })
    expect(socketCalls).toHaveLength(1)
    expect(socketCalls[0].dispose).not.toHaveBeenCalled()

    // Switching to a real board closes the All Boards socket (bindApi's `open()`).
    api.$boardSlug.set('shipping')
    expect(socketCalls[0].dispose).toHaveBeenCalledTimes(1)
    socketCalls.length = 0 // discard the '/events?board=shipping' socket bindApi just opened

    // Re-entering All Boards mode primes again from a fresh (possibly different) cursor map.
    api.$boardSlug.set(api.ALL_BOARDS)
    api.primeAllBoardsSocket({ shipping: 20 })

    expect(socketCalls).toHaveLength(1)
    const encoded = socketCalls[0].path.split('cursors=')[1]
    expect(JSON.parse(decodeURIComponent(encoded))).toEqual({ shipping: 20 })
  })

  it('does nothing before bindApi has bound a socket door', () => {
    api.$boardSlug.set(api.ALL_BOARDS)

    expect(() => api.primeAllBoardsSocket({ shipping: 1 })).not.toThrow()
    expect(socketCalls).toHaveLength(0)
  })
})

describe('multi-board event frame routing', () => {
  let api: Awaited<ReturnType<typeof freshApi>>
  let onMessage: (data: unknown) => void

  beforeEach(async () => {
    vi.clearAllMocks()
    api = await freshApi()

    const socketMock = (_path: string, cb: (data: unknown) => void) => {
      onMessage = cb

      return vi.fn()
    }

    api.bindApi(vi.fn(), makeStorage(), socketMock)
    api.$boardSlug.set(api.ALL_BOARDS)
    api.primeAllBoardsSocket({ homelab: 0, shipping: 0 })
  })

  afterEach(() => {
    api.$boardSlug.set('')
  })

  it('invalidates the All Boards query cache and every touched task, keyed on the sentinel', () => {
    const spy = vi.spyOn(sdkQueryClient, 'invalidateQueries')

    onMessage({
      events: [
        { board: 'shipping', id: 1, kind: 'updated', payload: null, task_id: 'ship-1' },
        { board: 'homelab', id: 1, kind: 'updated', payload: null, task_id: 'home-1' }
      ]
    })

    const keys = spy.mock.calls.map(call => JSON.stringify((call[0] as { queryKey: unknown }).queryKey))

    expect(keys).toContain(JSON.stringify(api.boardKey(api.ALL_BOARDS, false)))
    expect(keys).toContain(JSON.stringify(api.boardKey(api.ALL_BOARDS, true)))
    expect(keys).toContain(JSON.stringify(api.BOARDS_KEY))
    expect(keys).toContain(JSON.stringify(api.taskKey(api.ALL_BOARDS, 'ship-1')))
    expect(keys).toContain(JSON.stringify(api.taskKey(api.ALL_BOARDS, 'home-1')))

    spy.mockRestore()
  })

  it('splits a merged frame back into per-board groups for notification', () => {
    onMessage({
      events: [
        { board: 'shipping', id: 5, kind: 'completed', payload: { summary: 'done' }, task_id: 'ship-1' },
        { board: 'homelab', id: 7, kind: 'blocked', payload: { reason: 'stuck' }, task_id: 'home-1' },
        { board: 'shipping', id: 6, kind: 'created', payload: null, task_id: 'ship-2' }
      ]
    })

    expect(onKanbanEventsFrameMock).toHaveBeenCalledTimes(2)
    const callsByBoard = new Map(onKanbanEventsFrameMock.mock.calls.map(call => [call[0], call[1]]))

    expect(callsByBoard.get('shipping')).toHaveLength(2)
    expect(callsByBoard.get('homelab')).toHaveLength(1)
    // Never notifies against the sentinel itself.
    expect(callsByBoard.has(api.ALL_BOARDS)).toBe(false)
  })

  it('ignores an empty or missing events array without throwing', () => {
    expect(() => onMessage({})).not.toThrow()
    expect(() => onMessage({ events: [] })).not.toThrow()
    expect(onKanbanEventsFrameMock).not.toHaveBeenCalled()
  })

  it('drops events with no board field from per-board notification (but still invalidates tasks)', () => {
    onMessage({ events: [{ id: 1, kind: 'updated', payload: null, task_id: 'orphan' }] })

    expect(onKanbanEventsFrameMock).not.toHaveBeenCalled()
  })
})
