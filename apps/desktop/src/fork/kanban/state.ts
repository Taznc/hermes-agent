/**
 * Focus-mode state shared by the board-level overlay (`page.tsx`) and every
 * card slot. Module atoms rather than React context: the slots render inside
 * upstream's `Card`, which the fork can't wrap in a provider without editing
 * more of upstream's board. Presentation only — a reload starts unfocused.
 */

import { atom } from 'nanostores'

import type { DependencyGraph } from './deps'
import type { KanbanTask } from './types'

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
  const storageKey = `hermes.kanban-fork.${key}`
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
