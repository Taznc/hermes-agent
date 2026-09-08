import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { clearClarifyRequest } from '@/store/clarify'
import { clearAllPrompts, setApprovalRequest, setSudoRequest } from '@/store/prompts'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

// The sidebar's "needs input" glyph must stay lit for as long as the session
// has ANY blocking prompt parked on it. Two handlers used to clear the flag on
// the assumption that the next tool.complete / clarify.expire after a block IS
// that block resolving — only true when exactly one prompt is ever in flight.
// A turn can hold a live approval bar while an unrelated background tool
// finishes, and that tool.complete wiped the indicator out from under a prompt
// still waiting on the user.

const SID = 'session-1'

let stream: MessageStreamHarness

const seedNeedsInput = () => {
  const state = createClientSessionState()
  state.needsInput = true
  stream.states.set(SID, state)
}

const toolComplete = (payload: Record<string, unknown>) =>
  act(() => stream.handleEvent({ payload, session_id: SID, type: 'tool.complete' }))

const clarifyRequest = (payload: Record<string, unknown>) =>
  act(() => stream.handleEvent({ payload, session_id: SID, type: 'clarify.request' }))

const clarifyExpire = (requestId: string) =>
  act(() => stream.handleEvent({ payload: { request_id: requestId }, session_id: SID, type: 'clarify.expire' }))

describe('needsInput survives events unrelated to the pending prompt', () => {
  beforeEach(() => {
    clearAllPrompts()
    clearClarifyRequest()
    stream = renderMessageStream(SID)
  })

  afterEach(() => {
    cleanup()
    clearAllPrompts()
    clearClarifyRequest()
  })

  it('keeps needsInput while an approval is pending and a background tool completes', () => {
    seedNeedsInput()
    setApprovalRequest({ command: 'sleep 240', description: 'dangerous command', requestId: 'ap-1', sessionId: SID })

    // A concurrent background process finishing has nothing to do with the
    // approval bar the user is still looking at.
    toolComplete({ name: 'terminal', tool_id: 'bg-1' })

    expect(stream.state().needsInput).toBe(true)
  })

  it('keeps needsInput when a clarify expires while an approval is still pending', () => {
    clarifyRequest({ choices: ['a', 'b'], question: 'Pick', request_id: 'req-1' })
    setApprovalRequest({
      command: 'rm -rf /tmp/x',
      description: 'dangerous command',
      requestId: 'ap-2',
      sessionId: SID
    })

    clarifyExpire('req-1')

    expect(stream.state().needsInput).toBe(true)
  })

  it('keeps needsInput when a clarify expires while a sudo prompt is still pending', () => {
    clarifyRequest({ choices: ['a', 'b'], question: 'Pick', request_id: 'req-2' })
    setSudoRequest({ requestId: 'sudo-1', sessionId: SID })

    clarifyExpire('req-2')

    expect(stream.state().needsInput).toBe(true)
  })

  it('still clears needsInput on tool.complete when nothing else is pending', () => {
    seedNeedsInput()

    toolComplete({ name: 'terminal', tool_id: 'bg-2' })

    expect(stream.state().needsInput).toBe(false)
  })

  it('still clears needsInput on clarify.expire when nothing else is pending', () => {
    clarifyRequest({ choices: ['a', 'b'], question: 'Pick', request_id: 'req-3' })

    expect(stream.state().needsInput).toBe(true)

    clarifyExpire('req-3')

    expect(stream.state().needsInput).toBe(false)
  })

  it('ignores another session\u2019s blocking prompt', () => {
    seedNeedsInput()
    setApprovalRequest({
      command: 'sleep 240',
      description: 'dangerous command',
      requestId: 'ap-other',
      sessionId: 'session-other'
    })

    toolComplete({ name: 'terminal', tool_id: 'bg-3' })

    expect(stream.state().needsInput).toBe(false)
  })
})
