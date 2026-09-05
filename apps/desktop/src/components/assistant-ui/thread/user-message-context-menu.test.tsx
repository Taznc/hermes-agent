// Regression for the web-served desktop double context menu: right-clicking a
// user message bubble with reactions disabled used to fall all the way
// through to an unhandled contextmenu event (the reaction bubble has no local
// handler in that state, and AppContextMenu's own capture-phase listener
// deliberately skips `data-context-menu-skip` surfaces). In Electron that
// silently resolves to "no menu" (main never calls Menu.popup); in a plain
// browser tab it IS Chromium's native context menu.
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

describe('user message bubble contextmenu with reactions disabled', () => {
  it('prevents the native menu when the Electron bridge is missing (web build)', async () => {
    $reactionsEnabled.set(false)
    render(<Harness />)

    const bubble = (await screen.findByText('plain chat text')).closest('[data-context-menu-skip]')

    expect(bubble).toBeTruthy()

    const event = fireEvent.contextMenu(bubble!)

    expect(event).toBe(false) // fireEvent returns false when preventDefault() was called
  })

  it('does NOT prevent the native menu when the Electron bridge is present', async () => {
    $reactionsEnabled.set(false)
    desktopWindow.hermesDesktop = {
      contextMenuEdit: vi.fn().mockResolvedValue(undefined)
    } as unknown as Window['hermesDesktop']
    render(<Harness />)

    const bubble = (await screen.findByText('plain chat text')).closest('[data-context-menu-skip]')

    expect(bubble).toBeTruthy()

    const event = fireEvent.contextMenu(bubble!)

    expect(event).toBe(true) // fireEvent returns true when preventDefault() was NOT called
  })
})
