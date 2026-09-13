import { describe, expect, it } from 'vitest'

import {
  COLUMN_META,
  columnMeta,
  type Diagnostic,
  isRoadmapLane,
  laneDropAllowed,
  orderLanes,
  ROADMAP_LANE_TRANSITIONS,
  ROADMAP_LANES,
  SEVERITY_TONE
} from './types'

describe('SEVERITY_TONE', () => {
  it('has a real, non-undefined tone for every Diagnostic severity, including info', () => {
    const severities: Diagnostic['severity'][] = ['critical', 'error', 'warning', 'info']

    for (const sev of severities) {
      const tone = SEVERITY_TONE[sev]
      expect(tone).toBeTypeOf('string')
      expect(tone.length).toBeGreaterThan(0)
    }
  })

  it('gives info a distinct, non-destructive tone from error/critical', () => {
    expect(SEVERITY_TONE.info).not.toBe(SEVERITY_TONE.error)
    expect(SEVERITY_TONE.info).not.toBe(SEVERITY_TONE.critical)
  })
})

// The backend returns `idea`/`roadmap` as real columns (BOARD_COLUMNS ends
// `…, "done", "idea", "roadmap"`). Without their own COLUMN_META entries they
// fall through to the generic `circle-outline` dot, which is what these lanes
// looked like before this card landed.
describe('COLUMN_META — wishlist lanes', () => {
  it.each(ROADMAP_LANES)('%s has its own entry rather than the unknown-status fallback', name => {
    const fallback = columnMeta('some_future_status')

    expect(COLUMN_META[name]).toBeTruthy()
    expect(columnMeta(name).codicon).not.toBe(fallback.codicon)
  })

  it('gives the lanes distinct icons from each other and from triage', () => {
    const icons = [columnMeta('idea').codicon, columnMeta('roadmap').codicon, columnMeta('triage').codicon]

    expect(new Set(icons).size).toBe(icons.length)
  })
})

describe('isRoadmapLane', () => {
  it('is true for exactly the two wishlist statuses and nothing else in COLUMN_META', () => {
    const lanes = Object.keys(COLUMN_META).filter(isRoadmapLane)

    expect(lanes.sort()).toEqual([...ROADMAP_LANES].sort())
  })
})

// The drag matrix. This mirrors kanban_db.ROADMAP_LANE_TRANSITIONS, and it is
// the single predicate every move affordance filters on (lane drop, card
// context menu, drawer status picker), so a divergence here is a client that
// optimistically paints a move the backend answers with a 400.
describe('laneDropAllowed — the drag matrix', () => {
  const LIVE_STATUSES = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'on_hold', 'review', 'done']

  it('allows idea → roadmap (refine) and roadmap → idea (demote)', () => {
    expect(laneDropAllowed('idea', 'roadmap')).toBe(true)
    expect(laneDropAllowed('roadmap', 'idea')).toBe(true)
  })

  it('allows roadmap → triage and roadmap → ready (spawn)', () => {
    expect(laneDropAllowed('roadmap', 'triage')).toBe(true)
    expect(laneDropAllowed('roadmap', 'ready')).toBe(true)
  })

  it('allows archiving out of either lane', () => {
    expect(laneDropAllowed('idea', 'archived')).toBe(true)
    expect(laneDropAllowed('roadmap', 'archived')).toBe(true)
  })

  it('refuses idea → triage and idea → ready — an idea must be refined first', () => {
    expect(laneDropAllowed('idea', 'triage')).toBe(false)
    expect(laneDropAllowed('idea', 'ready')).toBe(false)
  })

  it.each(LIVE_STATUSES)('refuses %s → idea and %s → roadmap: nothing live may enter the wishlist', from => {
    expect(laneDropAllowed(from, 'idea')).toBe(false)
    expect(laneDropAllowed(from, 'roadmap')).toBe(false)
  })

  it('leaves moves between two live statuses alone', () => {
    for (const from of LIVE_STATUSES) {
      for (const to of LIVE_STATUSES) {
        expect(laneDropAllowed(from, to)).toBe(true)
      }
    }
  })

  it('agrees with ROADMAP_LANE_TRANSITIONS for every target the board can render', () => {
    const targets = [...Object.keys(COLUMN_META), 'some_future_status']

    for (const from of ROADMAP_LANES) {
      for (const to of targets) {
        expect(laneDropAllowed(from, to), `${from} -> ${to}`).toBe(ROADMAP_LANE_TRANSITIONS[from].has(to))
      }
    }
  })
})

describe('orderLanes', () => {
  it('moves the lanes leftmost in Ideas-then-Roadmap order, keeping the rest as the backend sent them', () => {
    // The backend's own BOARD_COLUMNS order: lanes trail the live columns.
    const backend = ['triage', 'todo', 'ready', 'running', 'done', 'idea', 'roadmap'].map(name => ({ name }))

    expect(orderLanes(backend).map(col => col.name)).toEqual([
      'idea',
      'roadmap',
      'triage',
      'todo',
      'ready',
      'running',
      'done'
    ])
  })

  it('is a no-op on a payload with no lane columns (older backend)', () => {
    const backend = ['triage', 'todo', 'ready'].map(name => ({ name }))

    expect(orderLanes(backend).map(col => col.name)).toEqual(['triage', 'todo', 'ready'])
  })

  it('does not mutate its input', () => {
    const backend = [{ name: 'triage' }, { name: 'idea' }]

    orderLanes(backend)

    expect(backend.map(col => col.name)).toEqual(['triage', 'idea'])
  })
})
