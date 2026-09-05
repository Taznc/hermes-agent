import { describe, expect, it } from 'vitest'

import { dedupeOpenClarifyParts, restorePendingClarifyToolCall } from './tool-parts'
import type { ChatMessage } from './types'

/**
 * The duplicated clarify card, at the projection layer.
 *
 * A session blocks on ONE `clarify.respond` at a time, so two unanswered
 * clarify rows in a transcript are always a correlation miss — never real
 * state. The live-stream path has its own coverage (`clarify-hydration.test`);
 * these cover the paths that reconcile a hydrated/resumed transcript against a
 * live `clarify.request`, which is where the field report came from (the card
 * mounted twice with identical content, both showing "0 of N answered").
 */

const clarifyPart = (toolCallId: string, args: Record<string, unknown>) => ({
  type: 'tool-call' as const,
  toolCallId,
  toolName: 'clarify',
  args: args as never,
  argsText: JSON.stringify(args)
})

const batchArgs = {
  question: 'Scoping the fork-delta inventory',
  questions: [{ question: 'Which layers?' }, { question: 'How granular?' }]
}

const requestArgs = {
  questions: [
    { question: 'Which layers?' },
    { question: 'How granular?' }
  ]
}

describe('dedupeOpenClarifyParts', () => {
  it('leaves a transcript with one open clarify untouched (same array identity)', () => {
    const messages: ChatMessage[] = [
      { id: 'a', role: 'assistant', parts: [clarifyPart('call-1', batchArgs)] }
    ]

    expect(dedupeOpenClarifyParts(messages)).toBe(messages)
  })

  it('never touches SETTLED clarify rows — a transcript may hold many', () => {
    const settled = { ...clarifyPart('call-1', batchArgs), result: { responses: [] } }

    const messages: ChatMessage[] = [
      { id: 'a', role: 'assistant', parts: [settled] },
      { id: 'b', role: 'assistant', parts: [{ ...settled, toolCallId: 'call-2' }] }
    ]

    expect(dedupeOpenClarifyParts(messages)).toBe(messages)
  })

  it('collapses two open rows to one', () => {
    const messages: ChatMessage[] = [
      { id: 'a', role: 'assistant', parts: [clarifyPart('call-provider', batchArgs)] },
      { id: 'b', role: 'assistant', parts: [clarifyPart('req-live', requestArgs)] }
    ]

    const open = dedupeOpenClarifyParts(messages)
      .flatMap(m => m.parts)
      .filter(p => p.type === 'tool-call' && p.toolName === 'clarify')

    expect(open).toHaveLength(1)
  })

  it('gives the survivor the provider tool id so tool.complete still lands', () => {
    const messages: ChatMessage[] = [
      { id: 'a', role: 'assistant', parts: [clarifyPart('call-provider', batchArgs)] },
      { id: 'b', role: 'assistant', parts: [clarifyPart('live-tool:clarify:1', requestArgs)] }
    ]

    const [survivor] = dedupeOpenClarifyParts(messages)
      .flatMap(m => m.parts)
      .filter(p => p.type === 'tool-call' && p.toolName === 'clarify')

    expect(survivor.type === 'tool-call' && survivor.toolCallId).toBe('call-provider')
  })

  it('drops a message left with no parts rather than rendering an empty bubble', () => {
    const messages: ChatMessage[] = [
      { id: 'a', role: 'assistant', parts: [clarifyPart('call-provider', batchArgs)] },
      { id: 'b', role: 'assistant', parts: [clarifyPart('req-live', requestArgs)] }
    ]

    // 'a' is the earlier open row and its message carries nothing else.
    expect(dedupeOpenClarifyParts(messages).map(m => m.id)).toEqual(['b'])
  })

  it('keeps sibling parts of a message whose clarify row was dropped', () => {
    const messages: ChatMessage[] = [
      {
        id: 'a',
        role: 'assistant',
        parts: [{ type: 'text', text: 'Recon done.' }, clarifyPart('call-provider', batchArgs)]
      },
      { id: 'b', role: 'assistant', parts: [clarifyPart('req-live', requestArgs)] }
    ]

    const next = dedupeOpenClarifyParts(messages)

    expect(next.map(m => m.id)).toEqual(['a', 'b'])
    expect(next[0].parts.map(p => p.type)).toEqual(['text'])
  })
})

describe('restorePendingClarifyToolCall', () => {
  it('re-arms a resumed transcript without leaving a second open card', () => {
    // The field shape: a hydrated transcript already carries the provider's
    // clarify call, and the live clarify.request (different id, no top-level
    // question) arrives to re-arm it.
    const messages: ChatMessage[] = [
      { id: 'user-1', role: 'user', parts: [{ type: 'text', text: 'inventory the fork' }] },
      {
        id: 'assistant-1',
        role: 'assistant',
        parts: [{ type: 'text', text: 'Recon done.' }, clarifyPart('call-provider', batchArgs)]
      }
    ]

    const projection = restorePendingClarifyToolCall(messages, {
      args: requestArgs,
      tool_id: 'req-live'
    })

    const open = projection.messages
      .flatMap(m => m.parts)
      .filter(p => p.type === 'tool-call' && p.toolName === 'clarify' && p.result === undefined)

    expect(open).toHaveLength(1)
    expect(projection.streamId).toBe('assistant-1')
  })

  it('collapses a transcript that ALREADY carries two open clarify rows', () => {
    // Belt and braces: whatever produced the pair upstream (a reconcile that
    // grafted a live projection onto a hydrated tail), re-arming must not
    // publish both.
    const messages: ChatMessage[] = [
      { id: 'a', role: 'assistant', parts: [clarifyPart('call-provider', batchArgs)] },
      { id: 'b', role: 'assistant', parts: [clarifyPart('live-tool:clarify:1', requestArgs)] }
    ]

    const projection = restorePendingClarifyToolCall(messages, {
      args: requestArgs,
      tool_id: 'req-live'
    })

    const open = projection.messages
      .flatMap(m => m.parts)
      .filter(p => p.type === 'tool-call' && p.toolName === 'clarify' && p.result === undefined)

    expect(open).toHaveLength(1)
  })
})
