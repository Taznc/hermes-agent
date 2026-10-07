import { readFileSync } from 'node:fs'

import { useStore } from '@nanostores/react'
import { act, cleanup, renderHook } from '@testing-library/react'
import type { WritableAtom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sidebarStatusFilter } from '@/store/layout'
import { hasLiveTurn, $sessionDotStateById as rawStore, sessionStatusBucket } from '@/store/session-dot-state'
import type { SessionDotState } from '@/store/session-dot-state'
import type * as DotModule from '@/store/session-dot-state'
const raw = rawStore as WritableAtom<Readonly<Record<string, SessionDotState>>>

vi.mock('@/store/session-dot-state', async importOriginal => {
  const actual = await importOriginal<typeof DotModule>()
  const { atom } = await import('nanostores')

  return { ...actual, $sessionDotStateById: atom<Readonly<Record<string, SessionDotState>>>({}) }
})

// Resolve the REAL consumer import seam. On base these use raw status; after
// adaptation they use fork projections. No missing-module error is the RED.
async function filterStore() {
  const source = readFileSync('src/app/chat/sidebar/index.tsx', 'utf8')
  const path = './sidebar-filter-status'

  return source.includes("from '@/fork/sidebar-filter-status'")
    ? (await import(/* @vite-ignore */ path)).$sessionDotStateById
    : raw
}

async function sectionStore() {
  const source = readFileSync('src/app/chat/sidebar/sessions-section.tsx', 'utf8')

  return source.includes("from '@/store/session-dot-state'")
    ? raw
    : (await import('./sidebar-group-actions')).$sessionDotStateById
}

beforeEach(() => {
  raw.set({})
  $sidebarStatusFilter.set([])
})
afterEach(() => {
  cleanup()
  raw.set({})
  $sidebarStatusFilter.set([])
})

describe('sidebar consumer subscription seams', () => {
  it('preserves every upstream bucket and live-turn answer, including attention and background', async () => {
    const filter = await filterStore()
    const section = await sectionStore()
    $sidebarStatusFilter.set(['working'])
    const states = ['working', 'stalled', 'background', 'needs-input', 'unread', 'draft', 'idle'] as const
    raw.set(Object.fromEntries(states.map(state => [state, state])))

    for (const state of states) {
      expect(sessionStatusBucket(filter.get()[state])).toBe(sessionStatusBucket(state))
      expect(hasLiveTurn(section.get()[state] ?? 'idle')).toBe(hasLiveTurn(state))
    }

    expect(sessionStatusBucket(filter.get().unknown)).toBe('idle')
  })

  it('root ignores raw status changes when unfiltered, and reuses unchanged filter buckets', async () => {
    const store = await filterStore()
    const { result } = renderHook(() => useStore(store))
    const empty = result.current
    act(() => raw.set({ 'off-page': 'working' }))
    expect(result.current).toBe(empty)
    act(() => $sidebarStatusFilter.set(['working']))
    const working = result.current
    expect(sessionStatusBucket(working['off-page'])).toBe('working')
    act(() => raw.set({ 'off-page': 'stalled' }))
    expect(result.current).toBe(working)
    act(() => raw.set({ 'off-page': 'background' }))
    expect(result.current).toBe(working)
    act(() => $sidebarStatusFilter.set([]))
    expect(result.current).toBe(empty)
  })

  it('section ignores working/stalled/awaiting transitions but tracks live membership and resets', async () => {
    const store = await sectionStore()
    const { result } = renderHook(() => useStore(store))
    act(() => raw.set({ 'off-page': 'working', ancestor: 'working', tip: 'working' }))
    const live = result.current
    act(() => raw.set({ 'off-page': 'stalled', ancestor: 'needs-input', tip: 'needs-input' }))
    expect(result.current).toBe(live)
    expect(hasLiveTurn(result.current.ancestor ?? 'idle')).toBe(true)
    act(() => raw.set({ 'off-page': 'background' }))
    expect(hasLiveTurn(result.current['off-page'] ?? 'idle')).toBe(false)
    expect(hasLiveTurn(result.current.unknown ?? 'idle')).toBe(false)
    act(() => raw.set({}))
    expect(result.current).toEqual({})
  })
})
