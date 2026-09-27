// Double-click in the transcript is TEXT SELECTION, not a tapback.
//
// The gesture used to heart the message, which meant double-clicking a word to
// copy it both fired a reaction and cleared the selection the browser had just
// made (and, on older messages, toasted "Could not react / message not found").
// Selection is the primitive a chat transcript owes the user; reacting stays
// available on the ☺ slot and the right-click picker.
import {
  type AppendMessage,
  AssistantRuntimeProvider,
  ExportedMessageRepository,
  type ThreadMessage
} from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'
import type * as ReactionsStore from '@/store/reactions'
import { $reactionsEnabled } from '@/store/reactions-enabled'
import { $localReactions } from '@/store/reactions-local'

import { assistantMessage, stubThreadEnvironment, stubThreadViewportSize, userMessage } from '../test-utils'

import * as messageReactions from './use-message-reactions'
import { EDIT_CLICK_DELAY_MS, isSelectionClick } from './user-message'

import { Thread } from '.'
stubThreadEnvironment()
stubThreadViewportSize()

const toggleMessageReaction = vi.fn(async () => {})

vi.mock('@/store/reactions', async importOriginal => ({
  ...(await importOriginal<typeof ReactionsStore>()),
  toggleMessageReaction: (...args: unknown[]) => toggleMessageReaction(...(args as []))
}))

function Harness({ onEdit = async () => {} }: { onEdit?: (message: AppendMessage) => Promise<void> }) {
  const repository = ExportedMessageRepository.fromArray([
    userMessage('user-1', 'select this word'),
    assistantMessage()
  ])

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>({
    messageRepository: repository,
    isRunning: false,
    setMessages: () => {},
    onNew: async () => {},
    onEdit,
    onCancel: async () => {},
    onReload: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread cwd={null} gateway={null} sessionId="session-1" />
    </AssistantRuntimeProvider>
  )
}

beforeEach(() => {
  $localReactions.set({})
  // Reactions ON is the case that used to fire: the gesture was gated on this
  // toggle, so with it off the old code was inert either way.
  $reactionsEnabled.set(true)
  toggleMessageReaction.mockClear()
})

afterEach(() => {
  cleanup()
  $reactionsEnabled.set(false)
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
  it('no longer exports a double-click tapback gesture', () => {
    expect('useTapbackDoubleClick' in messageReactions).toBe(false)
    expect('isTapbackDoubleClick' in messageReactions).toBe(false)
  })

  it('does not react on an assistant reply, so the browser keeps the word it selected', async () => {
    render(<Harness />)

    const word = await screen.findByText('done')
    const root = word.closest('[data-slot="aui_assistant-message-root"]')

    expect(root).toBeTruthy()

    const removeAllRanges = vi.spyOn(Selection.prototype, 'removeAllRanges')

    fireEvent.doubleClick(word, { detail: 2 })
    fireEvent.doubleClick(root!, { detail: 2 })

    expect($localReactions.get()['assistant-1']).toBeUndefined()
    expect(toggleMessageReaction).not.toHaveBeenCalled()
    // The old gesture cleared the selection the double-click had just made.
    expect(removeAllRanges).not.toHaveBeenCalled()
    removeAllRanges.mockRestore()
  })

  it('does not react on a user prompt either', async () => {
    render(<Harness />)

    const word = await screen.findByText('select this word')

    fireEvent.doubleClick(word, { detail: 2 })

    expect($localReactions.get()['user-1']).toBeUndefined()
    expect(toggleMessageReaction).not.toHaveBeenCalled()
  })
})

describe('multi-click on a user bubble selects instead of editing', () => {
  it.each([2, 3])('click detail=%i does not open the edit composer', async detail => {
    render(<Harness />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    await act(async () => {
      fireEvent.pointerDown(bubble, { detail })
      fireEvent.click(bubble, { detail })
      await new Promise(resolve => setTimeout(resolve, EDIT_CLICK_DELAY_MS + 50))
    })

    expect(screen.queryByRole('textbox', { name: 'Edit message' })).toBeNull()
  })

  // The real browser sequence: a double-click is click(detail=1) THEN
  // click(detail=2). The first click alone must not swap the bubble for the
  // editor, or the second click lands on the editor and no word is selected.
  it('a real double-click sequence (detail 1 then 2) never opens the edit composer', async () => {
    render(<Harness />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    await act(async () => {
      fireEvent.click(bubble, { detail: 1 })
      await new Promise(resolve => setTimeout(resolve, 60))
    })

    // Between the two clicks the bubble is still a bubble.
    expect(screen.queryByRole('textbox', { name: 'Edit message' })).toBeNull()

    await act(async () => {
      fireEvent.click(bubble, { detail: 2 })
      fireEvent.click(bubble, { detail: 3 })
      await new Promise(resolve => setTimeout(resolve, EDIT_CLICK_DELAY_MS + 50))
    })

    expect(screen.queryByRole('textbox', { name: 'Edit message' })).toBeNull()
  })

  it('a plain single click still opens the edit composer', async () => {
    render(<Harness />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    await act(async () => {
      fireEvent.click(bubble, { detail: 1 })
    })

    expect(await screen.findByRole('textbox', { name: 'Edit message' })).toBeTruthy()
  })

  it('keyboard activation (click detail=0) opens the edit composer at once', async () => {
    render(<Harness />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    await act(async () => {
      fireEvent.click(bubble, { detail: 0 })
    })

    expect(screen.getByRole('textbox', { name: 'Edit message' })).toBeTruthy()
  })
})
