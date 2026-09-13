// Right-clicking a user message bubble with reactions disabled must produce a
// USABLE menu — the one that carries Copy / Copy message.
//
// History: the bubble used to claim the gesture unconditionally via
// `data-context-menu-skip` (that attr tells AppContextMenu's capture-phase
// listener to stand down so the reaction picker can own touch-and-hold). With
// reactions OFF there is no picker to protect, so the gesture fell through to
// nothing: in Electron that silently resolves to "no menu" (main never calls
// Menu.popup), and in a plain browser tab it was Chromium's own native menu
// painting over the app. Either way the user could not copy their own prompt
// from the menu.
//
// The bubble now stamps the skip attr ONLY while the picker can actually open,
// so a reactions-off right-click reaches the shared menu like any other text.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $reactionsEnabled } from '@/store/reactions-enabled'

import { stubThreadEnvironment, userMessage } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

function Harness() {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [userMessage('user-1', 'plain chat text')],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread />
    </AssistantRuntimeProvider>
  )
}

afterEach(() => {
  cleanup()
  delete desktopWindow.hermesDesktop
  $reactionsEnabled.set(false)
})

describe('user message bubble contextmenu', () => {
  it('does not claim the gesture when the reaction picker cannot open', async () => {
    $reactionsEnabled.set(false)
    render(<Harness />)

    const text = await screen.findByText('plain chat text')

    // No skip marker => AppContextMenu handles this right-click and can offer
    // Copy message. (The marker returning would strand the gesture.)
    expect(text.closest('[data-context-menu-skip]')).toBeNull()
  })

  it('claims the gesture for the picker while reactions are on', async () => {
    $reactionsEnabled.set(true)
    desktopWindow.hermesDesktop = {
      contextMenuEdit: vi.fn().mockResolvedValue(undefined)
    } as unknown as Window['hermesDesktop']
    render(<Harness />)

    const text = await screen.findByText('plain chat text')
    const bubble = text.closest('[data-context-menu-skip]')

    expect(bubble).toBeTruthy()

    // The picker gesture preventDefaults; fireEvent returns false in that case.
    expect(fireEvent.contextMenu(bubble!)).toBe(false)
  })
})
