/**
 * Focus-mode state shared by the board-level overlay (`page.tsx`) and every
 * card slot. Module atoms rather than React context: the slots render inside
 * upstream's `Card`, which the fork can't wrap in a provider without editing
 * more of upstream's board. Presentation only — a reload starts unfocused.
 */

import { atom } from 'nanostores'

import type { DependencyGraph } from '@/fork/kanban/deps'
import type { KanbanTask } from '@/fork/kanban/types'

export type FocusDepth = 'chain' | 'direct'

/** Everything a card needs to paint its part of a trace. Rebuilt by the page
 *  overlay per board payload / focus change, never per card. */
export interface DependencyView {
  downstream: ReadonlySet<string>
  focused: null | string
  graph: DependencyGraph
  /** False when the fork backend's edges are unavailable: counts only, so
   *  there is nothing to trace and no trace affordance is offered. */
  hasEdges: boolean
  index: Map<string, KanbanTask>
  upstream: ReadonlySet<string>
}

export const EMPTY_IDS: ReadonlySet<string> = new Set<string>()

export const NO_DEPENDENCIES: DependencyView = {
  downstream: EMPTY_IDS,
  focused: null,
  graph: { blockedBy: new Map(), blocking: new Map() },
  hasEdges: false,
  index: new Map(),
  upstream: EMPTY_IDS
}

export const $depView = atom<DependencyView>(NO_DEPENDENCIES)

/** The focused card's key (`deps.cardKey`), or null. */
export const $focused = atom<null | string>(null)
export const $focusDepth = atom<FocusDepth>('direct')

/** Toggle focus on `key`: re-triggering the focused card clears it. */
export function toggleFocus(key: string): void {
  $focused.set($focused.get() === key ? null : key)
}

/** A localStorage-backed boolean atom (answer-bar display toggles). */
function persisted(key: string, fallback: boolean) {
  const storageKey = `hermes.kanban-fork.${key}` // pre-rename key, kept so saved board choices survive
  let initial = fallback

  try {
    const raw = globalThis.localStorage?.getItem(storageKey)

    initial = raw === null || raw === undefined ? fallback : raw === '1'
  } catch {
    // Storage blocked (privacy mode, tests): keep the default.
  }

  const store = atom<boolean>(initial)

  store.listen(value => {
    try {
      globalThis.localStorage?.setItem(storageKey, value ? '1' : '0')
    } catch {
      // Non-fatal: the toggle just won't survive a reload.
    }
  })

  return store
}

export const $depChevrons = persisted('depChevrons', true)
export const $depFlow = persisted('depFlow', false)

/** How the card wrapper renders one card while a trace is live: as itself,
 *  folded away, or as the "+N cards" marker that stands for its run (the run's
 *  FIRST card carries the marker). Absent key = render normally. Published by
 *  the overlay from the lanes upstream actually renders. */
export type FoldSlot = { count: number; gap: string; kind: 'gap' } | { kind: 'hidden' }

export const NO_FOLDS: ReadonlyMap<string, FoldSlot> = new Map()

export const $folds = atom<ReadonlyMap<string, FoldSlot>>(NO_FOLDS)

/** Gaps the user opened, remembered only for the focus they were opened
 *  under: a new trace folds from scratch. */
export const $openGaps = atom<{ focus: null | string; ids: ReadonlySet<string> }>({ focus: null, ids: EMPTY_IDS })

export function openGap(id: string): void {
  const focus = $focused.get()
  const current = $openGaps.get()
  const ids = current.focus === focus ? current.ids : EMPTY_IDS

  $openGaps.set({ focus, ids: new Set([...ids, id]) })
}

/** Card keys upstream currently has selected (reported by the card wrapper).
 *  Selected cards never fold — a bulk action must not hit cards the user
 *  can't see — and Esc belongs to the selection while one exists. */
export const $selectedKeys = atom<ReadonlySet<string>>(EMPTY_IDS)

export function markSelected(key: string, selected: boolean): void {
  const current = $selectedKeys.get()

  if (current.has(key) === selected) {
    return
  }

  const next = new Set(current)

  if (selected) {
    next.add(key)
  } else {
    next.delete(key)
  }

  $selectedKeys.set(next)
}

/** All Boards filter chips: boards whose cards are hidden. Presentation only. */
export const $hiddenBoards = atom<ReadonlySet<string>>(EMPTY_IDS)

export function toggleBoardHidden(slug: string): void {
  const next = new Set($hiddenBoards.get())

  if (!next.delete(slug)) {
    next.add(slug)
  }

  $hiddenBoards.set(next)
}

/** Reset every piece of focus state (board switch, page unmount, tests). */
export function resetFocus(): void {
  $focused.set(null)
  $depView.set(NO_DEPENDENCIES)
  $folds.set(NO_FOLDS)
  $openGaps.set({ focus: null, ids: EMPTY_IDS })
}
