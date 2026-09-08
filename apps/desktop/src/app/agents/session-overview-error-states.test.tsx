import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type { AgentOverview, OverviewSession } from '@/global'
import { mergeOverview, overviewCache } from '@/store/agent-overview'

import { SessionOverview } from './sessions'

function snapshot(sessions: OverviewSession[]): AgentOverview {
  return {
    fetchedAt: Date.now(),
    sources: [
      {
        connectionId: 'hq',
        label: 'HQ source',
        kind: 'remote',
        state: 'ready',
        sessions: [],
        canonical: [],
        live: sessions,
        profiles: [],
        total: sessions.length,
        offset: sessions.length,
        complete: true,
        liveCoverage: 'process',
        errors: []
      }
    ]
  }
}

function session(id: string, extra: Partial<OverviewSession> = {}): OverviewSession {
  return { id, profile: 'coder', title: id, last_active: Date.now() / 1000, ...extra }
}

async function mount() {
  // The real watch()/refresh() cycle would immediately overwrite the
  // hand-set fixture state with a live (mocked) fetch result; these tests
  // are about presentation of a given cache state, not the fetch cycle
  // (that is activity.test.tsx's job), so stub watch() to a no-op subscribe.
  vi.spyOn(overviewCache, 'watch').mockReturnValue(() => {})

  const view = render(
    <PaneVisibleContext.Provider value={true}>
      <SessionOverview />
    </PaneVisibleContext.Provider>
  )

  await act(async () => {})

  return view
}

afterEach(() => {
  cleanup()
  overviewCache.state.set({ loading: false, error: null })
  vi.restoreAllMocks()
})

// Acceptance criterion 1: a hard error (no retained rows) replaces the whole
// surface — no "All quiet", no list surface, no fresh-looking coverage strip.
it('renders only the error and Retry for a hard error with no retained rows', async () => {
  overviewCache.state.set({ data: undefined, loading: false, error: 'Agent overview is unavailable.' })
  await mount()
  expect(screen.getByText('Agent overview is unavailable.')).toBeTruthy()
  expect(screen.getByTestId('agent-retry')).toBeTruthy()
  expect(screen.queryByText('All quiet')).toBeNull()
  expect(screen.queryAllByTestId('agent-row')).toHaveLength(0)
  expect(screen.queryByTestId('agent-source-coverage')).toBeNull()
})

// Acceptance criterion 2: a stale-but-populated snapshot keeps its rows (with
// their existing stale labeling) instead of blanking the panel on error.
it('keeps rows with stale labeling visible under a stale error, not a hard blank', async () => {
  const fresh = mergeOverview(undefined, snapshot([session('kept', { status: 'idle' })]))

  const outage = mergeOverview(fresh, {
    fetchedAt: Date.now(),
    sources: [{ ...fresh.sources[0]!, state: 'offline', complete: false, live: [] }]
  })

  overviewCache.state.set({
    data: outage,
    loading: false,
    error: 'Agent overview read timed out. Retry to reconnect.'
  })
  await mount()
  expect(screen.getByTestId('agent-row')).toBeTruthy()
  expect(screen.getByText(/^Stale/)).toBeTruthy()
  expect(screen.getByText('Agent overview read timed out. Retry to reconnect.')).toBeTruthy()
  expect(screen.getByTestId('agent-retry')).toBeTruthy()
  // Not the full-panel hard-error presentation (its title reads "Offline" as
  // an <h2>; the source-coverage footer legitimately shows "Offline" too,
  // since this source really did go offline — that is the honest-degraded
  // footer behavior, not the thing under test here).
  expect(screen.queryByRole('heading', { name: 'Offline' })).toBeNull()
})

// Acceptance criterion 3 (regression guard alongside index.test.tsx/activity.test.tsx):
// a healthy, genuinely empty result shows "All quiet" and no error block.
it('shows All quiet with no error block for a healthy empty result', async () => {
  overviewCache.state.set({ data: mergeOverview(undefined, snapshot([])), loading: false, error: null })
  await mount()
  expect(screen.getByText('All quiet')).toBeTruthy()
  expect(screen.queryByTestId('agent-retry')).toBeNull()
  expect(screen.queryByText('Offline')).toBeNull()
})

// Acceptance criterion 4: Retry reads as a normal button, asserted via role +
// a stable data-* hook rather than a CSS class string.
it('exposes Retry as a real button via a stable data hook', async () => {
  overviewCache.state.set({ data: undefined, loading: false, error: 'boom' })
  await mount()
  const retry = screen.getByTestId('agent-retry')
  expect(retry.tagName).toBe('BUTTON')
  expect(screen.getByRole('button', { name: 'Retry' })).toBe(retry)
})
