import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { registerFloatingComposer } from '@/app/chat/composer/floating-target'
import { $reactionsEnabled } from '@/store/reactions-enabled'

import { assistantMessage, stubThreadEnvironment, userMessage } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()

/** The transcript inside a chat surface that owns a floating composer — the
 * shape of every chat pane, and the one the window-level focus-follow acts on. */
function Harness() {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [userMessage('user-1', 'plain chat text'), assistantMessage()],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div data-chat-surface="" data-composer-surface-id="surface-1">
        <Thread />
        <div data-composer-owner="surface-1">
          <div contentEditable data-slot="composer-rich-input" suppressContentEditableWarning tabIndex={-1} />
        </div>
      </div>
    </AssistantRuntimeProvider>
  )
}

let unregister: (() => void) | undefined

beforeEach(() => {
  $reactionsEnabled.set(true)
  unregister = registerFloatingComposer('surface-1', { groupId: 'g1', target: 'main' })
})

afterEach(() => {
  unregister?.()
  unregister = undefined
  cleanup()
  $reactionsEnabled.set(false)
})

describe('assistant reaction picker', () => {
  it('stays open while the pointer moves over the message toward it', async () => {
    render(<Harness />)

    const message = (await screen.findByText('done')).closest<HTMLElement>('[data-slot="aui_assistant-message-root"]')
    const trigger = message?.querySelector<HTMLButtonElement>('[data-slot="aui_msg-reactions"]')

    expect(trigger).toBeTruthy()
    fireEvent.click(trigger!)
    expect(await screen.findByRole('button', { name: '👍' })).toBeTruthy()

    fireEvent.pointerMove(message!, { buttons: 0, clientX: 40, clientY: 40 })

    expect(screen.queryByRole('button', { name: '👍' })).not.toBeNull()
    expect(trigger?.getAttribute('data-state')).toBe('open')
  })
})

// The user bubble's right-click is the desktop stand-in for touch-and-hold,
// so while reactions are ON it opens the picker (reacting survives the removal
// of the double-click tapback); Copy message rides that picker (see
// user-message-copy.test.tsx). While reactions are OFF there is no picker to
// protect, so the bubble must NOT claim the gesture: the shared AppContextMenu
// takes it and offers Copy message. The bubble used to stamp
// `data-context-menu-skip` unconditionally, stranding that right-click on
// "no menu" in Electron and on Chromium's native menu in a browser tab.
describe('user bubble right-click', () => {
  it('opens the reaction picker while reactions are on', async () => {
    render(<Harness />)

    const bubble = (await screen.findByText('plain chat text')).closest('[data-context-menu-skip]')

    expect(bubble).toBeTruthy()
    // The picker gesture preventDefaults; fireEvent returns false in that case.
    expect(fireEvent.contextMenu(bubble!)).toBe(false)
    expect(await screen.findByRole('button', { name: '👍' })).toBeTruthy()
  })

  it('does not claim the gesture when the reaction picker cannot open', async () => {
    $reactionsEnabled.set(false)
    render(<Harness />)

    const text = await screen.findByText('plain chat text')

    // No skip marker => AppContextMenu handles this right-click and can offer
    // Copy message. (The marker returning would strand the gesture.)
    expect(text.closest('[data-context-menu-skip]')).toBeNull()
    expect(fireEvent.contextMenu(text)).toBe(true)
    expect(screen.queryByRole('button', { name: '👍' })).toBeNull()
  })
})
