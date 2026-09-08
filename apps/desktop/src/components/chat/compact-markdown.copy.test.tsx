/**
 * Focused coverage for the copy control CompactMarkdown adds to fenced code
 * blocks inside a tool's detail body (Phase 2.7). The copy must be the raw
 * fence source — no injected header chrome, no wrap artifacts — and it uses
 * the shared CopyButton's copied/failed feedback.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { CompactMarkdown } from './compact-markdown'

const LONG_COMMAND = '.venv/bin/pip install -q --disable-pip-version-check -e .[dev]'

function renderWithFence(code: string, lang = 'bash') {
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <CompactMarkdown text={`\`\`\`${lang}\n${code}\n\`\`\``} />
    </I18nProvider>
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('CompactMarkdown fenced code copy control', () => {
  it('renders a copy button only next to fenced code blocks', () => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <CompactMarkdown text="just prose, no fence" />
      </I18nProvider>
    )

    expect(screen.queryByRole('button', { name: 'Copy code' })).toBeNull()
  })

  it('copies the exact fenced source, including long flags, with no trailing newline', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })

    renderWithFence(LONG_COMMAND)

    const button = screen.getByRole('button', { name: 'Copy code' })
    fireEvent.click(button)

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(LONG_COMMAND))
    expect(writeText.mock.calls[0][0]).toBe(LONG_COMMAND)
  })

  it('shows copied feedback via the shared CopyButton', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })

    renderWithFence('echo hi')

    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy())
  })

  it('surfaces the failed state when the clipboard write rejects', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })

    renderWithFence('echo hi')

    fireEvent.click(screen.getByRole('button', { name: 'Copy code' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Copy failed' })).toBeTruthy(), { timeout: 5000 })
  })

  it('renders a raw HTML <pre> with a bare text child without throwing, and shows no copy button', () => {
    // Streamdown passes raw HTML through; a bare `<pre>text</pre>` (no `<code>`
    // wrapper) hands MarkdownPre a text child, not a React element. The old
    // `Children.count(children) === 1 ? Children.only(children) : null` guard
    // checked count, not element-ness, and `Children.only` throws on a text
    // child — blanking the whole chat window (no error boundary above this).
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <CompactMarkdown text={'Page content.\n\n<pre>\n$ npm run build\ndone\n</pre>\n\nEnd.'} />
      </I18nProvider>
    )

    expect(screen.getByText(/Page content\./)).toBeTruthy()
    expect(screen.getByText(/End\./)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Copy code' })).toBeNull()
  })
})
