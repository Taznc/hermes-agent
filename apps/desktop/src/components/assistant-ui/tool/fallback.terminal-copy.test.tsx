/**
 * Focused coverage for the terminal-command copy affordance added to the
 * expanded `terminal` tool card (Phase 2.7): the raw `command` string is
 * copied byte-identical — no wrap artifacts from the rendered `<code>`
 * line — and it shows the shared CopyButton's copied/failed feedback.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { setToolDisclosureOpen } from '@/store/tool-view'

vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-copy-1', status: { type: 'complete' } }, thread: { isRunning: false } })
}))

const { ToolFallback } = await import('./fallback')

const LONG_COMMAND = '.venv/bin/pip install -q --disable-pip-version-check -e .[dev]'

function renderTerminalRow(command: string) {
  setToolDisclosureOpen('tool-entry:msg-copy-1:tool:call-copy-1', true)

  const props = {
    args: { command },
    result: { stdout: '', stderr: '', exit_code: 0 },
    toolCallId: 'call-copy-1',
    toolName: 'terminal'
  } as unknown as ComponentProps<typeof ToolFallback>

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <ToolFallback {...props} />
    </I18nProvider>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('terminal transcript copy control', () => {
  it('copies the exact command string, including long flags, not the rendered line', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })

    renderTerminalRow(LONG_COMMAND)

    // Both the whole-card copy button and the inline command copy share the
    // "Copy command" label when there's no substantial stdout/stderr yet —
    // click the one inside the terminal transcript strip.
    const button = document
      .querySelector('.group\\/terminal-transcript')
      ?.querySelector('button[aria-label="Copy command"]') as HTMLButtonElement
    expect(button).toBeTruthy()
    fireEvent.click(button)

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(LONG_COMMAND))
    // Byte-identical: no leading `$ ` prompt glyph, no reflowed whitespace.
    expect(writeText.mock.calls[0][0]).toBe(LONG_COMMAND)
  })

  it('shows copied feedback and an accessible name via the shared CopyButton', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })

    renderTerminalRow('echo hi')

    const button = document
      .querySelector('.group\\/terminal-transcript')
      ?.querySelector('button[aria-label="Copy command"]') as HTMLButtonElement
    fireEvent.click(button)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
  })

  it('surfaces the failed state when the clipboard write rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })

    renderTerminalRow('echo hi')

    const button = document
      .querySelector('.group\\/terminal-transcript')
      ?.querySelector('button[aria-label="Copy command"]') as HTMLButtonElement
    fireEvent.click(button)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Copy failed' })).toBeTruthy())
  })
})
