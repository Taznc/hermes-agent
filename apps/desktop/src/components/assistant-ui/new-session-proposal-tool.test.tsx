import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { I18nProvider } from '@/i18n'
import { $gateway } from '@/store/gateway'
import {
  $newSessionProposalRequests,
  $startNewSessionFromTopic,
  setNewSessionProposalRequest
} from '@/store/new-session-proposal'

import { NewSessionProposalTool } from './new-session-proposal-tool'

// The pending card only needs to know whether the turn is still running; the
// settled path doesn't touch this at all. Mirrors clarify-tool.test.tsx.
let messageRunning = true

vi.mock('@assistant-ui/react', () => ({
  useAuiState: () => messageRunning
}))

afterEach(() => {
  cleanup()
  $newSessionProposalRequests.set({})
  $gateway.set(null)
  $startNewSessionFromTopic.set(null)
  messageRunning = true
  vi.clearAllMocks()
})

function tree(ui: ReactNode) {
  return (
    <I18nProvider configClient={null} initialLocale="en">
      {ui}
    </I18nProvider>
  )
}

function tileView(sessionId: string | null): SessionView {
  return { ...({} as SessionView), $runtimeId: atom<null | string>(sessionId), kind: 'tile' }
}

function pendingProps(toolCallId = 'propose-live'): ToolCallMessagePartProps {
  const args = { reason: 'Topic pivot detected', topic: 'Debug the flaky upload test' }

  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result: undefined,
    resume: vi.fn(),
    status: { type: 'running' },
    toolCallId,
    toolName: 'propose_new_session',
    type: 'tool-call'
  }
}

function settledProps(
  args: ToolCallMessagePartProps['args'],
  result: ToolCallMessagePartProps['result']
): ToolCallMessagePartProps {
  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result,
    resume: vi.fn(),
    status: { type: 'complete' },
    toolCallId: 'propose-settled',
    toolName: 'propose_new_session',
    type: 'tool-call'
  }
}

describe('NewSessionProposalTool pending card', () => {
  it('renders the topic and reason, buttons disabled until the request arrives', () => {
    render(
      tree(
        <SessionViewProvider value={tileView('session-1')}>
          <NewSessionProposalTool {...pendingProps()} />
        </SessionViewProvider>
      )
    )

    expect(screen.getByText(/Debug the flaky upload test/)).toBeTruthy()
    expect(screen.getByText(/Topic pivot detected/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Start new session' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Stay here' }).hasAttribute('disabled')).toBe(true)
  })

  it('falls back to the plain tool row when the turn stopped with no result', () => {
    messageRunning = false
    render(
      tree(
        <SessionViewProvider value={tileView('session-1')}>
          <NewSessionProposalTool {...pendingProps()} />
        </SessionViewProvider>
      )
    )

    expect(screen.queryByRole('button', { name: 'Start new session' })).toBeNull()
  })

  it('Approve routes through the canonical startNewSessionFromTopic pipeline', async () => {
    const startNewSessionFromTopic = vi.fn().mockResolvedValue(true)
    const request = vi.fn().mockResolvedValue({ ok: true })

    $gateway.set({ request } as never)
    $startNewSessionFromTopic.set(startNewSessionFromTopic)
    setNewSessionProposalRequest({
      reason: 'Topic pivot detected',
      requestId: 'request-1',
      sessionId: 'session-1',
      topic: 'Debug the flaky upload test'
    })

    render(
      tree(
        <SessionViewProvider value={tileView('session-1')}>
          <NewSessionProposalTool {...pendingProps()} />
        </SessionViewProvider>
      )
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Start new session' }))

    // Approve must go through the shared create -> publish -> prompt.submit
    // pipeline (bridged in from ContribWiring), never a raw session.create —
    // that never persists a row or submits the seeded topic as a real turn
    // (t_2023fb69 review round 1).
    await waitFor(() => {
      expect(startNewSessionFromTopic).toHaveBeenCalledWith('Debug the flaky upload test')
    })

    expect(request).not.toHaveBeenCalledWith('session.create', expect.anything())

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        'session.propose.respond',
        expect.objectContaining({
          request_id: 'request-1',
          result: JSON.stringify({ status: 'approved', topic: 'Debug the flaky upload test' })
        })
      )
    })
  })

  it('Approve surfaces a failure when startNewSessionFromTopic reports it did not submit', async () => {
    const startNewSessionFromTopic = vi.fn().mockResolvedValue(false)
    const request = vi.fn().mockResolvedValue({ ok: true })

    $gateway.set({ request } as never)
    $startNewSessionFromTopic.set(startNewSessionFromTopic)
    setNewSessionProposalRequest({
      reason: 'Topic pivot detected',
      requestId: 'request-1',
      sessionId: 'session-1',
      topic: 'Debug the flaky upload test'
    })

    render(
      tree(
        <SessionViewProvider value={tileView('session-1')}>
          <NewSessionProposalTool {...pendingProps()} />
        </SessionViewProvider>
      )
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Start new session' }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        'session.propose.respond',
        expect.objectContaining({
          request_id: 'request-1',
          result: expect.stringContaining('"status":"error"')
        })
      )
    })
  })


  it('Decline responds with declined and never calls session.create', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true })

    $gateway.set({ request } as never)
    setNewSessionProposalRequest({
      reason: 'Topic pivot detected',
      requestId: 'request-2',
      sessionId: 'session-1',
      topic: 'Debug the flaky upload test'
    })

    render(
      tree(
        <SessionViewProvider value={tileView('session-1')}>
          <NewSessionProposalTool {...pendingProps()} />
        </SessionViewProvider>
      )
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Stay here' }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        'session.propose.respond',
        expect.objectContaining({
          request_id: 'request-2',
          result: JSON.stringify({ status: 'declined', topic: 'Debug the flaky upload test' })
        })
      )
    })

    expect(request).not.toHaveBeenCalledWith('session.create', expect.anything())
  })
})

describe('NewSessionProposalTool settled card', () => {
  it('shows the approved outcome line and topic', () => {
    const args = { reason: 'Topic pivot detected', topic: 'Debug the flaky upload test' }
    const result = JSON.stringify({ status: 'approved', topic: args.topic })

    render(tree(<NewSessionProposalTool {...settledProps(args, result)} />))

    expect(screen.getByText('Started a new session')).toBeTruthy()
    expect(screen.getByText('Debug the flaky upload test')).toBeTruthy()
  })

  it('shows the declined outcome line', () => {
    const args = { reason: '', topic: 'Debug the flaky upload test' }
    const result = JSON.stringify({ status: 'declined', topic: args.topic })

    render(tree(<NewSessionProposalTool {...settledProps(args, result)} />))

    expect(screen.getByText('Declined')).toBeTruthy()
  })

  it('shows the unanswered outcome line on timeout', () => {
    const args = { reason: '', topic: 'Debug the flaky upload test' }
    const result = JSON.stringify({ status: 'unanswered' })

    render(tree(<NewSessionProposalTool {...settledProps(args, result)} />))

    expect(screen.getByText('No response')).toBeTruthy()
  })
})
