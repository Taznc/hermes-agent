/**
 * Focused tests for the task drawer's activity-run collapsing and run-history
 * error framing (Phase 2.16 polish card): `groupActivity`, `ActivityRow`
 * expand/collapse, `runErrorText`, and `RunErrorLine` expand/collapse.
 * Mirrors the mocking pattern in drawer.cta.test.tsx — usePluginI18n echoes
 * the dotted key, so assertions match on those keys instead of translated
 * English text.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ActivityRow, groupActivity, RunErrorLine, runErrorText } from './drawer'
import { useKanban } from './i18n'
import type { KanbanEvent } from './types'

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('@hermes/plugin-sdk')

  return { ...actual, usePluginI18n: () => (key: string) => key }
})

afterEach(() => {
  cleanup()
})

// A tiny host component so tests can call the real useKanban() hook (it's
// bound with useMemo, and drawer.tsx doesn't export it directly with a
// resolved value outside a component).
function withK(render: (k: ReturnType<typeof useKanban>) => ReactElement) {
  function Host() {
    const k = useKanban()

    return render(k)
  }

  return <Host />
}

const heartbeat = (id: number, secondsAgo = 0): KanbanEvent => ({
  id,
  kind: 'heartbeat',
  payload: null,
  created_at: Math.floor(Date.now() / 1000) - secondsAgo
})

const commentEvent = (id: number, author: string): KanbanEvent => ({
  id,
  kind: 'commented',
  payload: { author },
  created_at: Math.floor(Date.now() / 1000)
})

describe('groupActivity', () => {
  it('collapses consecutive identical events into one group', () => {
    const k = { evtCommentBy: (a: string) => `comment by ${a}` } as ReturnType<typeof useKanban>
    const events = [heartbeat(1), heartbeat(2), heartbeat(3)]
    const groups = groupActivity(events, k)

    expect(groups).toHaveLength(1)
    expect(groups[0].events).toHaveLength(3)
    expect(groups[0].label).toBe('heartbeat')
  })

  it('does not collapse across a different event in between', () => {
    const k = { evtCommentBy: (a: string) => `comment by ${a}` } as ReturnType<typeof useKanban>
    const events = [heartbeat(1), heartbeat(2), commentEvent(3, 'alice'), heartbeat(4)]
    const groups = groupActivity(events, k)

    expect(groups.map(g => g.events.length)).toEqual([2, 1, 1])
  })

  it('does not collapse same-kind events whose rendered detail differs', () => {
    const k = { evtCommentBy: (a: string) => `comment by ${a}` } as ReturnType<typeof useKanban>
    const events = [commentEvent(1, 'alice'), commentEvent(2, 'bob')]
    const groups = groupActivity(events, k)

    expect(groups).toHaveLength(2)
  })

  it('single events stay as their own one-item group', () => {
    const k = {} as ReturnType<typeof useKanban>
    const groups = groupActivity([heartbeat(1)], k)

    expect(groups).toHaveLength(1)
    expect(groups[0].events).toHaveLength(1)
  })
})

describe('ActivityRow', () => {
  it('a single-event group renders inline with no expand affordance', () => {
    render(<ul>{withK(k => <ActivityRow group={{ events: [heartbeat(1)], label: 'heartbeat' }} k={k} />)}</ul>)

    expect(screen.getByText('heartbeat')).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('a multi-event group renders collapsed by default, with a count summary', () => {
    const group = { events: [heartbeat(1, 300), heartbeat(2, 60), heartbeat(3, 46)], label: 'heartbeat' }
    render(<ul>{withK(k => <ActivityRow group={group} k={k} />)}</ul>)

    const toggle = screen.getByRole('button')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    // k.activityRun echoes as the dotted key under the i18n test mock.
    expect(screen.getByText('activityRun')).toBeTruthy()
  })

  it('expanding a collapsed group reveals every individual event', () => {
    const group = { events: [heartbeat(1, 300), heartbeat(2, 60), heartbeat(3, 46)], label: 'heartbeat' }
    render(<ul>{withK(k => <ActivityRow group={group} k={k} />)}</ul>)

    const toggle = screen.getByRole('button')
    fireEvent.click(toggle)

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getAllByText('heartbeat')).toHaveLength(3)
  })
})

describe('runErrorText', () => {
  const k = {
    runErrPidExited: (code: string) => `exited ${code}`,
    runErrPidNotAlive: 'not alive plain',
    runErrPidSignaled: (signal: string) => `signaled ${signal}`,
    runErrStaleLock: 'stale lock plain'
  } as ReturnType<typeof useKanban>

  it('recognizes stale_lock and frames it in plain language, keeping the raw string', () => {
    const result = runErrorText('stale_lock=hermes-dev:27237', k)
    expect(result.primary).toBe('stale lock plain')
    expect(result.raw).toBe('stale_lock=hermes-dev:27237')
  })

  it('recognizes "pid N not alive"', () => {
    const result = runErrorText('pid 111279 not alive', k)
    expect(result.primary).toBe('not alive plain')
    expect(result.raw).toBe('pid 111279 not alive')
  })

  it('recognizes "pid N exited with code C"', () => {
    const result = runErrorText('pid 42 exited with code 1', k)
    expect(result.primary).toBe('exited 1')
  })

  it('recognizes "pid N killed by signal S"', () => {
    const result = runErrorText('pid 42 killed by signal SIGKILL', k)
    expect(result.primary).toBe('signaled SIGKILL')
  })

  it('unrecognized shapes fall back to showing the raw string as primary, with no raw toggle', () => {
    const result = runErrorText('some unexpected worker error', k)
    expect(result.primary).toBe('some unexpected worker error')
    expect(result.raw).toBeUndefined()
  })
})

describe('RunErrorLine', () => {
  it('recognized diagnostics show plain language primary and hide the raw string until expanded', () => {
    render(withK(k => <RunErrorLine error="stale_lock=hermes-dev:27237" k={k} />))

    expect(screen.getByText('runErrStaleLock')).toBeTruthy()
    expect(screen.queryByText('stale_lock=hermes-dev:27237')).toBeNull()

    fireEvent.click(screen.getByRole('button'))
    expect(screen.getByText('stale_lock=hermes-dev:27237')).toBeTruthy()
  })

  it('unrecognized diagnostics show the raw string as primary with no expand toggle', () => {
    render(withK(k => <RunErrorLine error="some unexpected worker error" k={k} />))

    expect(screen.getByText('some unexpected worker error')).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
