import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AppContextMenu } from '@/app/context-menu/app-context-menu'
import { $contextMenu } from '@/app/context-menu/store'
import { $reactionsEnabled } from '@/store/reactions-enabled'

import { assistantMessage, stubThreadEnvironment, userMessage } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()

const PROMPT = 'summarize the release notes'
const ATTACHMENT = '@file:`notes/release.md`'

/** A user prompt carrying every piece of chrome its root renders besides the
 *  text: an agent reaction (badge), a timeline timestamp, and an attachment
 *  ref (chip below the bubble). Copy message must yield prompt + attachment,
 *  never the badge emoji or the timestamp. */
function decoratedPrompt(): ThreadMessage {
  const base = userMessage('user-1', PROMPT)

  return {
    ...base,
    metadata: {
      custom: {
        attachmentRefs: [ATTACHMENT],
        reactions: [{ author: 'agent', emoji: '🎉', at: 1_790_000_000 }],
        timelineTimestamp: 1_790_000_000
      }
    }
  } as ThreadMessage
}

function Harness() {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [decoratedPrompt(), assistantMessage()],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <MemoryRouter>
      <AppContextMenu />
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
    </MemoryRouter>
  )
}

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
let writeClipboard: ReturnType<typeof vi.fn>

beforeEach(() => {
  writeClipboard = vi.fn().mockResolvedValue(undefined)
  desktopWindow.hermesDesktop = { writeClipboard } as unknown as Window['hermesDesktop']
})

afterEach(() => {
  cleanup()
  $contextMenu.set(null)
  $reactionsEnabled.set(false)
  delete desktopWindow.hermesDesktop
})

function copiedText(): string {
  return String(writeClipboard.mock.calls[0]?.[0])
}

function expectPromptOnly(text: string) {
  expect(text).toBe(`${PROMPT}\n${ATTACHMENT}`)
  expect(text).not.toContain('🎉')
}

describe('Copy message on a user prompt', () => {
  it('rides the right-click reaction picker while reactions are on, and copies only the prompt', async () => {
    $reactionsEnabled.set(true)
    render(<Harness />)

    // The rendered root really carries the chrome Copy must skip.
    const text = await screen.findByText(PROMPT)
    const root = text.closest<HTMLElement>('[data-slot="aui_user-message-root"]')!

    expect(root.textContent).toContain('🎉')

    // Bare right-click: the bubble keeps the gesture for the picker…
    expect(fireEvent.contextMenu(text)).toBe(false)
    expect(await screen.findByRole('button', { name: '👍' })).toBeTruthy()

    // …and Copy message is right there in it.
    fireEvent.click(screen.getByRole('button', { name: 'Copy message' }))

    await waitFor(() => expect(writeClipboard).toHaveBeenCalledTimes(1))
    expectPromptOnly(copiedText())
    await waitFor(() => expect(screen.queryByRole('button', { name: '👍' })).toBeNull())
  })

  it('comes from the app menu while reactions are off, and copies only the prompt', async () => {
    render(<Harness />)

    const text = await screen.findByText(PROMPT)

    fireEvent.contextMenu(text)
    fireEvent.click(await screen.findByText('Copy message'))

    await waitFor(() => expect(writeClipboard).toHaveBeenCalledTimes(1))
    expectPromptOnly(copiedText())
  })
})
