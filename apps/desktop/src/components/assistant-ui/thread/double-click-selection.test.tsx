// Double-click in the transcript is TEXT SELECTION, not a tapback.
//
// The gesture used to heart the message, which meant double-clicking a word to
// copy it both fired a reaction and cleared the selection the browser had just
// made. Selection is the primitive a chat transcript owes the user; reacting
// stays available on the ☺ slot and the right-click picker.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as ReactionsStore from '@/store/reactions'
import { $reactionsEnabled } from '@/store/reactions-enabled'
import { $localReactions } from '@/store/reactions-local'

import { assistantMessage, stubThreadEnvironment } from '../test-utils'

import { isSelectionClick } from './user-message'

import { Thread } from '.'
stubThreadEnvironment()

const toggleMessageReaction = vi.fn(async () => {})

vi.mock('@/store/reactions', async importOriginal => ({
  ...(await importOriginal<typeof ReactionsStore>()),
  toggleMessageReaction: (...args: unknown[]) => toggleMessageReaction(...(args as []))
}))

function Harness() {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [assistantMessage()],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

beforeEach(() => {
  $localReactions.set({})
  $reactionsEnabled.set(false)
  toggleMessageReaction.mockClear()
})

afterEach(() => {
  cleanup()
})

describe('isSelectionClick', () => {
  it('claims a double-click, whose word-select lands after the handler runs', () => {
    expect(isSelectionClick({ detail: 2 })).toBe(true)
  })

  it('claims a triple-click (select the paragraph)', () => {
    expect(isSelectionClick({ detail: 3 })).toBe(true)
  })

  it('leaves a plain single click to the element it landed on', () => {
    expect(isSelectionClick({ detail: 1 })).toBe(false)
  })
})

describe('double-click in the transcript', () => {
  it('does not react, so the browser keeps the word it selected', async () => {
    // Reactions ON is the case that used to fire: the gesture was gated on
    // this toggle, so with it off the old code was inert either way.
    $reactionsEnabled.set(true)
    render(<Harness />)

    const message = (await screen.findByText('done')).closest('[data-slot="aui_assistant-message-root"]')

    expect(message).toBeTruthy()

    fireEvent.doubleClick(message!, { detail: 2 })

    expect($localReactions.get()['assistant-1']).toBeUndefined()
    expect(toggleMessageReaction).not.toHaveBeenCalled()
  })

  it('leaves assistant message text selectable rather than user-select: none', async () => {
    render(<Harness />)

    const body = (await screen.findByText('done')).closest('[data-slot="aui_assistant-message-content"]')

    expect(body).toBeTruthy()
    // The gesture is gone; nothing in the render path may re-disable selection
    // on the message body inline (the stylesheet grants it globally).
    expect((body as HTMLElement).style.userSelect).not.toBe('none')
  })
})
