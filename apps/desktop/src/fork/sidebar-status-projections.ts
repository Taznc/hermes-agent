import { computed } from 'nanostores'

import { stableRecord } from '@/lib/stable-array'
import { $sidebarStatusFilter } from '@/store/layout'
import { $sessionDotStateById, hasLiveTurn, type SessionDotState, sessionStatusBucket } from '@/store/session-dot-state'

const EMPTY: Readonly<Record<string, SessionDotState>> = Object.freeze({})
let buckets = EMPTY
let live = EMPTY

// Consumer-compatible dot maps, NOT another status authority. Every raw id
// (including lineage aliases and unloaded project rows) is projected. Missing
// ids keep upstream's idle fallback; resets flow through computed, not mirrors.
export const $sidebarFilterDotStateById = computed([$sessionDotStateById, $sidebarStatusFilter], (states, filter) => {
  if (!filter.length) {
    return (buckets = EMPTY)
  }

  const next: Record<string, SessionDotState> = Object.fromEntries(
    Object.entries(states).map(([id, state]) => [id, sessionStatusBucket(state)])
  )

  return (buckets = stableRecord(buckets, next))
})

// Status grouping asks ONLY hasLiveTurn. Background is deliberately not live;
// working, stalled and awaiting-input share upstream's positive answer.
export const $sidebarLiveDotStateById = computed($sessionDotStateById, states => {
  const next: Record<string, SessionDotState> = Object.fromEntries(
    Object.entries(states)
      .filter(([, state]) => hasLiveTurn(state))
      .map(([id]) => [id, 'working'])
  )

  return (live = stableRecord(live, next))
})
