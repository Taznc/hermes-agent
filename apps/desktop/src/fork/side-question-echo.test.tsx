import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { renderMessageStream } from '@/app/session/hooks/use-message-stream/test-harness'

import { ASK_SIDE_MARKER } from './side-question'

// Anchor `ask-side-question-echo` (gateway-event/status.ts): a side question asked from an Ask
// card is answered inside the card, so the core's `[btw "…"]` transcript line is not added.
describe('btw.complete from an Ask card side question', () => {
  afterEach(cleanup)

  it('does not echo the card prompt, but still echoes a plain /btw', () => {
    const stream = renderMessageStream('s1')

    const send = (question: string, task_id: string) =>
      act(() => stream.handleEvent({ payload: { question, task_id, text: 'Because.' }, session_id: 's1', type: 'btw.complete' }))

    send(`${ASK_SIDE_MARKER} [The user has an open question card…] Question: why?`, 'btw_card')
    expect(stream.state('s1').messages.at(-1)).toBeUndefined()

    send('why?', 'btw_plain')
    expect(stream.state('s1').messages.at(-1)?.id).toBe('btw-complete-btw_plain')
  })
})
