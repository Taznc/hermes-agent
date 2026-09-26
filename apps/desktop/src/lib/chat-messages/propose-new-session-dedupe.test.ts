import { describe, expect, it } from 'vitest'

import { upsertToolPart } from './tool-parts'
import type { ChatMessagePart } from './types'

/**
 * Dedupe regression for `propose_new_session` (t_2023fb69 recovery, reviewer
 * comment 1341 item 3): `toolPartMatchValues()`/`toolPayloadMatchValues()`
 * gained `topic` as a correlation key (tool-parts.ts:91,140) so a synthetic
 * `session.propose.request` row and the model's real `tool.start` row
 * correlate on the same topic string onto ONE open card instead of two —
 * the same fix clarify already has for `question`/`server`. Mirrors
 * clarify-dedupe.test.ts's coverage pattern for the two possible arrival
 * orders (request-first and tool.start-first), and additionally proves the
 * settled `tool.complete` (always keyed by the provider's real id) still
 * correlates onto the surviving row afterwards.
 */

const TOPIC = 'refactor the auth module'
const REASON = 'this session has drifted far from the original topic'

function proposePayload(toolId: string) {
  return { args: { reason: REASON, topic: TOPIC }, name: 'propose_new_session', tool_id: toolId }
}

function openProposeParts(parts: ChatMessagePart[]) {
  return parts.filter(part => part.type === 'tool-call' && part.toolName === 'propose_new_session' && part.result === undefined)
}

describe('propose_new_session tool.start / session.propose.request dedupe', () => {
  it('(a) synthetic request row then real tool.start: correlates onto one open card', () => {
    let parts: ChatMessagePart[] = []

    // input-requests.ts:272 upserts a synthetic row keyed by the request id
    // as soon as session.propose.request lands.
    parts = upsertToolPart(parts, proposePayload('req-live-1'), 'running')

    // The model's own tool.start can still arrive after it (event-order race
    // documented at tool-parts.ts:494-498 for clarify).
    parts = upsertToolPart(parts, proposePayload('provider-call-1'), 'running')

    const open = openProposeParts(parts)

    // ONE card, not two — the topic correlation collapsed the second upsert
    // onto the same row instead of appending a duplicate.
    expect(open).toHaveLength(1)
    // The most recent event's id is authoritative on the row (mirrors
    // upsertToolPart's normal same-tool-call-id-update semantics); it's the
    // provider's real id here, so tool.complete (keyed by it) lands directly.
    expect(open[0]?.type === 'tool-call' && open[0].toolCallId).toBe('provider-call-1')
  })

  it('(b) real tool.start then synthetic request row: correlates onto one open card, keeping the provider id', () => {
    let parts: ChatMessagePart[] = []

    // The synthetic request hydration path (input-requests.ts:271-284) passes
    // `preferExistingId: true` into `upsertToolPart` for exactly this reason:
    // a request id that merely correlates by topic must never clobber the
    // provider's real tool-call id, or the card's id would flap between the
    // synthetic and real id depending on arrival order, only for the later
    // tool.complete (always keyed by the provider id) to force it back.
    parts = upsertToolPart(parts, proposePayload('provider-call-1'), 'running')
    parts = upsertToolPart(parts, proposePayload('req-live-1'), 'running', undefined, { preferExistingId: true })

    const open = openProposeParts(parts)

    // Still ONE card — the synthetic request update landed on the SAME row
    // the tool.start created, not a second row under the live-tool id.
    expect(open).toHaveLength(1)
    // The provider's real id survives the hydration update, unlike order (a)
    // where the row is genuinely new and simply adopts whichever id arrives.
    expect(open[0]?.type === 'tool-call' && open[0].toolCallId).toBe('provider-call-1')
  })

  it('a subsequent tool.complete keyed by the provider id settles the single card from order (a)', () => {
    let parts: ChatMessagePart[] = []

    parts = upsertToolPart(parts, proposePayload('req-live-1'), 'running')
    parts = upsertToolPart(parts, proposePayload('provider-call-1'), 'running')

    parts = upsertToolPart(
      parts,
      {
        args: { reason: REASON, topic: TOPIC },
        name: 'propose_new_session',
        tool_id: 'provider-call-1',
        result: JSON.stringify({ status: 'approved', session_id: 'new-sid' })
      },
      'complete'
    )

    const settled = parts.filter(part => part.type === 'tool-call' && part.toolName === 'propose_new_session' && part.result !== undefined)

    expect(settled).toHaveLength(1)
    expect(openProposeParts(parts)).toHaveLength(0)
  })

  it('a subsequent tool.complete keyed by the provider id settles the single card from order (b)', () => {
    let parts: ChatMessagePart[] = []

    parts = upsertToolPart(parts, proposePayload('provider-call-1'), 'running')
    parts = upsertToolPart(parts, proposePayload('req-live-1'), 'running', undefined, { preferExistingId: true })

    parts = upsertToolPart(
      parts,
      {
        args: { reason: REASON, topic: TOPIC },
        name: 'propose_new_session',
        tool_id: 'provider-call-1',
        result: JSON.stringify({ status: 'approved', session_id: 'new-sid' })
      },
      'complete'
    )

    // The row already carries the provider id (preferExistingId kept it from
    // being overwritten by the synthetic request id), so the completion
    // settles it by a direct id match — never a stray second card.
    const settled = parts.filter(part => part.type === 'tool-call' && part.toolName === 'propose_new_session' && part.result !== undefined)

    expect(settled).toHaveLength(1)
    expect(openProposeParts(parts)).toHaveLength(0)
  })
})
