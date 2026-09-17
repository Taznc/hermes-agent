// An editable user bubble that contains an ACTIVE directive chip is the one
// place two activation contracts overlap: the bubble opens the edit composer,
// the chip runs its directive. Nesting one native control inside another is
// invalid DOM (React reports "<button> cannot be a descendant of <button>"),
// so the bubble is a role="button" host and the chips stay native buttons.
//
// That split only holds if activating a chip does NOTHING to the bubble's edit
// lifecycle — including the part that has no visible composer: the pointerdown
// that arms the thread's edit hold (`data-editing` on the viewport, which only
// clears when an edit composer unmounts). These tests drive both named active
// chip kinds (url, session) through a real pointer sequence and through the
// keyboard, and assert the directive runs exactly once with the bubble's edit
// lifecycle untouched.
import { type AppendMessage, ExportedMessageRepository } from '@assistant-ui/react'
import { AssistantRuntimeProvider, type ThreadMessage } from '@assistant-ui/react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as ExternalLinkModule from '@/lib/external-link'
import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'
import { __resetSessionLinkTitleCache } from '@/lib/session-link-title'
import { closeRightRail } from '@/store/preview'

import { assistantMessage, stubThreadEnvironment, stubThreadViewportSize, userMessage } from '../test-utils'

import { Thread } from '.'

const openLink = vi.fn()
const openSession = vi.fn()

vi.mock('@/lib/external-link', async importOriginal => {
  const actual = await importOriginal<typeof ExternalLinkModule>()

  return { ...actual, openLink: (...args: unknown[]) => openLink(...args) }
})

vi.mock('@/app/open-session', () => ({
  openSession: (...args: unknown[]) => openSession(...args)
}))

stubThreadEnvironment()
stubThreadViewportSize()

afterEach(() => {
  cleanup()
  closeRightRail()
  openLink.mockClear()
  openSession.mockClear()
  __resetSessionLinkTitleCache()
})

/** Mirrors chat/index.tsx: incremental runtime + messageRepository + onEdit. */
function Harness({ onEdit, text }: { onEdit: (message: AppendMessage) => Promise<void>; text: string }) {
  const repository = ExportedMessageRepository.fromArray([userMessage('user-1', text), assistantMessage()])

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
      <Thread />
    </AssistantRuntimeProvider>
  )
}

const editComposer = (container: HTMLElement) => container.querySelector('[data-slot="aui_edit-composer-root"]')

const editHold = (container: HTMLElement) =>
  container.querySelector('[data-slot="aui_thread-viewport"]')?.getAttribute('data-editing') ?? null

/** The event sequence a real mouse produces on a control, in browser order.
 *  `pointerdown` is the one that matters here: it is what arms the thread's
 *  edit hold, and it bubbles out of the chip into the bubble. */
function activateByPointer(target: HTMLElement) {
  fireEvent.pointerDown(target)
  fireEvent.mouseDown(target)
  fireEvent.pointerUp(target)
  fireEvent.mouseUp(target)
  fireEvent.click(target, { detail: 1 })
}

/** jsdom does not implement a native button's keyboard activation, so the
 *  platform contract is applied explicitly — which is only legitimate because
 *  each test first asserts the chip IS a native button. Two halves matter: the
 *  raw key event must not reach the bubble's own Enter/Space→edit handler, and
 *  the click the browser then synthesizes from the button must run the
 *  directive without opening the composer. */
function activateByKeyboard(target: HTMLElement, key: 'Enter' | ' ') {
  target.focus()
  fireEvent.keyDown(target, { key })

  if (key === ' ') {
    fireEvent.keyUp(target, { key })
  }

  fireEvent.click(target, { detail: 0 })
}

interface ChipCase {
  /** What the chip's directive must have done, exactly once. */
  expectRan: () => Promise<void>
  kind: string
  ran: () => number
  text: string
  title: string
}

const cases: ChipCase[] = [
  {
    kind: 'url',
    text: 'review @url:`https://example.com/docs` before editing',
    title: 'https://example.com/docs',
    ran: () => openLink.mock.calls.length,
    expectRan: async () => {
      await waitFor(() => expect(openLink).toHaveBeenCalledTimes(1))
      expect(openLink).toHaveBeenCalledWith('https://example.com/docs')
    }
  },
  {
    kind: 'session',
    text: 'pick up @session:work/20260101_abc123 where we left off',
    title: 'work/20260101_abc123',
    ran: () => openSession.mock.calls.length,
    expectRan: async () => {
      // openSessionRef lazy-imports the navigator, so the call lands a tick later.
      await waitFor(() => expect(openSession).toHaveBeenCalledWith('20260101_abc123', expect.any(Function), 'tab'))
      expect(openSession).toHaveBeenCalledTimes(1)
    }
  }
]

describe.each(cases)('editable user message containing an active $kind directive', testCase => {
  it('renders the chip as a native button that is not nested in another control', async () => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})

    try {
      render(<Harness onEdit={async () => {}} text={testCase.text} />)

      const editControl = await screen.findByRole('button', { name: 'Edit message' })
      const chip = screen.getByTitle(testCase.title)

      expect(chip.tagName).toBe('BUTTON')
      expect(editControl.tagName).not.toBe('BUTTON')
      expect(chip.parentElement?.closest('button')).toBeNull()
      expect(errors).not.toHaveBeenCalledWith(expect.stringMatching(/button.*descendant/i), expect.anything())
    } finally {
      errors.mockRestore()
    }
  })

  it('runs the directive once on pointer activation without touching the edit lifecycle', async () => {
    const onEdit = vi.fn(async () => {})
    const { container } = render(<Harness onEdit={onEdit} text={testCase.text} />)

    await screen.findByRole('button', { name: 'Edit message' })

    const chip = screen.getByTitle(testCase.title)

    expect(chip.tagName).toBe('BUTTON')
    expect(editHold(container)).toBeNull()

    activateByPointer(chip)

    await testCase.expectRan()

    // The composer never mounts, so an edit hold armed here would never be
    // released — assert the hold was never armed, not merely that it cleared.
    expect(editHold(container)).toBeNull()
    expect(editComposer(container)).toBeNull()
    expect(onEdit).not.toHaveBeenCalled()
  })

  it.each(['Enter', ' '] as const)(
    'runs the directive once on %j activation without touching the edit lifecycle',
    async key => {
      const onEdit = vi.fn(async () => {})
      const { container } = render(<Harness onEdit={onEdit} text={testCase.text} />)

      await screen.findByRole('button', { name: 'Edit message' })

      const chip = screen.getByTitle(testCase.title)

      expect(chip.tagName).toBe('BUTTON')

      activateByKeyboard(chip, key)

      await testCase.expectRan()

      expect(editHold(container)).toBeNull()
      expect(editComposer(container)).toBeNull()
      expect(onEdit).not.toHaveBeenCalled()
    }
  )

  it('still opens the editor by pointer from the ordinary text around the chip', async () => {
    const { container } = render(<Harness onEdit={async () => {}} text={testCase.text} />)

    await screen.findByRole('button', { name: 'Edit message' })

    const prose = container.querySelector('[data-slot="aui_user-message-text"]')

    expect(prose).toBeTruthy()

    // A press on ordinary descendant text still arms the edit hold — the chip
    // guard must key off the nested control the press landed in, not off "the
    // event did not originate on the bubble element itself".
    fireEvent.pointerDown(prose as Element)
    expect(editHold(container)).toBe('true')

    fireEvent.click(prose as Element, { detail: 1 })

    await waitFor(() => expect(editComposer(container)).toBeTruthy())
    expect(testCase.ran()).toBe(0)
  })

  it('still opens the editor by keyboard from the bubble itself', async () => {
    const { container } = render(<Harness onEdit={async () => {}} text={testCase.text} />)

    const bubble = await screen.findByRole('button', { name: 'Edit message' })

    fireEvent.keyDown(bubble, { key: 'Enter' })

    await waitFor(() => expect(editComposer(container)).toBeTruthy())
    expect(testCase.ran()).toBe(0)
  })
})
