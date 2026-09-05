// Render-isolation contract for the sidebar decomposition (t_4d2f3e30):
// a dot-state tick must re-render only the section it actually affects, not
// a sibling section mounted alongside it. `SidebarSessionsSection`'s
// WORKING/DONE status grouping reads `$liveTurnSessionIds`
// (store/session-dot-state.ts) — a stableArray-guarded projection off the
// same `$sessionDotStateById` map the audit named as the near-continuous-tick
// source. This proves the section re-renders when that projection's
// membership actually flips, and — the property the decomposition exists
// for — an unrelated sibling mounted next to it in the same subtree does not.
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $sessions } from '@/store/session'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { SidebarSessionsSection } from './sessions-section'

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  $sessions.set([])
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {
          earlierThisMonth: 'Earlier this month',
          lastMonth: 'Last month',
          lastWeek: 'Last week',
          older: 'Older',
          today: 'Today',
          yesterday: 'Yesterday'
        },
        projects: {
          toggle: (label: string, open: boolean) => `${open ? 'Hide' : 'Show'} ${label} sessions`
        },
        statusDivider: { done: 'Done', working: 'Working' }
      }
    }
  })
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: ({ session }: { session: SessionInfo }) => (
    <div data-testid={`session-row-${session.id}`}>{session.id}</div>
  )
}))

const row = (id: string, startedAt = 1000): SessionInfo =>
  ({
    handoff_platform: null,
    handoff_state: null,
    id,
    last_active: startedAt,
    profile: 'default',
    started_at: startedAt
  }) as unknown as SessionInfo

const noop = () => {}

/** A sibling with no store subscriptions at all — the sidebar's OTHER
 *  sections in production (Pinned, Messaging, Cron). If the tree re-renders
 *  it purely because a sibling's store ticked, this counter goes up; the
 *  whole point of the decomposition is that it must not. */
function Sibling({ onRender }: { onRender: () => void }) {
  onRender()

  return <div data-testid="sibling">unaffected</div>
}

describe('sidebar section render isolation (dot-state tick)', () => {
  it('a live-turn dot-state flip re-renders the sessions section but not a sibling', () => {
    $sessions.set([row('s1'), row('s2')])

    let siblingRenders = 0

    function Harness() {
      return (
        <div>
          <SidebarSessionsSection
            activeSessionId={null}
            emptyState={<div>Empty</div>}
            grouping="status"
            label="Sessions"
            onArchiveSession={noop}
            onDeleteSession={noop}
            onResumeSession={noop}
            onToggle={noop}
            onTogglePin={noop}
            onToggleUnread={noop}
            open
            pinned={false}
            sessions={[row('s1'), row('s2')]}
          />
          <Sibling onRender={() => (siblingRenders += 1)} />
        </div>
      )
    }

    const { container } = render(<Harness />)

    // Both start idle: no WORKING divider yet.
    expect(container.textContent).not.toContain('Working')
    const siblingRendersAfterMount = siblingRenders

    // Flip s1's dot-state to a live turn (working) — the exact edge
    // $liveTurnSessionIds exists to isolate. This does NOT touch $sessions,
    // props, or anything the Harness/Sibling themselves read — no explicit
    // `rerender()` call here, which would force React to reconcile the WHOLE
    // tree (including Sibling) regardless of what changed. The section's own
    // `useStore($liveTurnSessionIds)` subscription is what must drive this.
    act(() => {
      publishSessionState('rt-1', { ...createClientSessionState('s1'), busy: true })
    })

    // The section actually reacted: s1 now buckets under the WORKING divider.
    expect(container.textContent).toContain('Working')
    // The unrelated sibling did not re-render for this store's tick.
    expect(siblingRenders).toBe(siblingRendersAfterMount)
  })

  it('a second flip that does not change hasLiveTurn membership keeps the sibling untouched too', () => {
    $sessions.set([row('s1')])
    publishSessionState('rt-1', { ...createClientSessionState('s1'), busy: true })

    let siblingRenders = 0

    function Harness() {
      return (
        <div>
          <SidebarSessionsSection
            activeSessionId={null}
            emptyState={<div>Empty</div>}
            grouping="status"
            label="Sessions"
            onArchiveSession={noop}
            onDeleteSession={noop}
            onResumeSession={noop}
            onToggle={noop}
            onTogglePin={noop}
            onToggleUnread={noop}
            open
            pinned={false}
            sessions={[row('s1')]}
          />
          <Sibling onRender={() => (siblingRenders += 1)} />
        </div>
      )
    }

    render(<Harness />)
    const after = siblingRenders

    // Re-publish the SAME busy=true state — a no-op transition. The dot-state
    // computed store re-notifies its own listeners only on a reference
    // change (stableArray in $liveTurnSessionIds), so this must not cascade
    // anywhere, sibling included.
    act(() => {
      publishSessionState('rt-1', { ...createClientSessionState('s1'), busy: true })
    })

    expect(siblingRenders).toBe(after)
  })
})
