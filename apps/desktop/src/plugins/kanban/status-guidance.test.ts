/**
 * Focused tests for the pure status/cause resolver — the anchor bug fix
 * (t_44ca59a3: gave_up / last_failure_error='pid 704578 not alive' rendering
 * as "needs your input" / "did not record a reason"), the recency-ordered
 * precedence contract (NOT "manual always wins" — a card can be manually
 * blocked then auto-blocked, or the reverse), and the coverage contract that
 * every `COLUMN_META` status resolves to non-empty guidance.
 */
import { describe, expect, it } from 'vitest'

import { KANBAN_LOCALES } from './i18n'
import { resolveBlockCause, runErrorText, statusGuidance, type StatusGuidanceDeps } from './status-guidance'
import { COLUMN_META, type KanbanEvent, type KanbanRun, type KanbanTaskFull } from './types'

// Echoes the dotted key/args, mirroring the i18n test-mock convention used
// throughout this plugin (drawer.cta.test.tsx, drawer.activity-runs.test.tsx)
// so assertions read the resolver's OWN choice of key, not translated prose.
const k: StatusGuidanceDeps = {
  col: Object.fromEntries(Object.keys(COLUMN_META).map(id => [id, { help: `col.${id}.help` }])),
  guideAssignReady: 'guideAssignReady',
  guideBlockLoop: reason => `guideBlockLoop(${reason})`,
  guideBlockedAutomatic: cause => `guideBlockedAutomatic(${cause})`,
  guideBlockedGeneric: 'guideBlockedGeneric',
  guideBlockedManualCapability: 'guideBlockedManualCapability',
  guideBlockedManualTransient: 'guideBlockedManualTransient',
  guideBlockedReviewNoVerdict: 'guideBlockedReviewNoVerdict',
  guideBlockedReviewRoundCap: 'guideBlockedReviewRoundCap',
  guideBlockedUnknown: 'guideBlockedUnknown',
  guideDone: 'guideDone',
  guideIdea: 'guideIdea',
  guideOnHold: 'guideOnHold',
  guideReadyQueued: 'guideReadyQueued',
  guideReview: 'guideReview',
  guideRoadmap: 'guideRoadmap',
  guideRunning: 'guideRunning',
  guideRunningStale: 'guideRunningStale',
  guideScheduled: 'guideScheduled',
  guideTodo: 'guideTodo',
  guideTriage: 'guideTriage',
  runErrPidExited: code => `exited ${code}`,
  runErrPidNotAlive: 'not alive plain',
  runErrPidSignaled: signal => `signaled ${signal}`,
  runErrStaleLock: 'stale lock plain'
}

const baseTask = (overrides: Partial<KanbanTaskFull> = {}): KanbanTaskFull => ({
  id: 't_abc123',
  status: 'blocked',
  title: 'Some task',
  ...overrides
})

const event = (kind: string, payload: unknown = null, id = 1, created_at = 0): KanbanEvent => ({
  created_at,
  id,
  kind,
  payload
})

describe('resolveBlockCause — the anchor bug (t_44ca59a3)', () => {
  it('a gave_up event with no blocked/block_loop_detected event is read as automatic, not manual', () => {
    const task = baseTask({ block_kind: null, last_failure_error: 'pid 704578 not alive' })

    const events = [
      event('crashed', { error: 'pid 704578 not alive' }),
      event('gave_up', { error: 'pid 704578 not alive' })
    ]

    const cause = resolveBlockCause(task, events, [])

    expect(cause).toEqual({ origin: 'automatic', raw: 'pid 704578 not alive' })
  })

  it('runErrorText humanizes the automatic cause into an intelligible message', () => {
    const { primary } = runErrorText('pid 704578 not alive', k)

    expect(primary).toBe('not alive plain')
  })

  it('statusGuidance for the anchor card never reads as a missing reason', () => {
    const task = baseTask({ block_kind: null, last_failure_error: 'pid 704578 not alive' })

    const events = [
      event('crashed', { error: 'pid 704578 not alive' }),
      event('gave_up', { error: 'pid 704578 not alive' })
    ]

    const guidance = statusGuidance('blocked', task, events, [], k)

    expect(guidance).toBe('guideBlockedAutomatic(not alive plain)')
    expect(guidance).not.toContain('guideBlockedGeneric')
  })
})

describe('resolveBlockCause — precedence is recency-ordered, not "manual always wins"', () => {
  it('manual block AFTER an earlier gave_up resolves to manual', () => {
    const task = baseTask({ block_kind: 'needs_input' })
    const events = [event('gave_up', { error: 'pid 1 not alive' }), event('blocked', { reason: 'Which key?' })]

    expect(resolveBlockCause(task, events, [])).toEqual({ kind: 'needs_input', origin: 'manual', reason: 'Which key?' })
  })

  it('manual block, unblocked, then a later gave_up resolves to automatic', () => {
    const task = baseTask({ block_kind: 'needs_input', last_failure_error: null })

    const events = [
      event('blocked', { reason: 'Which key?' }),
      event('unblocked'),
      event('gave_up', { error: 'pid 2 not alive' })
    ]

    expect(resolveBlockCause(task, events, [])).toEqual({ origin: 'automatic', raw: 'pid 2 not alive' })
  })
})

describe('resolveBlockCause — review_no_verdict is neutral, never a question', () => {
  it('resolves to review_no_verdict, not manual/automatic', () => {
    const task = baseTask({ block_kind: null })
    const events = [event('review_no_verdict', { pid: 123 })]

    expect(resolveBlockCause(task, events, [])).toEqual({ origin: 'review_no_verdict' })
  })
})

describe('resolveBlockCause — fallback chain', () => {
  it('falls back to task.last_failure_error when no relevant event exists', () => {
    const task = baseTask({ last_failure_error: 'pid 9 not alive' })

    expect(resolveBlockCause(task, [], [])).toEqual({ origin: 'automatic', raw: 'pid 9 not alive' })
  })

  it('falls back to the newest failed run error when events and last_failure_error are both empty', () => {
    const task = baseTask({ last_failure_error: null })

    const runs: KanbanRun[] = [
      { id: 1, status: 'gave_up', error: 'older error' },
      { id: 2, status: 'gave_up', error: 'newest error' }
    ]

    expect(resolveBlockCause(task, [], runs)).toEqual({ origin: 'automatic', raw: 'newest error' })
  })

  it('never fabricates a cause: empty everywhere resolves to unknown', () => {
    const task = baseTask({ last_failure_error: null })

    expect(resolveBlockCause(task, [], [])).toEqual({ origin: 'unknown' })
  })
})

describe('resolveBlockCause — the dispatcher review-round cap (t_583024aa)', () => {
  // The live card that exposed the defect: block_kind='review_round_cap' with a
  // review_round_cap event. Before the fix this event kind was absent from
  // CAUSE_EVENT_KINDS, so the scan fell through to `unknown` and the banner
  // claimed "no cause is recorded" while the diagnostics panel below it printed
  // the exact cause.
  const capEvent = (payload: unknown = { changes_rounds: 2, max_review_rounds: 2, reason: 'Round 2 still fails six safety requirements' }) =>
    event('review_round_cap', payload)

  it('resolves the cap event to its own origin, never to unknown', () => {
    const task = baseTask({ block_kind: 'review_round_cap', last_failure_error: null })

    const cause = resolveBlockCause(task, [capEvent()], [])

    expect(cause.origin).toBe('review_round_cap')
    expect(cause).not.toEqual({ origin: 'unknown' })
  })

  it('carries the round counts and the reviewer reason off the payload', () => {
    const task = baseTask({ block_kind: 'review_round_cap', last_failure_error: null })

    expect(resolveBlockCause(task, [capEvent()], [])).toEqual({
      max: 2,
      origin: 'review_round_cap',
      reason: 'Round 2 still fails six safety requirements',
      rounds: 2
    })
  })

  it('a payload missing the counts still resolves as the cap, with nulls — never unknown', () => {
    const task = baseTask({ block_kind: 'review_round_cap', last_failure_error: null })

    expect(resolveBlockCause(task, [capEvent({})], [])).toEqual({
      max: null,
      origin: 'review_round_cap',
      reason: null,
      rounds: null
    })
  })

  it('a dispatcher-written block_kind is never cast into a manual BlockKind label', () => {
    // `review_round_cap` is written by a raw UPDATE that bypasses
    // VALID_BLOCK_KINDS, so it must not reach `blockKind[kind]` (which would
    // index to undefined and render an empty banner title).
    const task = baseTask({ block_kind: 'review_round_cap' })

    const cause = resolveBlockCause(task, [event('blocked', { reason: 'manual words' })], [])

    expect(cause).toEqual({ kind: null, origin: 'manual', reason: 'manual words' })
  })

  it('guidance points at an intervention, not a bare retry into the same loop', () => {
    const task = baseTask({ block_kind: 'review_round_cap', last_failure_error: null })

    const guidance = statusGuidance('blocked', task, [capEvent()], [], k)

    expect(guidance).toBe('guideBlockedReviewRoundCap')
    expect(guidance).not.toBe('guideBlockedUnknown')
    expect(guidance).not.toBe('guideBlockedGeneric')
  })

  it('every locale defines the round-cap copy and never reuses the no-cause banner body', () => {
    for (const [locale, bundle] of Object.entries(KANBAN_LOCALES)) {
      expect(bundle.guideBlockedReviewRoundCap, `locale "${locale}"`).toBeTruthy()
      expect(bundle.ctaReviewRoundCapBody, `locale "${locale}"`).toBeTruthy()
      expect(bundle.ctaReviewRoundCapBody, `locale "${locale}"`).not.toBe(bundle.ctaBlockedNoReason)
      expect(bundle.evtReviewRoundCap, `locale "${locale}"`).toBeTruthy()
    }
  })
})

describe('statusGuidance — blocked with unknown cause is honest, never a fabricated question', () => {
  it('a blocked task with no cause anywhere resolves to the unknown-cause copy, not guideBlockedGeneric', () => {
    const task = baseTask({ block_kind: null, last_failure_error: null })

    const guidance = statusGuidance('blocked', task, [], [], k)

    expect(guidance).toBe('guideBlockedUnknown')
    expect(guidance).not.toBe('guideBlockedGeneric')
  })

  it('guideBlockedUnknown is a distinct next-action line in every locale, never a copy of the banner body (ctaBlockedNoReason) — the banner already states the diagnosis, this line must state the action', () => {
    for (const [locale, bundle] of Object.entries(KANBAN_LOCALES)) {
      expect(bundle.guideBlockedUnknown, `locale "${locale}"`).not.toBe(bundle.ctaBlockedNoReason)
    }
  })
})

describe('statusGuidance — a manual block never echoes the raw reason (defect: choices-fence dump)', () => {
  it('a manual block with a plain-text reason gets next-action guidance, not the reason text', () => {
    const task = baseTask({ block_kind: 'needs_input' })
    const events = [event('blocked', { reason: 'Which key should we use?' })]

    const guidance = statusGuidance('blocked', task, events, [], k)

    expect(guidance).toBe('guideBlockedGeneric')
    expect(guidance).not.toContain('Which key should we use?')
  })

  it('a manual block whose reason carries a ```choices fence never leaks the fence or its JSON', () => {
    const task = baseTask({ block_kind: 'needs_input' })

    const reason =
      'Pick a path forward:\n\n```choices\n[\n  {"key": "A", "label": "Option A - do the thing"},\n  {"key": "B", "label": "Option B - do the other thing"}\n]\n```'

    const events = [event('blocked', { reason })]

    const guidance = statusGuidance('blocked', task, events, [], k)

    expect(guidance).not.toContain('```')
    expect(guidance).not.toContain('"key"')
    expect(guidance).not.toContain('Option A')
  })

  it('a manual block with an empty reason still gets sensible reply/unblock guidance (no regression)', () => {
    const task = baseTask({ block_kind: null })
    const events = [event('blocked', { reason: '' })]

    expect(statusGuidance('blocked', task, events, [], k)).toBe('guideBlockedGeneric')
  })

  it('a manual block with block_kind "capability" gets the capability-specific next action, not "needs your input"', () => {
    const task = baseTask({ block_kind: 'capability' })
    const events = [event('blocked', { reason: 'Missing an API key for this provider.' })]

    const guidance = statusGuidance('blocked', task, events, [], k)

    expect(guidance).toBe('guideBlockedManualCapability')
    expect(guidance).not.toBe('guideBlockedGeneric')
  })

  it('a manual block with block_kind "transient" gets the transient-specific next action, not "needs your input"', () => {
    const task = baseTask({ block_kind: 'transient' })
    const events = [event('blocked', { reason: 'A flaky network call failed.' })]

    const guidance = statusGuidance('blocked', task, events, [], k)

    expect(guidance).toBe('guideBlockedManualTransient')
    expect(guidance).not.toBe('guideBlockedGeneric')
  })
})

describe('statusGuidance — coverage contract', () => {
  it('every COLUMN_META status resolves to non-empty guidance', () => {
    for (const status of Object.keys(COLUMN_META)) {
      const task = baseTask({ assignee: 'someone', status })
      const guidance = statusGuidance(status, task, [], [], k)

      expect(guidance, `status "${status}" must have non-empty guidance`).toBeTruthy()
    }
  })

  it('ready without an assignee tells the user to assign it', () => {
    const task = baseTask({ assignee: null, status: 'ready' })

    expect(statusGuidance('ready', task, [], [], k)).toBe('guideAssignReady')
  })

  it('ready with an assignee reads as queued, not a call to action', () => {
    const task = baseTask({ assignee: 'alice', status: 'ready' })

    expect(statusGuidance('ready', task, [], [], k)).toBe('guideReadyQueued')
  })

  it('running with a stale heartbeat differs from a live running task', () => {
    const live = baseTask({ last_heartbeat_at: Date.now() / 1000, status: 'running' })
    const stale = baseTask({ last_heartbeat_at: Date.now() / 1000 - 300, status: 'running' })

    expect(statusGuidance('running', live, [], [], k)).toBe('guideRunning')
    expect(statusGuidance('running', stale, [], [], k)).toBe('guideRunningStale')
  })

  it('triage surfaces the actual block_loop_detected reason when present', () => {
    const task = baseTask({ status: 'triage' })
    const events = [event('block_loop_detected', { reason: 'same cause 3x' })]

    expect(statusGuidance('triage', task, events, [], k)).toBe('guideBlockLoop(same cause 3x)')
  })

  it('idea and roadmap resolve to their own calm guidance, not a column-help fallback', () => {
    expect(statusGuidance('idea', baseTask({ status: 'idea' }), [], [], k)).toBe('guideIdea')
    expect(statusGuidance('roadmap', baseTask({ status: 'roadmap' }), [], [], k)).toBe('guideRoadmap')
  })

  it('a lane card is never nagged about staleness, block history, or a missing assignee', () => {
    const noisy = {
      assignee: null,
      block_kind: 'needs_input' as const,
      last_failure_error: 'pid 704578 not alive',
      last_heartbeat_at: 0
    }

    const events = [event('block_loop_detected', { reason: 'same cause 3x' }), event('gave_up', { error: 'boom' })]

    expect(statusGuidance('idea', baseTask({ ...noisy, status: 'idea' }), events, [], k)).toBe('guideIdea')
    expect(statusGuidance('roadmap', baseTask({ ...noisy, status: 'roadmap' }), events, [], k)).toBe('guideRoadmap')
  })

  it('an unknown backend status falls back to its column help', () => {
    const task = baseTask({ status: 'some_future_status' })

    expect(statusGuidance('some_future_status', task, [], [], k)).toBe('')
  })
})
