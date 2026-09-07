import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $clarifyRequest,
  $clarifyRequests,
  $settledClarifyHelp,
  associateClarifyToolRequest,
  type ClarifyRequest,
  clearClarifyRequest,
  hasClarifyRequest,
  normalizeChoices,
  normalizeQuestions,
  reconcileClarifyHelp,
  setClarifyRequest,
  settledClarifyHelpForToolCall,
  skipClarifyRequest,
  updateClarifyHelp
} from './clarify'
import { $gateway } from './gateway'
import { $activeSessionId } from './session'

function clarify(sessionId: string | null, requestId: string): ClarifyRequest {
  return {
    requestId,
    question: `question-${requestId}`,
    choices: null,
    multiSelect: false,
    sessionId
  }
}

describe('clarify store', () => {
  beforeEach(() => {
    $clarifyRequests.set({})
    $settledClarifyHelp.set({})
    $activeSessionId.set(null)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $settledClarifyHelp.set({})
    $activeSessionId.set(null)
  })

  it('keeps clarify requests from concurrent sessions independent', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a')
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('exposes only the active session via the focus-scoped view', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    $activeSessionId.set('session-a')
    expect($clarifyRequest.get()?.requestId).toBe('req-a')

    $activeSessionId.set('session-b')
    expect($clarifyRequest.get()?.requestId).toBe('req-b')

    $activeSessionId.set('session-c')
    expect($clarifyRequest.get()).toBeNull()
  })

  it('clears only the targeted session, leaving the other pending', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    clearClarifyRequest('req-a', 'session-a')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('ignores a stale clear whose request id no longer matches', () => {
    setClarifyRequest(clarify('session-a', 'req-a2'))

    clearClarifyRequest('req-a1', 'session-a')

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a2')
  })

  it('clears by request id across sessions when no session hint is given', () => {
    setClarifyRequest(clarify('session-a', 'shared'))
    setClarifyRequest(clarify('session-b', 'other'))

    clearClarifyRequest('shared')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('other')
  })

  it('reconciles an optimistic help entry with its event-first explanation id without leaving a loader', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    updateClarifyHelp('req-a', 'session-a', 'local-1', {
      choice: 'staging',
      followUp: 'Why staging?',
      status: 'loading'
    })
    // The gateway publishes this event before returning explanation_id to the RPC caller.
    updateClarifyHelp('req-a', 'session-a', 'explain-1', {
      choice: 'staging',
      content: 'It limits blast radius.',
      followUp: '',
      status: 'complete'
    })

    reconcileClarifyHelp('req-a', 'session-a', 'local-1', 'explain-1')

    expect($clarifyRequests.get()['session-a']?.help).toEqual({
      'explain-1': expect.objectContaining({
        choice: 'staging',
        content: 'It limits blast radius.',
        followUp: 'Why staging?',
        status: 'complete'
      })
    })
  })

  it('keeps out-of-order repeated help responses correlated to their own follow-ups', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    updateClarifyHelp('req-a', 'session-a', 'local-old', { choice: 'staging', followUp: 'old question', status: 'loading' })
    updateClarifyHelp('req-a', 'session-a', 'local-new', { choice: 'staging', followUp: 'new question', status: 'loading' })
    // A newer request completes first. Its content must not acquire the old request's metadata.
    updateClarifyHelp('req-a', 'session-a', 'explain-new', { choice: 'staging', content: 'new answer', followUp: '', status: 'complete' })
    reconcileClarifyHelp('req-a', 'session-a', 'local-new', 'explain-new')
    updateClarifyHelp('req-a', 'session-a', 'explain-old', { choice: 'staging', content: 'old answer', followUp: '', status: 'complete' })
    reconcileClarifyHelp('req-a', 'session-a', 'local-old', 'explain-old')

    expect($clarifyRequests.get()['session-a']?.help).toEqual(expect.objectContaining({
      'explain-new': expect.objectContaining({ content: 'new answer', followUp: 'new question' }),
      'explain-old': expect.objectContaining({ content: 'old answer', followUp: 'old question' })
    }))
  })

  it('retains help for the exact tool row after its pending request settles', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    associateClarifyToolRequest('tool-a', 'req-a')
    updateClarifyHelp('req-a', 'session-a', 'explain-a', {
      content: 'Staging is safer for validation.',
      followUp: '',
      status: 'complete'
    })

    clearClarifyRequest('req-a', 'session-a')

    expect(settledClarifyHelpForToolCall('tool-a')).toEqual({
      'explain-a': expect.objectContaining({ content: 'Staging is safer for validation.' })
    })
  })
})

describe('skipClarifyRequest', () => {
  const request = vi.fn(async () => ({ ok: true }))

  beforeEach(() => {
    $clarifyRequests.set({})
    request.mockClear()
    $gateway.set({ request } as unknown as ReturnType<typeof $gateway.get>)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $gateway.set(null)
  })

  it('answers the session\u2019s clarify with an empty answer and drops it', async () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    await expect(skipClarifyRequest('session-a')).resolves.toBe(true)

    expect(request).toHaveBeenCalledWith('clarify.respond', { request_id: 'req-a', answer: '' })
    expect(hasClarifyRequest('session-a')).toBe(false)
    // A background session's question is untouched — only the one being typed
    // over is skipped.
    expect(hasClarifyRequest('session-b')).toBe(true)
  })

  it('is a no-op when the session has no clarify parked', async () => {
    await expect(skipClarifyRequest('session-a')).resolves.toBe(false)
    expect(request).not.toHaveBeenCalled()
  })

  it('still reports the skip when the respond RPC fails', async () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    request.mockRejectedValueOnce(new Error('socket closed'))

    await expect(skipClarifyRequest('session-a')).resolves.toBe(true)
    expect(hasClarifyRequest('session-a')).toBe(false)
  })
})

describe('normalizeChoices', () => {
  it('returns empty array for null/undefined', () => {
    expect(normalizeChoices(null)).toEqual([])
    expect(normalizeChoices(undefined)).toEqual([])
  })

  it('returns empty array for non-array input', () => {
    expect(normalizeChoices('hello')).toEqual([])
    expect(normalizeChoices(42)).toEqual([])
    expect(normalizeChoices({})).toEqual([])
  })

  it('filters out non-string items', () => {
    expect(normalizeChoices(['a', 42, 'b', null, 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops blank and whitespace-only strings', () => {
    expect(normalizeChoices(['a', '', 'b', '   ', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops strings with newlines', () => {
    expect(normalizeChoices(['a', 'b\nc', 'd'])).toEqual(['a', 'd'])
  })

  it('drops strings over 200 chars', () => {
    const long = 'x'.repeat(201)
    const ok = 'y'.repeat(200)
    expect(normalizeChoices(['a', long, ok])).toEqual(['a', ok])
  })

  it('drops empty items and keeps valid ones', () => {
    expect(normalizeChoices(['valid', '  ', '', 'also valid'])).toEqual(['valid', 'also valid'])
  })

  it('returns empty array when nothing survives', () => {
    expect(normalizeChoices(['', '  ', null, undefined])).toEqual([])
    expect(normalizeChoices([])).toEqual([])
  })
})

describe('normalizeQuestions', () => {
  it('returns empty array for non-array input', () => {
    expect(normalizeQuestions(null)).toEqual([])
    expect(normalizeQuestions('x')).toEqual([])
    expect(normalizeQuestions({})).toEqual([])
  })

  it('normalizes a valid batch and keys by qid', () => {
    const result = normalizeQuestions([
      { choices: ['a', 'b'], qid: 'q0', question: 'One?' },
      { qid: 'q1', question: 'Two?' }
    ])

    expect(result).toEqual([
      { choices: ['a', 'b'], multiSelect: false, qid: 'q0', question: 'One?' },
      { choices: null, multiSelect: false, qid: 'q1', question: 'Two?' }
    ])
  })

  it('drops entries missing qid or question text', () => {
    const result = normalizeQuestions([
      { qid: '', question: 'no qid' },
      { qid: 'q1', question: '   ' },
      'not-an-object',
      { qid: 'q2', question: 'kept' }
    ])

    expect(result.map(q => q.qid)).toEqual(['q2'])
  })

  it('degrades all-blank choices to open-ended per question', () => {
    const result = normalizeQuestions([{ choices: ['', '  '], qid: 'q0', question: 'Q?' }])

    expect(result[0]?.choices).toBeNull()
  })

  it('only honors multi_select when choices survive', () => {
    const result = normalizeQuestions([
      { choices: ['a', 'b'], multi_select: true, qid: 'q0', question: 'A?' },
      { multi_select: true, qid: 'q1', question: 'B?' }
    ])

    expect(result[0]?.multiSelect).toBe(true)
    expect(result[1]?.multiSelect).toBe(false)
  })
})
