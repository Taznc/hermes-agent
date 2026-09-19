import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import * as gateway from '@/store/gateway'
import { _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { clearAllSessionStates, publishSessionState } from '@/store/session-states'
import { $subagentsBySession, upsertSubagent } from '@/store/subagents'

import { AgentsView } from './index'

Element.prototype.animate = vi.fn(() => ({ cancel() {} }) as Animation)

afterEach(() => {
  cleanup()
  clearAllSessionStates()
  $subagentsBySession.set({})
  _resetSessionOwnerHintsForTests()
  vi.restoreAllMocks()
})

it('recovers background children and the Working parent with no composer mounted', async () => {
  window.hermesDesktop = {
    ...window.hermesDesktop,
    getAgentOverview: vi.fn(async () => ({
      fetchedAt: Date.now(),
      sources: [
        {
          connectionId: 'remote',
          label: 'Lab',
          kind: 'remote',
          state: 'ready',
          sessions: [{ id: 'parent-stored', profile: 'coder', title: 'Parent', last_active: Date.now() / 1000 }],
          live: [{ id: 'parent-stored', profile: 'coder', runtime_id: 'parent-runtime', status: 'idle' }],
          canonical: [],
          profiles: [],
          total: 1,
          offset: 1,
          complete: true,
          liveCoverage: 'process',
          errors: []
        }
      ]
    }))
  } as typeof window.hermesDesktop
  vi.spyOn(gateway, 'isGatewayOpenForAgent').mockReturnValue(true)

  const request = vi.spyOn(gateway, 'requestGatewayForAgent').mockResolvedValue({
    subagents: [{ subagent_id: 'child', goal: 'Recovered background work', status: 'running', started_at: 1000 }]
  } as never)

  const ownerRoute = { connectionId: 'remote', profile: 'coder' }
  setSessionOwnerHint('parent-stored', ownerRoute)
  publishSessionState('parent-runtime', { ...createClientSessionState('parent-stored'), ownerRoute, busy: false })
  render(<AgentsView onClose={vi.fn()} />)
  await waitFor(() => expect($subagentsBySession.get()['parent-runtime']?.[0]?.status).toBe('running'))
  await waitFor(() => expect(screen.getByText('1 working')).toBeTruthy())
  expect(request).toHaveBeenCalledWith('remote', 'coder', 'subagent.list', { session_id: 'parent-runtime' })
  fireEvent.click(screen.getByRole('button', { name: 'Spawn tree' }))
  expect(screen.getByText('Recovered background work')).toBeTruthy()
  expect(screen.queryByText('No live subagents')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Sessions' }))
  act(() => upsertSubagent('parent-runtime', { subagent_id: 'child', status: 'completed' }, false, 'subagent.complete'))
  await waitFor(() => expect(screen.getByText('All quiet')).toBeTruthy())
})
