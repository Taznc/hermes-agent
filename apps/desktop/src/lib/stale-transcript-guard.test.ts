import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'

import { messagesIfTranscriptBehind } from './stale-transcript-guard'

const chat = (id: string, rowId?: number): ChatMessage => ({
  id,
  role: 'user',
  parts: [{ type: 'text', text: id }],
  ...(rowId !== undefined ? { rowId } : {})
})

describe('messagesIfTranscriptBehind', () => {
  it('treats an identical transcript that opens with a page-local fold as current', () => {
    const transcript = [chat('opening-fold'), chat('prompt', 2), chat('reply', 3)]

    expect(messagesIfTranscriptBehind(transcript, [...transcript])).toBeNull()
  })

  it('does not report a window with a duplicated leading fold as behind', () => {
    // The window picked up a second copy of the fold from an earlier refresh;
    // the backend has the same rows. Reporting it behind blocks every send.
    const remote = [chat('opening-fold'), chat('prompt', 2), chat('reply', 3)]
    const local = [chat('opening-fold'), ...remote]

    expect(messagesIfTranscriptBehind(local, remote)).toBeNull()
  })

  it('still reports a window that is missing newer stored rows', () => {
    const local = [chat('opening-fold'), chat('prompt', 2)]
    const remote = [chat('opening-fold'), chat('prompt', 2), chat('reply', 3)]

    expect(messagesIfTranscriptBehind(local, remote)?.map(m => m.id)).toEqual(['opening-fold', 'prompt', 'reply'])
  })
})
