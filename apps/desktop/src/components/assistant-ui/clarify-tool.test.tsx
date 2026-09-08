import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { onComposerInsertRequest } from '@/app/chat/composer/focus'
import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { hiddenPaneProps } from '@/components/pane-shell/pane-visibility'
import { $activeTreeGroup, $hoveredTreeGroup } from '@/components/pane-shell/tree/store'
import { I18nProvider } from '@/i18n'
import { $clarifyToolRequestIds, clearClarifyRequest, setClarifyRequest, updateClarifyHelp } from '@/store/clarify'
import { $gateway } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import { $activeSessionId, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'

import { ClarifyTool, readClarifyBatchResult, readClarifyResult } from './clarify-tool'

// The OWNER-socket seam (`requestForOwnedSession` → `requestForSessionProfile`
// → here). Mocked so the real owner ladder still runs against real fixtures and
// only the dial is observed; the rest of the gateway store stays actual, so
// `$gateway` remains the genuine ambient atom every other test in this file
// drives.
const gatewayMocks = vi.hoisted(() => ({
  requestGatewayForAgent: vi.fn(async () => ({ ok: true }))
}))

vi.mock('@/store/gateway', async importActual => ({
  ...(await importActual<Record<string, unknown>>()),
  requestGatewayForAgent: gatewayMocks.requestGatewayForAgent
}))

// The live pending card used to require message-running. Tests that exercise
// the pending form force that on; the settle-shift case flips it off.
let messageRunning = true

vi.mock('@assistant-ui/react', () => ({
  useAuiState: () => messageRunning
}))

afterEach(() => {
  cleanup()
  clearClarifyRequest()
  // A tool row's request binding is sticky by design (it must not adopt a later
  // turn's request), so it has to be reset between tests or a reused
  // toolCallId inherits the previous test's binding.
  $clarifyToolRequestIds.set({})
  $activeSessionId.set(null)
  $gateway.set(null)
  messageRunning = true
  vi.clearAllMocks()
})

function clarifyTree(ui: ReactNode) {
  return (
    <I18nProvider configClient={null} initialLocale="en">
      {ui}
    </I18nProvider>
  )
}

function renderClarify(ui: ReactNode) {
  return render(clarifyTree(ui))
}

function settledClarifyProps(
  args: ToolCallMessagePartProps['args'],
  result: ToolCallMessagePartProps['result'],
  toolCallId: string
): ToolCallMessagePartProps {
  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result,
    resume: vi.fn(),
    status: { type: 'complete' },
    toolCallId,
    toolName: 'clarify',
    type: 'tool-call'
  }
}

function liveClarifyProps(choices = ['staging', 'production']): ToolCallMessagePartProps {
  const args = { choices, question: 'Which deployment target?' }

  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result: undefined,
    resume: vi.fn(),
    status: { type: 'running' },
    toolCallId: 'clarify-live',
    toolName: 'clarify',
    type: 'tool-call'
  }
}

function renderLiveClarify({ multiSelect = false }: { multiSelect?: boolean } = {}) {
  const request = vi.fn().mockResolvedValue({ ok: true })

  $activeSessionId.set('session-1')
  $gateway.set({ request } as never)
  setClarifyRequest({
    choices: ['staging', 'production'],
    multiSelect,
    question: 'Which deployment target?',
    requestId: 'request-1',
    sessionId: 'session-1'
  })
  const { rerender } = renderClarify(<ClarifyTool {...liveClarifyProps()} />)

  return { request, rerender }
}

describe('ClarifyTool live card stays mounted across settle', () => {
  it('keeps the question card while the gateway request is open and the turn reports not-running', () => {
    messageRunning = false
    renderLiveClarify()

    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(document.querySelector('[data-clarify-choices]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-settled]')).toBeNull()
  })

  it('keeps the question visible but inert when the turn stopped and no request is left to answer', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)
    renderClarify(<ClarifyTool {...liveClarifyProps()} />)

    // Previously this demoted to the generic tool row, which is exactly how a
    // live question ended up buried in raw TOOL PAYLOAD. The question stays
    // legible; what changes is that nothing is armed.
    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Continue/ }).hasAttribute('disabled')).toBe(true)
    // The shortcut marker stays off, so the composer keeps its printable keys.
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()
  })

  it('holds the card through the gap between answering and the settled result', async () => {
    const { request, rerender } = renderLiveClarify()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]staging/ }))
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalled()
    })

    // tool.complete is what swaps in the settled card; the turn can already
    // read as not-running in that gap.
    messageRunning = false
    rerender(clarifyTree(<ClarifyTool {...liveClarifyProps()} />))

    // The card is still the clarify card, not a settled row or a payload row.
    // Its shortcut marker is correctly gone — the request was cleared on submit,
    // so there is nothing left for a keystroke to act on.
    expect(document.querySelector('[data-slot="clarify-inline"]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-settled]')).toBeNull()
  })

  it('disarms but keeps the card when the turn is stopped after it was live but never answered', () => {
    renderLiveClarify()

    expect(document.querySelector('[data-clarify-choices]')).toBeTruthy()

    messageRunning = false
    act(() => clearClarifyRequest('request-1', 'session-1'))

    // Disarmed: the shortcut marker is gone and Continue cannot fire. Still
    // visible: the question is in the tool args, so burying it in raw payload
    // would be the very regression this file guards.
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()
    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Continue/ }).hasAttribute('disabled')).toBe(true)
  })

  it('paints the question from tool args instead of a spinner while request_id is still racing', () => {
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)
    renderClarify(<ClarifyTool {...liveClarifyProps()} />)

    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.queryByRole('status', { name: /loading question/i })).toBeNull()
    expect(screen.getByRole('button', { name: /Continue/ }).hasAttribute('disabled')).toBe(true)
  })
})

describe('ClarifyTool choice selection', () => {
  it('selects independently, deselects and submits multi-select choices as a JSON array', async () => {
    const { request } = renderLiveClarify({ multiSelect: true })
    const staging = screen.getByRole('button', { name: /^[A-Z]staging/ })
    const production = screen.getByRole('button', { name: /^[A-Z]production/ })

    fireEvent.click(staging)
    fireEvent.click(production)
    expect(staging.getAttribute('aria-pressed')).toBe('true')
    expect(production.getAttribute('aria-pressed')).toBe('true')

    fireEvent.keyDown(window, { key: 'ArrowDown' })
    expect(staging.getAttribute('aria-pressed')).toBe('true')
    expect(production.getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(staging)
    expect(staging.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(staging)

    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: JSON.stringify(['production', 'staging']),
        request_id: 'request-1'
      })
    })
  })

  it('sends an optional note separately from the selected single answer', async () => {
    const { request } = renderLiveClarify()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]staging/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for staging' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Note for staging' }), {
      target: { value: 'Prefer the preview.' }
    })
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'staging',
        note: 'Prefer the preview.',
        request_id: 'request-1'
      })
    })
  })

  it('keeps a question note reviewable after deselecting every choice and selecting another', () => {
    renderLiveClarify({ multiSelect: true })
    const staging = screen.getByRole('button', { name: /^[A-Z]staging/ })

    fireEvent.click(staging)
    fireEvent.click(screen.getByRole('button', { name: 'Add note for staging' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Note for staging' }), {
      target: { value: 'Preserve this note.' }
    })
    fireEvent.click(staging)
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]production/ }))

    expect(screen.getByRole('textbox', { name: 'Note for production' })).toHaveProperty('value', 'Preserve this note.')
  })

  it('keeps a selected-answer note visible and sends it when switching to Other', async () => {
    const { request } = renderLiveClarify()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]staging/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for staging' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Note for staging' }), {
      target: { value: 'Keep this context.' }
    })
    fireEvent.change(screen.getByPlaceholderText('Other (type your answer)'), {
      target: { value: 'Custom deployment' }
    })

    expect(screen.getByRole('textbox', { name: 'Note for Other (type your answer)' })).toHaveProperty(
      'value',
      'Keep this context.'
    )
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'Custom deployment',
        note: 'Keep this context.',
        request_id: 'request-1'
      })
    )
  })

  it('does not submit a note twice while the first response is pending', async () => {
    const { request } = renderLiveClarify()
    request.mockImplementation(() => new Promise(() => {}))

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]staging/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for staging' }))
    const note = screen.getByRole('textbox', { name: 'Note for staging' })
    fireEvent.change(note, { target: { value: 'Ready.' } })
    fireEvent.keyDown(note, { key: 'Enter' })
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))

    expect((note as HTMLTextAreaElement).disabled).toBe(true)
    fireEvent.keyDown(note, { key: 'Enter' })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('opens one scoped note editor when multiple choices are selected', () => {
    renderLiveClarify({ multiSelect: true })

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]staging/ }))
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]production/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for staging' }))

    expect(screen.getAllByPlaceholderText('Add an optional note…')).toHaveLength(1)
  })

  it('keeps single-select replacement and plain-string submission', async () => {
    const { request } = renderLiveClarify()
    const staging = screen.getByRole('button', { name: /^[A-Z]staging/ })
    const production = screen.getByRole('button', { name: /^[A-Z]production/ })

    fireEvent.click(staging)
    fireEvent.click(production)

    expect(staging.getAttribute('aria-pressed')).toBe('false')
    expect(production.getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'production',
        request_id: 'request-1'
      })
    })
  })

  it('submits the Other draft on plain Enter and cancels the newline', async () => {
    const { request } = renderLiveClarify()
    const other = screen.getByPlaceholderText('Other (type your answer)')

    other.focus()
    fireEvent.change(other, { target: { value: 'canary' } })

    // A cancelled keydown is how the browser is told not to insert "\n".
    expect(fireEvent.keyDown(other, { key: 'Enter' })).toBe(false)
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'canary',
        request_id: 'request-1'
      })
    })
  })

  it('leaves Shift+Enter and IME Enter alone in the Other draft', () => {
    const { request } = renderLiveClarify()
    const other = screen.getByPlaceholderText('Other (type your answer)')

    other.focus()
    fireEvent.change(other, { target: { value: 'canary' } })

    expect(fireEvent.keyDown(other, { key: 'Enter', shiftKey: true })).toBe(true)
    expect(fireEvent.keyDown(other, { isComposing: true, key: 'Enter' })).toBe(true)
    expect(request).not.toHaveBeenCalled()
    expect((other as HTMLTextAreaElement).value).toBe('canary')
  })
})

describe('ClarifyTool help controls', () => {
  it('routes question and choice help without changing the staged answer', async () => {
    const { request } = renderLiveClarify()

    fireEvent.click(screen.getByRole('button', { name: 'Why? Which deployment target?' }))
    fireEvent.click(screen.getByRole('button', { name: 'Why? staging' }))

    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    expect(request).toHaveBeenNthCalledWith(1, 'clarify.explain', {
      request_id: 'request-1',
      session_id: 'session-1',
      version: 1
    })
    expect(request).toHaveBeenNthCalledWith(2, 'clarify.explain', {
      choice: 'staging',
      request_id: 'request-1',
      session_id: 'session-1',
      version: 1
    })
    expect(screen.getByRole('button', { name: /Continue/ }).getAttribute('disabled')).not.toBeNull()
  })

  it('submits a custom follow-up in place without selecting its choice', async () => {
    const { request } = renderLiveClarify()
    const choice = screen.getByRole('button', { name: /^[A-Z]staging/ })

    fireEvent.click(screen.getByRole('button', { name: 'Ask about staging' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Follow-up for choice-0' }), {
      target: { value: 'What changes after deployment?' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.explain', {
        choice: 'staging',
        follow_up: 'What changes after deployment?',
        request_id: 'request-1',
        session_id: 'session-1',
        version: 1
      })
    })
    expect(choice.getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByRole('button', { name: /Continue/ }).hasAttribute('disabled')).toBe(true)
  })

  it('keeps keyboard help activation from selecting its target choice', async () => {
    const { request } = renderLiveClarify()
    const choice = screen.getByRole('button', { name: /^[A-Z]staging/ })
    const why = screen.getByRole('button', { name: 'Why? staging' })

    why.focus()
    fireEvent.keyDown(why, { key: 'Enter' })
    fireEvent.click(why)

    await waitFor(() =>
      expect(request).toHaveBeenCalledWith('clarify.explain', expect.objectContaining({ choice: 'staging' }))
    )
    expect(choice.getAttribute('aria-pressed')).toBe('false')
  })

  it('keeps a batch draft and staged selection while question help fails and is retried', async () => {
    const request = renderLiveBatch()
    request
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce({ explanation_id: 'retry-help' })

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'packet' } })
    fireEvent.click(screen.getByRole('button', { name: 'Why? Color?' }))

    await waitFor(() => expect(screen.getByText('backend unavailable')).toBeTruthy())
    expect(screen.getByRole('button', { name: /^[A-Z]red/ }).getAttribute('aria-pressed')).toBe('true')
    expect((screen.getByPlaceholderText('Type your answer…') as HTMLTextAreaElement).value).toBe('packet')
    expect((screen.getByRole('button', { name: /Confirm and continue/ }) as HTMLButtonElement).disabled).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Why? Color?' }))
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    expect((screen.getByPlaceholderText('Type your answer…') as HTMLTextAreaElement).value).toBe('packet')
  })

  it('keeps help on the same settled tool row after the pending card remounts', () => {
    renderLiveClarify()
    updateClarifyHelp('request-1', 'session-1', 'explain-1', {
      content: 'Production affects customer traffic.',
      followUp: '',
      status: 'complete'
    })
    act(() => clearClarifyRequest('request-1', 'session-1'))
    cleanup()

    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'production'] },
          { question: 'Which deployment target?', user_response: 'staging' },
          'clarify-live'
        )}
      />
    )

    const details = screen.getByText('Help requested').closest('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    fireEvent.click(screen.getByText('Help requested'))
    expect(details.open).toBe(true)
    expect(screen.getByText('Production affects customer traffic.')).toBeTruthy()
  })
})

describe('readClarifyResult', () => {
  it('reads question + user_response from the tool JSON payload', () => {
    expect(
      readClarifyResult({
        question: 'Which target?',
        choices_offered: ['staging', 'prod'],
        user_response: 'staging'
      })
    ).toEqual({
      question: 'Which target?',
      answer: 'staging',
      error: undefined
    })
  })

  it('parses a JSON string result the same way as an object', () => {
    expect(
      readClarifyResult(
        JSON.stringify({
          question: 'Ship it?',
          user_response: 'yes'
        })
      )
    ).toEqual({
      question: 'Ship it?',
      answer: 'yes',
      error: undefined
    })
  })

  it('keeps an empty user_response so Skip can render as skipped', () => {
    expect(readClarifyResult({ question: 'Ok?', user_response: '' })).toEqual({
      question: 'Ok?',
      answer: '',
      error: undefined
    })
  })
})

describe('ClarifyTool settled view', () => {
  it('keeps the question and answer visible after the tool completes', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          {
            question: 'Which deployment target?',
            choices_offered: ['staging', 'prod'],
            user_response: 'staging'
          },
          'clarify-1'
        )}
      />
    )

    expect(screen.getByText('Which deployment target?')).toBeTruthy()
    expect(screen.getByText('staging')).toBeTruthy()
    expect(document.querySelector('[data-clarify-settled]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-answer]')?.textContent).toBe('staging')
  })

  it('labels an empty response as Skipped', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Anything else?' },
          { question: 'Anything else?', user_response: '' },
          'clarify-2'
        )}
      />
    )

    expect(screen.getByText('Anything else?')).toBeTruthy()
    expect(screen.getByText('Skipped')).toBeTruthy()
  })

  it('keeps the original choices visible and clickable after a skip', async () => {
    const inserts: string[] = []

    const stop = onComposerInsertRequest(detail => {
      inserts.push(detail.text)
    })

    try {
      renderClarify(
        <ClarifyTool
          {...settledClarifyProps(
            { question: 'Which deployment target?', choices: ['staging', 'prod'] },
            { question: 'Which deployment target?', user_response: '' },
            'clarify-3'
          )}
        />
      )

      // The skip label renders AND the original options are still on screen.
      expect(screen.getByText('Skipped')).toBeTruthy()
      const group = document.querySelector('[data-clarify-late-choices]')
      expect(group).toBeTruthy()
      expect(screen.getByText('staging')).toBeTruthy()
      expect(screen.getByText('prod')).toBeTruthy()

      // Picking one drafts a quoted follow-up into the composer. The insert
      // bus defers dispatch by a macrotask, so flush one tick.
      fireEvent.click(screen.getByText('prod'))
      await new Promise(resolve => window.setTimeout(resolve, 0))

      expect(inserts).toHaveLength(1)
      expect(inserts[0]).toContain('Which deployment target?')
      expect(inserts[0]).toContain('prod')
    } finally {
      stop()
    }
  })

  it('does not render late choices on an answered clarify', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Which deployment target?', choices: ['staging', 'prod'] },
          { question: 'Which deployment target?', user_response: 'staging' },
          'clarify-4'
        )}
      />
    )

    expect(document.querySelector('[data-clarify-late-choices]')).toBeNull()
  })

  it('does not render late choices for a free-text (no-choice) skip', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { question: 'Anything else?' },
          { question: 'Anything else?', user_response: '' },
          'clarify-5'
        )}
      />
    )

    expect(document.querySelector('[data-clarify-late-choices]')).toBeNull()
  })
})

describe('ClarifyTool keyboard navigation', () => {
  it('cycles through choices and Other with the arrow keys', () => {
    renderLiveClarify()

    const staging = screen.getByRole('button', { name: /^[A-Z]staging/ })
    const production = screen.getByRole('button', { name: /^[A-Z]production/ })
    const other = screen.getByPlaceholderText(/Other/)

    expect(staging.getAttribute('data-highlighted')).toBe('true')
    expect(staging.getAttribute('aria-current')).toBe('true')
    expect(staging.getAttribute('aria-keyshortcuts')).toBe('A 1')

    fireEvent.keyDown(window, { key: 'ArrowDown' })
    expect(production.getAttribute('data-highlighted')).toBe('true')

    fireEvent.keyDown(window, { key: 'ArrowDown' })
    expect(other.closest('label')?.getAttribute('data-highlighted')).toBe('true')
    expect(other.getAttribute('aria-current')).toBe('true')
    expect(other.getAttribute('aria-keyshortcuts')).toBe('C 3')

    fireEvent.keyDown(window, { key: 'ArrowDown' })
    expect(staging.getAttribute('data-highlighted')).toBe('true')

    fireEvent.keyDown(window, { key: 'ArrowUp' })
    expect(other.closest('label')?.getAttribute('data-highlighted')).toBe('true')
  })

  it('selects by number and confirms the answer with Enter', async () => {
    const { request } = renderLiveClarify()

    fireEvent.keyDown(window, { key: '2' })
    fireEvent.keyDown(window, { key: 'Enter' })

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'production',
        request_id: 'request-1'
      })
    })
  })

  it('stages a highlighted multi-select choice with Enter and submits it with Continue', async () => {
    const { request } = renderLiveClarify({ multiSelect: true })
    const production = screen.getByRole('button', { name: /^[A-Z]production/ })

    fireEvent.keyDown(window, { key: 'ArrowDown' })
    fireEvent.keyDown(window, { key: 'Enter' })

    expect(production.getAttribute('aria-pressed')).toBe('true')
    expect(request).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: JSON.stringify(['production']),
        request_id: 'request-1'
      })
    })
  })

  it('focuses Other when its number is pressed and leaves typing keys alone', () => {
    renderLiveClarify()

    const other = screen.getByPlaceholderText(/Other/)

    fireEvent.keyDown(window, { key: '3' })
    expect(document.activeElement).toBe(other)

    fireEvent.change(other, { target: { value: 'canary' } })
    fireEvent.keyDown(window, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(other)
    expect((other as HTMLTextAreaElement).value).toBe('canary')
  })

  it('does not intercept keyboard events while an action button has focus', () => {
    const { request } = renderLiveClarify()
    const skip = screen.getByRole('button', { name: 'Skip' })

    skip.focus()

    expect(fireEvent.keyDown(window, { key: 'Enter' })).toBe(true)
    expect(fireEvent.keyDown(window, { key: 'ArrowDown' })).toBe(true)
    expect(request).not.toHaveBeenCalled()
  })
})

describe('ClarifyTool recommended option', () => {
  it('dims the (Recommended) label and answers with the choice the backend sent', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)
    setClarifyRequest({
      choices: ['staging (Recommended)', 'production'],
      multiSelect: false,
      question: 'Which deployment target?',
      requestId: 'request-1',
      sessionId: 'session-1'
    })
    renderClarify(<ClarifyTool {...liveClarifyProps(['staging (Recommended)', 'production'])} />)

    const recommended = screen.getByRole('button', { name: /^[A-Z]staging/ })

    // The label rides in its own muted span so the option text still reads first.
    expect(recommended.querySelector('.text-\\(--ui-text-tertiary\\)')?.textContent).toBe('(Recommended)')

    fireEvent.click(recommended)
    fireEvent.keyDown(window, { key: 'Enter' })

    // The decorated string goes back verbatim; the tool strips the label before
    // the agent ever sees the answer.
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'staging (Recommended)',
        request_id: 'request-1'
      })
    })
  })
})

describe('ClarifyTool pending marker', () => {
  it('marks a live choices card with its row count so type-to-focus yields exactly its keys', () => {
    renderLiveClarify()

    // `clarifyCardOwnsKey` reads the count off this marker to yield only the
    // shortcuts the card renders (A..N + "Other", 1-9, Enter) and let every
    // other printable through to the composer.
    const card = document.querySelector('[data-clarify-choices]')

    expect(card).toBeTruthy()
    expect(Number(card?.getAttribute('data-clarify-choices'))).toBeGreaterThan(0)
  })

  it('does not mark a free-text (no-choice) pending card', () => {
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn().mockResolvedValue({ ok: true }) } as never)
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: 'Anything else?',
      requestId: 'request-1',
      sessionId: 'session-1'
    })

    const args = { question: 'Anything else?' }
    renderClarify(
      <ClarifyTool
        addResult={vi.fn()}
        args={args}
        argsText={JSON.stringify(args)}
        isError={false}
        respondToApproval={vi.fn()}
        result={undefined}
        resume={vi.fn()}
        status={{ type: 'running' }}
        toolCallId="clarify-free"
        toolName="clarify"
        type="tool-call"
      />
    )

    // No shortcuts → nothing to protect → composer type-to-focus stays live.
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()
  })
})

// ─── Batch (multi-question) clarify ─────────────────────────────────────────

function batchArgs(): { questions: { question: string; choices?: string[] }[] } {
  return {
    questions: [{ choices: ['red', 'blue'], question: 'Color?' }, { question: 'Name?' }]
  }
}

function liveBatchProps(): ToolCallMessagePartProps {
  const args = batchArgs()

  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result: undefined,
    resume: vi.fn(),
    status: { type: 'running' },
    toolCallId: 'clarify-batch',
    toolName: 'clarify',
    type: 'tool-call'
  }
}

function renderLiveBatch(
  lockedAnswers?: Record<string, string>,
  multiSelect = false,
  lockedNotes?: Record<string, string>
) {
  const request = vi.fn().mockResolvedValue({ ok: true, remaining: [] })

  $activeSessionId.set('session-1')
  $gateway.set({ request } as never)
  setClarifyRequest({
    choices: null,
    lockedAnswers,
    lockedNotes,
    multiSelect: false,
    question: '',
    questions: [
      { choices: ['red', 'blue'], multiSelect, qid: 'q0', question: 'Color?' },
      { choices: null, multiSelect: false, qid: 'q1', question: 'Name?' }
    ],
    requestId: 'request-batch',
    sessionId: 'session-1'
  })
  renderClarify(<ClarifyTool {...liveBatchProps()} />)

  return request
}

describe('readClarifyBatchResult', () => {
  it('parses responses with string and list answers plus timed_out', () => {
    const parsed = readClarifyBatchResult(
      JSON.stringify({
        responses: [
          { question: 'Color?', user_response: 'red' },
          { question: 'Tools?', user_response: ['a', 'b'] },
          { question: 'Name?', user_response: '' }
        ],
        timed_out: true
      })
    )

    expect(parsed.timedOut).toBe(true)
    expect(parsed.responses).toHaveLength(3)
    expect(parsed.responses[1]?.answer).toEqual(['a', 'b'])
    expect(parsed.responses[2]?.answer).toBe('')
  })

  it('returns empty responses for single-question payloads', () => {
    expect(readClarifyBatchResult({ question: 'Q?', user_response: 'a' }).responses).toEqual([])
  })
})

describe('ClarifyTool batch card', () => {
  it('renders every question at once', () => {
    renderLiveBatch()

    expect(screen.getByText('Color?')).toBeTruthy()
    expect(screen.getByText('Name?')).toBeTruthy()
    expect(screen.getByText('0 of 2 answered')).toBeTruthy()
  })

  it('gives each option in a question its OWN letter badge', () => {
    renderLiveBatch()

    // Guards the redesign's rename: the option map's index used to be named
    // `index` and shadowed nothing, but the block now also takes a question
    // `index` prop. If the two are ever conflated, every choice in a block
    // renders the SAME badge and the letters stop meaning anything.
    const block = document.querySelector('[data-clarify-batch-question="q0"]')
    const badges = [...(block?.querySelectorAll('[data-choice] kbd') ?? [])].map(el => el.textContent)

    expect(badges).toEqual(['A', 'B'])
  })

  it('marks a question answered once it is staged, for at-a-glance progress', () => {
    renderLiveBatch()

    const answeredQids = () =>
      [...document.querySelectorAll('[data-clarify-batch-question][data-clarify-answered]')].map(el =>
        el.getAttribute('data-clarify-batch-question')
      )

    expect(answeredQids()).toEqual([])

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))

    expect(answeredQids()).toEqual(['q0'])
  })

  it('stages locally and keeps the single confirm disabled until all answered', async () => {
    const request = renderLiveBatch()
    const confirm = screen.getByRole('button', { name: /Confirm and continue/ })

    expect((confirm as HTMLButtonElement).disabled).toBe(true)

    // Staging a pick sends NOTHING to the server.
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    expect(screen.getByText('1 of 2 answered')).toBeTruthy()
    expect(request).not.toHaveBeenCalled()
    expect((confirm as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'packet' } })
    expect(screen.getByText('2 of 2 answered')).toBeTruthy()
    expect(request).not.toHaveBeenCalled()
    expect((confirm as HTMLButtonElement).disabled).toBe(false)
  })

  it('keeps a scoped note directly under a selected choice and sends it separately', async () => {
    const request = renderLiveBatch()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))

    const note = screen.getByRole('textbox', { name: 'Note for red' })
    expect(note.closest('[data-clarify-batch-question="q0"]')).toBeTruthy()
    fireEvent.change(note, { target: { value: 'Use the stable palette.' } })
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'packet' } })
    fireEvent.submit(document.querySelector('form') as HTMLFormElement)

    await waitFor(() => {
      expect(request).toHaveBeenCalledTimes(2)
    })
    expect(request).toHaveBeenNthCalledWith(1, 'clarify.respond', {
      answer: 'red',
      note: 'Use the stable palette.',
      question_id: 'q0',
      request_id: 'request-batch'
    })
  })

  it('keeps one scoped note editor for a batch multi-select answer', () => {
    renderLiveBatch(undefined, true)

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]blue/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))

    expect(screen.getAllByPlaceholderText('Add an optional note…')).toHaveLength(1)
  })

  it('keeps a batch note reviewable after deselecting every choice and selecting another', () => {
    renderLiveBatch(undefined, true)
    const red = screen.getByRole('button', { name: /^[A-Z]red/ })

    fireEvent.click(red)
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Note for red' }), { target: { value: 'Keep me.' } })
    fireEvent.click(red)
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]blue/ }))

    expect(screen.getByRole('textbox', { name: 'Note for blue' })).toHaveProperty('value', 'Keep me.')
  })

  it('keeps a batch choice note visible when the answer changes to Other', () => {
    renderLiveBatch()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Note for red' }), { target: { value: 'Keep me.' } })
    fireEvent.change(screen.getByPlaceholderText('Other (type your answer)'), { target: { value: 'green' } })

    expect(screen.getByRole('textbox', { name: 'Note for Other (type your answer)' })).toHaveProperty(
      'value',
      'Keep me.'
    )
  })

  it('preserves a replayed locked note when confirming the remaining batch answer', async () => {
    const request = renderLiveBatch({ q0: '["red"]' }, true, { q0: 'Previously accepted note' })

    expect(screen.getByRole('textbox', { name: 'Note for red' })).toHaveProperty('value', 'Previously accepted note')
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Release' } })
    fireEvent.click(screen.getByRole('button', { name: /Confirm and continue/ }))

    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    expect(request).toHaveBeenNthCalledWith(1, 'clarify.respond', {
      answer: '["red"]',
      note: 'Previously accepted note',
      question_id: 'q0',
      request_id: 'request-batch'
    })
  })

  it('does not submit a batch note twice while confirmation is pending', async () => {
    const request = renderLiveBatch()
    request.mockImplementation(() => new Promise(() => {}))

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))
    const note = screen.getByRole('textbox', { name: 'Note for red' })
    fireEvent.change(note, { target: { value: 'Ready.' } })
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Release' } })
    fireEvent.keyDown(note, { key: 'Enter' })
    await waitFor(() => expect(request).toHaveBeenCalledTimes(1))

    expect((note as HTMLTextAreaElement).disabled).toBe(true)
    fireEvent.keyDown(note, { key: 'Enter' })
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('advances within its own batch card when another mounted card has the same qids', () => {
    const request = vi.fn().mockResolvedValue({ ok: true })

    const batchView = (sessionId: string): SessionView => ({
      ...({} as SessionView),
      $runtimeId: atom<null | string>(sessionId),
      kind: 'tile'
    })

    // Each card's own args, so each correlates to its own session's request —
    // the qids collide (both `q0`/`q1`), which is the thing under test.
    const cardProps = (label: string, toolCallId: string): ToolCallMessagePartProps => {
      const args: { questions: { question: string; choices?: string[] }[] } = {
        questions: [{ choices: ['red', 'blue'], question: `${label} color?` }, { question: `${label} name?` }]
      }

      return { ...liveBatchProps(), args, argsText: JSON.stringify(args), toolCallId }
    }

    $gateway.set({ request } as never)
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: '',
      questions: [
        { choices: ['red', 'blue'], multiSelect: false, qid: 'q0', question: 'Background color?' },
        { choices: null, multiSelect: false, qid: 'q1', question: 'Background name?' }
      ],
      requestId: 'request-background-batch',
      sessionId: 'session-background-batch'
    })
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: '',
      questions: [
        { choices: ['red', 'blue'], multiSelect: false, qid: 'q0', question: 'Foreground color?' },
        { choices: null, multiSelect: false, qid: 'q1', question: 'Foreground name?' }
      ],
      requestId: 'request-foreground-batch',
      sessionId: 'session-foreground-batch'
    })
    renderClarify(
      <>
        <SessionViewProvider value={batchView('session-background-batch')}>
          <ClarifyTool {...cardProps('Background', 'clarify-batch-background')} />
        </SessionViewProvider>
        <SessionViewProvider value={batchView('session-foreground-batch')}>
          <ClarifyTool {...cardProps('Foreground', 'clarify-batch-foreground')} />
        </SessionViewProvider>
      </>
    )

    const drafts = screen.getAllByPlaceholderText('Other (type your answer)')
    const nextAnswers = screen.getAllByPlaceholderText('Type your answer…')
    fireEvent.change(drafts[1] as HTMLTextAreaElement, { target: { value: 'green' } })
    fireEvent.keyDown(drafts[1] as HTMLTextAreaElement, { key: 'Enter' })

    expect(document.activeElement).toBe(nextAnswers[1])
  })

  it('uses plain Enter to advance a batch draft without creating a newline', () => {
    const request = renderLiveBatch()

    const colorDraft = screen.getByPlaceholderText('Other (type your answer)')
    fireEvent.change(colorDraft, { target: { value: 'green' } })

    expect(fireEvent.keyDown(colorDraft, { key: 'Enter' })).toBe(false)
    expect((colorDraft as HTMLTextAreaElement).value).toBe('green')
    expect(document.activeElement).toBe(screen.getByPlaceholderText('Type your answer…'))
    expect(request).not.toHaveBeenCalled()
  })

  it('cancels plain Enter in an empty batch draft without submitting or moving focus', () => {
    const request = renderLiveBatch()
    const colorDraft = screen.getByPlaceholderText('Other (type your answer)')
    colorDraft.focus()

    expect(fireEvent.keyDown(colorDraft, { key: 'Enter' })).toBe(false)
    expect((colorDraft as HTMLTextAreaElement).value).toBe('')
    expect(document.activeElement).toBe(colorDraft)
    expect(request).not.toHaveBeenCalled()
  })

  it('does not cancel Shift+Enter so the browser can insert a newline in a batch draft', () => {
    renderLiveBatch()

    const colorDraft = screen.getByPlaceholderText('Other (type your answer)')
    fireEvent.change(colorDraft, { target: { value: 'green' } })

    expect(fireEvent.keyDown(colorDraft, { key: 'Enter', shiftKey: true })).toBe(true)
    expect((colorDraft as HTMLTextAreaElement).value).toBe('green')
  })

  it('saves a batch note and advances to the next unanswered question on plain Enter', () => {
    const request = renderLiveBatch()
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))
    const note = screen.getByRole('textbox', { name: 'Note for red' })
    fireEvent.change(note, { target: { value: 'Keep this note.' } })

    expect(fireEvent.keyDown(note, { key: 'Enter' })).toBe(false)
    expect((note as HTMLTextAreaElement).value).toBe('Keep this note.')
    expect(document.activeElement).toBe(screen.getByPlaceholderText('Type your answer…'))
    expect(request).not.toHaveBeenCalled()
  })

  it('leaves Shift+Enter and IME Enter in a batch note to the browser', () => {
    const request = renderLiveBatch()
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))
    const note = screen.getByRole('textbox', { name: 'Note for red' })
    fireEvent.change(note, { target: { value: 'Keep this note.' } })

    expect(fireEvent.keyDown(note, { key: 'Enter', shiftKey: true })).toBe(true)
    expect(fireEvent.keyDown(note, { isComposing: true, key: 'Enter' })).toBe(true)
    expect(request).not.toHaveBeenCalled()
  })

  it('confirms a ready batch from note Enter and includes the note once', async () => {
    const request = renderLiveBatch()
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add note for red' }))
    const note = screen.getByRole('textbox', { name: 'Note for red' })
    fireEvent.change(note, { target: { value: 'Keep this note.' } })
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'Release' } })

    expect(fireEvent.keyDown(note, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2))
    expect(request).toHaveBeenNthCalledWith(1, 'clarify.respond', {
      answer: 'red',
      note: 'Keep this note.',
      question_id: 'q0',
      request_id: 'request-batch'
    })
    expect(request).toHaveBeenNthCalledWith(2, 'clarify.respond', {
      answer: 'Release',
      question_id: 'q1',
      request_id: 'request-batch'
    })
  })

  it('confirm sends every per-question lock in order and completes the batch', async () => {
    const request = renderLiveBatch()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'packet' } })
    fireEvent.submit(document.querySelector('form') as HTMLFormElement)

    await waitFor(() => {
      expect(request).toHaveBeenCalledTimes(2)
    })
    expect(request).toHaveBeenNthCalledWith(1, 'clarify.respond', {
      answer: 'red',
      question_id: 'q0',
      request_id: 'request-batch'
    })
    expect(request).toHaveBeenNthCalledWith(2, 'clarify.respond', {
      answer: 'packet',
      question_id: 'q1',
      request_id: 'request-batch'
    })
  })

  it('a staged answer stays editable before confirm', async () => {
    const request = renderLiveBatch()

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]blue/ }))
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'packet' } })
    fireEvent.submit(document.querySelector('form') as HTMLFormElement)

    await waitFor(() => {
      expect(request).toHaveBeenCalledTimes(2)
    })
    // The re-pick won: blue, not red.
    expect(request).toHaveBeenNthCalledWith(1, 'clarify.respond', {
      answer: 'blue',
      question_id: 'q0',
      request_id: 'request-batch'
    })
  })

  it('pre-stages replayed locked answers from a reconnect', () => {
    renderLiveBatch({ q0: 'red' })

    // The replayed answer counts as staged: one question left to answer.
    expect(screen.getByText('1 of 2 answered')).toBeTruthy()
  })

  it('reselects every choice from a replayed multi-select JSON answer', () => {
    renderLiveBatch({ q0: '["red","blue"]' }, true)

    expect(screen.getByRole('button', { name: /^[A-Z]red/ }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: /^[A-Z]blue/ }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByText('1 of 2 answered')).toBeTruthy()
  })

  it('Skip cancels the whole batch without a question_id', async () => {
    const request = renderLiveBatch()

    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: '',
        request_id: 'request-batch'
      })
    })
  })

  it('renders the settled batch with all questions and answers', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          batchArgs(),
          JSON.stringify({
            responses: [
              { choices_offered: ['red', 'blue'], question: 'Color?', user_response: 'red' },
              { choices_offered: null, question: 'Name?', user_response: '' }
            ]
          }),
          'clarify-batch-settled'
        )}
      />
    )

    expect(screen.getByText('Color?')).toBeTruthy()
    expect(screen.getByText('red')).toBeTruthy()
    expect(screen.getByText('Name?')).toBeTruthy()
    expect(screen.getByText('Skipped')).toBeTruthy()
  })

  it('renders settled selected answers and notes as distinct fields', () => {
    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          batchArgs(),
          JSON.stringify({
            responses: [{ note: 'Use the stable palette.', question: 'Color?', user_response: 'red' }]
          }),
          'clarify-batch-note-settled'
        )}
      />
    )

    expect(screen.getByText('Selected')).toBeTruthy()
    expect(screen.getByText('Note')).toBeTruthy()
    expect(screen.getByText('Use the stable palette.')).toBeTruthy()
  })
})

// ─── Owner routing (#91684 client half) ─────────────────────────────────────
// The clarify card used to answer on the AMBIENT socket. That socket follows
// foreground focus, so after a profile / Bot Chat switch it can be profile B
// while the blocking clarify belongs to profile A — the response lands on a
// backend that never held the request and the owner stays blocked until the
// tool times out. Every live clarify.respond now routes by request.sessionId.

const OWNER_CONNECTION_ID = 'conn-profile-a'
const OWNER_PROFILE = 'profile-a'

/** Profile A owns the clarify's session; the window has since switched to
 *  profile B, so `$gateway` (ambient) is profile B's socket. */
function armCrossProfileOwner() {
  // Two profiles exist → the ambient gateway is not provably the sole backend,
  // so the legacy single-backend escape hatch stays shut.
  $profiles.set([{ name: OWNER_PROFILE }, { name: 'profile-b' }] as never)
  setSessionOwnerHint('session-a', { connectionId: OWNER_CONNECTION_ID, profile: OWNER_PROFILE })

  const ambient = vi.fn().mockResolvedValue({ ok: true })

  $activeSessionId.set('session-a')
  $gateway.set({ request: ambient } as never)

  return ambient
}

function expectOwnerCall(nth: number, params: Record<string, unknown>) {
  expect(gatewayMocks.requestGatewayForAgent).toHaveBeenNthCalledWith(
    nth,
    OWNER_CONNECTION_ID,
    OWNER_PROFILE,
    'clarify.respond',
    params
  )
}

describe('ClarifyTool owner routing', () => {
  afterEach(() => {
    $profiles.set([])
    _resetSessionOwnerHintsForTests({ storage: true })
    gatewayMocks.requestGatewayForAgent.mockClear()
  })

  it('answers a single clarify on the owner socket, never profile B ambient', async () => {
    const ambient = armCrossProfileOwner()

    setClarifyRequest({
      choices: ['staging', 'production'],
      multiSelect: false,
      question: 'Which deployment target?',
      requestId: 'request-1',
      sessionId: 'session-a'
    })
    renderClarify(<ClarifyTool {...liveClarifyProps()} />)

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]staging/ }))
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledTimes(1)
    })
    expectOwnerCall(1, { answer: 'staging', request_id: 'request-1' })
    expect(ambient).not.toHaveBeenCalled()
  })

  it('sends help through the owner socket with the pending runtime session id', async () => {
    const ambient = armCrossProfileOwner()

    setClarifyRequest({
      choices: ['staging', 'production'],
      multiSelect: false,
      question: 'Which deployment target?',
      requestId: 'request-help',
      sessionId: 'session-a'
    })
    renderClarify(<ClarifyTool {...liveClarifyProps()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Why? Which deployment target?' }))

    await waitFor(() => expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledTimes(1))
    expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledWith(
      OWNER_CONNECTION_ID,
      OWNER_PROFILE,
      'clarify.explain',
      {
        request_id: 'request-help',
        session_id: 'session-a',
        version: 1
      }
    )
    expect(ambient).not.toHaveBeenCalled()
  })

  it('keeps the pending choice intact when its owner rejects stale help', async () => {
    const ambient = armCrossProfileOwner()
    gatewayMocks.requestGatewayForAgent.mockRejectedValueOnce(new Error('session not found'))

    setClarifyRequest({
      choices: ['staging', 'production'],
      multiSelect: false,
      question: 'Which deployment target?',
      requestId: 'request-help',
      sessionId: 'session-a'
    })
    renderClarify(<ClarifyTool {...liveClarifyProps()} />)

    const choice = screen.getByRole('button', { name: /^[A-Z]staging/ })
    fireEvent.click(choice)
    fireEvent.click(screen.getByRole('button', { name: 'Why? staging' }))

    await waitFor(() => expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledTimes(1))
    expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledWith(
      OWNER_CONNECTION_ID,
      OWNER_PROFILE,
      'clarify.explain',
      expect.objectContaining({ choice: 'staging', request_id: 'request-help', session_id: 'session-a' })
    )
    expect(
      await screen.findByText('This clarification is no longer available. Return to the conversation and try again.')
    ).toBeTruthy()
    expect(screen.queryByText('session not found')).toBeNull()
    expect(choice.getAttribute('aria-pressed')).toBe('true')
    expect((screen.getByRole('button', { name: /Continue/ }) as HTMLButtonElement).disabled).toBe(false)
    expect(ambient).not.toHaveBeenCalled()
  })

  it('sends both sequential batch locks on the owner socket, in order', async () => {
    const ambient = armCrossProfileOwner()

    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: '',
      questions: [
        { choices: ['red', 'blue'], multiSelect: false, qid: 'q0', question: 'Color?' },
        { choices: null, multiSelect: false, qid: 'q1', question: 'Name?' }
      ],
      requestId: 'request-batch',
      sessionId: 'session-a'
    })
    renderClarify(<ClarifyTool {...liveBatchProps()} />)

    fireEvent.click(screen.getByRole('button', { name: /^[A-Z]red/ }))
    fireEvent.change(screen.getByPlaceholderText('Type your answer…'), { target: { value: 'packet' } })
    fireEvent.click(screen.getByRole('button', { name: /Confirm and continue/ }))

    await waitFor(() => {
      expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledTimes(2)
    })
    // The LAST lock resolves the blocked tool, so order is load-bearing.
    expectOwnerCall(1, { answer: 'red', question_id: 'q0', request_id: 'request-batch' })
    expectOwnerCall(2, { answer: 'packet', question_id: 'q1', request_id: 'request-batch' })
    expect(ambient).not.toHaveBeenCalled()
  })

  it('sends a batch skip/cancel on the owner socket', async () => {
    const ambient = armCrossProfileOwner()

    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: '',
      questions: [
        { choices: ['red', 'blue'], multiSelect: false, qid: 'q0', question: 'Color?' },
        { choices: null, multiSelect: false, qid: 'q1', question: 'Name?' }
      ],
      requestId: 'request-batch',
      sessionId: 'session-a'
    })
    renderClarify(<ClarifyTool {...liveBatchProps()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))

    await waitFor(() => {
      expect(gatewayMocks.requestGatewayForAgent).toHaveBeenCalledTimes(1)
    })
    expectOwnerCall(1, { answer: '', request_id: 'request-batch' })
    expect(ambient).not.toHaveBeenCalled()
  })
})

describe('ClarifyTool visible-card scoping', () => {
  const BACKGROUND_SESSION = 'session-background'
  const FOREGROUND_SESSION = 'session-foreground'
  const BACKGROUND_REQUEST = 'request-background'
  const FOREGROUND_REQUEST = 'request-foreground'
  const ZONE_A_SESSION = 'session-zone-a'
  const ZONE_B_SESSION = 'session-zone-b'
  const ZONE_A_REQUEST = 'request-zone-a'
  const ZONE_B_REQUEST = 'request-zone-b'
  const QUESTION = 'Which deployment target?'

  afterEach(() => {
    $activeTreeGroup.set(null)
    $hoveredTreeGroup.set(null)
  })

  /** Minimal per-session view — the pending card only reads `$runtimeId`. */
  function tileView(sessionId: string): SessionView {
    return { ...({} as SessionView), $runtimeId: atom<null | string>(sessionId), kind: 'tile' }
  }

  function pendingCardProps(toolCallId: string): ToolCallMessagePartProps {
    const args = { choices: ['staging', 'production'], question: QUESTION }

    return { ...liveClarifyProps(), args, argsText: JSON.stringify(args), toolCallId }
  }

  function parkClarify(requestId: string, sessionId: string) {
    setClarifyRequest({
      choices: ['staging', 'production'],
      multiSelect: false,
      question: QUESTION,
      requestId,
      sessionId
    })
  }

  /** A card inside an inactive tab layer — mounted and live, just not on screen. */
  function backgroundCard() {
    return (
      <div {...hiddenPaneProps(true)}>
        <SessionViewProvider value={tileView(BACKGROUND_SESSION)}>
          <ClarifyTool {...pendingCardProps('clarify-background')} />
        </SessionViewProvider>
      </div>
    )
  }

  it('answers the visible card, not a background one that mounted first', async () => {
    const request = vi.fn().mockResolvedValue({ ok: true })

    $gateway.set({ request } as never)
    parkClarify(BACKGROUND_REQUEST, BACKGROUND_SESSION)
    parkClarify(FOREGROUND_REQUEST, FOREGROUND_SESSION)

    // The background card is rendered FIRST, so its window listener registers
    // first. Registration order used to decide the winner, which meant the card
    // the user was looking at lost to one parked in an inactive tab.
    renderClarify(
      <>
        {backgroundCard()}
        <SessionViewProvider value={tileView(FOREGROUND_SESSION)}>
          <ClarifyTool {...pendingCardProps('clarify-foreground')} />
        </SessionViewProvider>
      </>
    )

    fireEvent.keyDown(window, { key: 'Enter' })

    await waitFor(() => {
      expect(request).toHaveBeenCalledTimes(1)
    })

    // Exactly one answer, carrying the FOREGROUND request id — the background
    // session's turn must not be resumed by a keystroke aimed at this one.
    expect(request).toHaveBeenCalledWith('clarify.respond', {
      answer: 'staging',
      request_id: FOREGROUND_REQUEST
    })
  })

  it('leaves the key alone when the only pending card is hidden', () => {
    const request = vi.fn().mockResolvedValue({ ok: true })

    $gateway.set({ request } as never)
    parkClarify(BACKGROUND_REQUEST, BACKGROUND_SESSION)

    renderClarify(backgroundCard())

    // Untouched (no preventDefault) ⇒ the keystroke stays available to the
    // composer, matching what `clarifyCardOwnsKey` reports with no visible card.
    expect(fireEvent.keyDown(window, { key: 'Enter' })).toBe(true)
    expect(request).not.toHaveBeenCalled()
  })

  /** A card in its own split zone — unlike `backgroundCard` this one IS on
   *  screen, so a split renders two cards that both clear the hidden-pane
   *  filter and only the zone ladder can tell apart. */
  function zoneCard(zone: string, sessionId: string) {
    return (
      <div data-tree-group={zone}>
        <SessionViewProvider value={tileView(sessionId)}>
          <ClarifyTool {...pendingCardProps(`clarify-${zone}`)} />
        </SessionViewProvider>
      </div>
    )
  }

  /** Both zones visible, zone-a first in document order. */
  function renderSplit() {
    const request = vi.fn().mockResolvedValue({ ok: true })

    $gateway.set({ request } as never)
    parkClarify(ZONE_A_REQUEST, ZONE_A_SESSION)
    parkClarify(ZONE_B_REQUEST, ZONE_B_SESSION)

    renderClarify(
      <>
        {zoneCard('zone-a', ZONE_A_SESSION)}
        {zoneCard('zone-b', ZONE_B_SESSION)}
      </>
    )

    return request
  }

  it('answers the later-in-document card when its zone is the focused one', async () => {
    const request = renderSplit()

    $activeTreeGroup.set('zone-b')
    fireEvent.keyDown(window, { key: 'Enter' })

    await waitFor(() => {
      expect(request).toHaveBeenCalledTimes(1)
    })

    // Both cards are visible and both hold a live window listener, so this is
    // the case document order gets wrong: it would answer zone-a's question.
    expect(request).toHaveBeenCalledWith('clarify.respond', {
      answer: 'staging',
      request_id: ZONE_B_REQUEST
    })
  })

  it('answers the other visible card once the focus moves to its zone', async () => {
    const request = renderSplit()

    $activeTreeGroup.set('zone-a')
    fireEvent.keyDown(window, { key: 'Enter' })

    await waitFor(() => {
      expect(request).toHaveBeenCalledTimes(1)
    })

    // The direct pin for "the other visible card then cannot receive its
    // shortcut": neither zone may be permanently starved of its own keys.
    expect(request).toHaveBeenCalledWith('clarify.respond', {
      answer: 'staging',
      request_id: ZONE_A_REQUEST
    })
  })
})

// ─── Pending clarify with no correlated request (the buried-payload bug) ─────
//
// Field incident (session 20260907_234154_4b2f48): the provider tool call was
// persisted with args carrying the question and three choices, the session tab
// showed the amber needs-input mark for ~55 minutes, and the transcript showed
// only the generic "Asked a question" row whose question was reachable ONLY by
// expanding raw TOOL PAYLOAD. The renderer's `$clarifyRequests` entry was gone
// (repeated WS drops across the wait) while the tool itself was still pending,
// so the card's own gate — `!messageRunning && !request` — demoted a live
// question to a JSON dump with no answer affordance.
//
// The contract these tests pin: a clarify tool call whose `result` is still
// undefined and whose args carry a question NEVER renders as the generic tool
// fallback. It renders the dedicated card, inert until an owned request id
// exists, and nothing may be submitted from that inert state.

const BURIED_QUESTION = 'Where should the polished launcher live?'
const BURIED_CHOICES = ['Nav page plus Overview strip', 'Nav page only', 'Overview page only']

function uncorrelatedSingleProps(): ToolCallMessagePartProps {
  const args = { choices: BURIED_CHOICES, question: BURIED_QUESTION }

  return {
    addResult: vi.fn(),
    args,
    argsText: JSON.stringify(args),
    isError: false,
    respondToApproval: vi.fn(),
    result: undefined,
    resume: vi.fn(),
    status: { type: 'running' },
    toolCallId: 'clarify-uncorrelated',
    toolName: 'clarify',
    type: 'tool-call'
  }
}

/** The field shape: batch args (a `questions` array, no top-level question). */
function uncorrelatedBatchProps(): ToolCallMessagePartProps {
  const args = { questions: [{ choices: BURIED_CHOICES, question: BURIED_QUESTION }] }

  return {
    ...uncorrelatedSingleProps(),
    args,
    argsText: JSON.stringify(args),
    toolCallId: 'clarify-batch-uncorrelated'
  }
}

describe('ClarifyTool pending question never degrades to raw payload', () => {
  it('paints the dedicated card from args when the turn reports not-running and no request is parked', () => {
    // Exactly the incident state: tool pending, args intact, store empty.
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    renderClarify(<ClarifyTool {...uncorrelatedSingleProps()} />)

    expect(document.querySelector('[data-slot="clarify-inline"]')).toBeTruthy()
    expect(screen.getByText(BURIED_QUESTION)).toBeTruthy()

    // The semantic option rows, not the "Why?/Ask" help buttons beside them.
    const options = [...document.querySelectorAll('[data-choice]')].map(el => el.textContent ?? '')

    for (const choice of BURIED_CHOICES) {
      expect(options.some(text => text.includes(choice))).toBe(true)
    }
  })

  it('paints the BATCH questions from args alone (the shape the field incident used)', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    renderClarify(<ClarifyTool {...uncorrelatedBatchProps()} />)

    expect(document.querySelector('[data-slot="clarify-inline"]')).toBeTruthy()
    expect(screen.getByText(BURIED_QUESTION)).toBeTruthy()
    // A bare spinner is the other way to bury the question — also forbidden.
    expect(screen.queryByRole('status', { name: /Loading question/ })).toBeNull()
  })

  it('keeps every control inert and refuses to submit before an owned request exists', async () => {
    messageRunning = false
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)

    renderClarify(<ClarifyTool {...uncorrelatedSingleProps()} />)

    const choices = [...document.querySelectorAll<HTMLButtonElement>('[data-choice]')]
    expect(choices).toHaveLength(BURIED_CHOICES.length)
    expect(choices.every(el => el.hasAttribute('disabled'))).toBe(true)
    expect(screen.getByRole('button', { name: /Skip/ }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: /Continue/ }).hasAttribute('disabled')).toBe(true)
    expect((screen.getByPlaceholderText(/Other/) as HTMLTextAreaElement).disabled).toBe(true)

    // A visible reason for the inert state, not a silently dead form.
    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()

    fireEvent.click(choices[0])
    fireEvent.keyDown(window, { key: 'Enter' })
    fireEvent.keyDown(window, { key: 'a' })

    await waitFor(() => {
      expect(request).not.toHaveBeenCalled()
    })
  })

  it('does not arm the keyboard-shortcut marker while the card is inert', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    renderClarify(<ClarifyTool {...uncorrelatedSingleProps()} />)

    // `clarifyCardOwnsKey` reads this marker to YIELD those keys away from the
    // composer. An inert card that claims them swallows them for nobody, so the
    // user cannot type a message either.
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()
  })

  it('enables the same card in place when the request arrives, with no second row', async () => {
    messageRunning = false
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)

    const { rerender } = renderClarify(<ClarifyTool {...uncorrelatedSingleProps()} />)

    act(() => {
      setClarifyRequest({
        choices: BURIED_CHOICES,
        multiSelect: false,
        question: BURIED_QUESTION,
        requestId: 'request-late',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(<ClarifyTool {...uncorrelatedSingleProps()} />))

    // One card, now answerable — not a second transcript row.
    expect(document.querySelectorAll('[data-slot="clarify-inline"]')).toHaveLength(1)
    expect(document.querySelector('[data-clarify-restoring]')).toBeNull()

    const choice = [...document.querySelectorAll<HTMLButtonElement>('[data-choice]')][1]
    expect(choice.hasAttribute('disabled')).toBe(false)
    fireEvent.click(choice)
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: BURIED_CHOICES[1],
        request_id: 'request-late'
      })
    })
  })

  it('preserves a typed draft across the inert → answerable transition', async () => {
    messageRunning = false
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)

    const props: ToolCallMessagePartProps = {
      ...uncorrelatedSingleProps(),
      args: { question: BURIED_QUESTION },
      argsText: JSON.stringify({ question: BURIED_QUESTION })
    }

    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    const field = screen.getByRole('textbox')
    fireEvent.change(field, { target: { value: 'my own answer' } })

    act(() => {
      setClarifyRequest({
        choices: null,
        multiSelect: false,
        question: BURIED_QUESTION,
        requestId: 'request-draft',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('my own answer')

    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'my own answer',
        request_id: 'request-draft'
      })
    })
  })

  it('still falls back to the generic tool row when the args carry no question at all', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const args = { unrelated: true }
    renderClarify(
      <ClarifyTool
        addResult={vi.fn()}
        args={args}
        argsText={JSON.stringify(args)}
        isError={false}
        respondToApproval={vi.fn()}
        result={undefined}
        resume={vi.fn()}
        status={{ type: 'running' }}
        toolCallId="clarify-empty"
        toolName="clarify"
        type="tool-call"
      />
    )

    // Nothing to paint — an empty clarify shell would be a lie about state.
    expect(document.querySelector('[data-slot="clarify-inline"]')).toBeNull()
  })

  it('keeps a settled result on the settled card instead of resurrecting a form', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    renderClarify(
      <ClarifyTool
        {...settledClarifyProps(
          { choices: BURIED_CHOICES, question: BURIED_QUESTION },
          { question: BURIED_QUESTION, user_response: '' },
          'clarify-settled-terminal'
        )}
      />
    )

    expect(document.querySelector('[data-clarify-settled]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()
    expect(screen.queryByRole('button', { name: /Continue/ })).toBeNull()
  })
})

describe('ClarifyTool newly answerable focus handling', () => {
  function becomeAnswerable(rerender: (ui: ReactNode) => void, props: ToolCallMessagePartProps) {
    act(() => {
      setClarifyRequest({
        choices: BURIED_CHOICES,
        multiSelect: false,
        question: BURIED_QUESTION,
        requestId: 'request-focus',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(<ClarifyTool {...props} />))
  }

  it('moves focus to the card region when nothing else is focused', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedSingleProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    becomeAnswerable(rerender, props)

    // The card itself, not a choice button: focusing a control would announce
    // one option instead of the question AND disarm the card's own arrow /
    // letter / Enter handler, which stands down while a control is focused.
    const card = document.querySelector('[data-clarify-choices]')
    expect(card).toBeTruthy()
    expect(document.activeElement).toBe(card)
    expect(card?.getAttribute('aria-label')).toBe(BURIED_QUESTION)
    // Programmatic target only — it must not add a Tab stop.
    expect(card?.getAttribute('tabindex')).toBe('-1')
  })

  it('never steals focus from a control the user is already using', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedSingleProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    // Stand in for the composer: any focused editable must keep the caret.
    const composer = document.createElement('textarea')
    document.body.append(composer)
    composer.focus()

    becomeAnswerable(rerender, props)

    expect(document.activeElement).toBe(composer)
    composer.remove()
  })

  it('leaves focus alone for a card parked in a background tab', () => {
    messageRunning = false
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedSingleProps()

    const hidden = (ui: ReactNode) => (
      <div {...hiddenPaneProps(true)}>
        <SessionViewProvider value={{ ...({} as SessionView), $runtimeId: atom<null | string>('session-1') }}>
          {ui}
        </SessionViewProvider>
      </div>
    )

    const { rerender } = renderClarify(hidden(<ClarifyTool {...props} />))

    act(() => {
      setClarifyRequest({
        choices: BURIED_CHOICES,
        multiSelect: false,
        question: BURIED_QUESTION,
        requestId: 'request-hidden',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(hidden(<ClarifyTool {...props} />)))

    // A background session becoming answerable must not yank the user's focus
    // out of the chat they are actually looking at.
    expect(document.activeElement).toBe(document.body)
  })
})

describe('ClarifyTool inert card owner isolation', () => {
  /** Minimal per-session view — the pending card only reads `$runtimeId`. */
  function tileView(sessionId: string): SessionView {
    return { ...({} as SessionView), $runtimeId: atom<null | string>(sessionId), kind: 'tile' }
  }

  it('renders exactly one card per session and arms only the one whose request landed', () => {
    messageRunning = false
    $gateway.set({ request: vi.fn().mockResolvedValue({ ok: true }) } as never)

    // Only session-b's request survived the reconnect. Both transcripts still
    // hold a pending clarify tool row, so both paint — but a keystroke or a
    // submit may only reach the one that actually owns a request id.
    setClarifyRequest({
      choices: BURIED_CHOICES,
      multiSelect: false,
      question: BURIED_QUESTION,
      requestId: 'request-b',
      sessionId: 'session-b'
    })

    renderClarify(
      <>
        <SessionViewProvider value={tileView('session-a')}>
          <ClarifyTool {...uncorrelatedSingleProps()} />
        </SessionViewProvider>
        <SessionViewProvider value={tileView('session-b')}>
          <ClarifyTool {...{ ...uncorrelatedSingleProps(), toolCallId: 'clarify-b' }} />
        </SessionViewProvider>
      </>
    )

    // One dedicated card each — never a payload row, never a duplicate.
    expect(document.querySelectorAll('[data-slot="clarify-inline"]')).toHaveLength(2)
    // Exactly one is armed: session-b's. session-a's stays inert and marked.
    expect(document.querySelectorAll('[data-clarify-choices]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-clarify-restoring]')).toHaveLength(1)
  })

  it('paints a hydrated compaction/resume row from args with no live request at all', () => {
    // Post-compaction resume: the transcript is rebuilt from storage, the
    // clarify tool row is still pending, and no clarify.request has replayed
    // yet. The question must be on screen, not behind a payload expander.
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set(null)

    renderClarify(<ClarifyTool {...uncorrelatedBatchProps()} />)

    expect(screen.getByText(BURIED_QUESTION)).toBeTruthy()
    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Confirm and continue/ }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: /Skip/ }).hasAttribute('disabled')).toBe(true)
  })

  it('does not claim to be "restoring" during the submit → tool.complete gap', async () => {
    // Caught live on the served candidate build: answering clears the request
    // a beat before tool.complete swaps in the settled card. `!ready` is true
    // in that window for the right reason, so a plain !ready check told the
    // user their just-sent answer was "restoring — you can answer in a moment".
    const request = vi.fn().mockResolvedValue({ ok: true, remaining: [] })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: '',
      questions: [{ choices: BURIED_CHOICES, multiSelect: false, qid: 'q0', question: BURIED_QUESTION }],
      requestId: 'request-gap',
      sessionId: 'session-1'
    })

    renderClarify(<ClarifyTool {...uncorrelatedBatchProps()} />)
    expect(document.querySelector('[data-clarify-restoring]')).toBeNull()

    fireEvent.click([...document.querySelectorAll<HTMLButtonElement>('[data-choice]')][0])
    fireEvent.click(screen.getByRole('button', { name: /Confirm and continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalled()
    })

    // The request is cleared on success; the card holds until tool.complete.
    await waitFor(() => {
      expect(document.querySelector('[data-clarify-restoring]')).toBeNull()
    })
  })
})

// Review round 1 found three ways the args-only card still misbehaved once it
// was allowed to paint. Each case below is the reviewer's probe, kept.

describe('ClarifyTool pending row correlates to its OWN request', () => {
  const LATER_QUESTION = 'A later unrelated question?'

  function batchProps(question: string, toolCallId: string): ToolCallMessagePartProps {
    const args = { questions: [{ choices: BURIED_CHOICES, question }] }

    return { ...uncorrelatedSingleProps(), args, argsText: JSON.stringify(args), toolCallId }
  }

  it('does not rebind a stale BATCH row to a later request in the same session', () => {
    // The field shape: an old row sat unanswered for ~55 minutes while the turn
    // carried on, so a later question can park its own request on the SAME
    // session. Reading the session's request without correlating it repainted
    // the old row with the new question and armed it — answering the wrong turn.
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const props = batchProps('Old batch question?', 'clarify-old-batch')
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    act(() => {
      setClarifyRequest({
        choices: null,
        multiSelect: false,
        question: '',
        questions: [{ choices: BURIED_CHOICES, multiSelect: false, qid: 'q0', question: LATER_QUESTION }],
        requestId: 'request-later',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(<ClarifyTool {...props} />))

    // Still its own question, still inert — the later request is not its own.
    expect(screen.getByText('Old batch question?')).toBeTruthy()
    expect(screen.queryByText(LATER_QUESTION)).toBeNull()
    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Confirm and continue/ }).hasAttribute('disabled')).toBe(true)
  })

  it('does not rebind a stale SINGLE row to a later request in the same session', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedSingleProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    act(() => {
      setClarifyRequest({
        choices: BURIED_CHOICES,
        multiSelect: false,
        question: LATER_QUESTION,
        requestId: 'request-later-single',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect(screen.getByText(BURIED_QUESTION)).toBeTruthy()
    expect(screen.queryByText(LATER_QUESTION)).toBeNull()
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()
  })

  it('stays bound to its first request when a repeated same-text question follows', async () => {
    // Two turns can ask the identical question. Text alone cannot separate them,
    // so once a row has correlated it keeps ONLY that request id — otherwise the
    // settled/older row silently adopts the newer turn's request.
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)
    setClarifyRequest({
      choices: BURIED_CHOICES,
      multiSelect: false,
      question: BURIED_QUESTION,
      requestId: 'request-first',
      sessionId: 'session-1'
    })

    const props = uncorrelatedSingleProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    // It correlated with request-first; now a NEW turn asks the same words.
    act(() => {
      setClarifyRequest({
        choices: BURIED_CHOICES,
        multiSelect: false,
        question: BURIED_QUESTION,
        requestId: 'request-second',
        sessionId: 'session-1'
      })
    })
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-choices]')).toBeNull()

    fireEvent.click([...document.querySelectorAll<HTMLButtonElement>('[data-choice]')][0])
    fireEvent.keyDown(window, { key: 'Enter' })

    await waitFor(() => {
      expect(request).not.toHaveBeenCalled()
    })
  })

  it('re-arms on the SAME request id replayed after a reconnect', async () => {
    // Sticky-by-id must not block the legitimate case: a reconnect replays the
    // identical request id, and the card has to become answerable again.
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)

    const parked = {
      choices: BURIED_CHOICES,
      multiSelect: false,
      question: BURIED_QUESTION,
      requestId: 'request-replayed',
      sessionId: 'session-1'
    }

    setClarifyRequest(parked)

    const props = uncorrelatedSingleProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    act(() => clearClarifyRequest('request-replayed', 'session-1'))
    rerender(clarifyTree(<ClarifyTool {...props} />))
    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()

    act(() => setClarifyRequest(parked))
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect(document.querySelector('[data-clarify-restoring]')).toBeNull()
    fireEvent.click([...document.querySelectorAll<HTMLButtonElement>('[data-choice]')][0])
    fireEvent.click(screen.getByRole('button', { name: /Continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: BURIED_CHOICES[0],
        request_id: 'request-replayed'
      })
    })
  })
})

describe('ClarifyTool batch card newly answerable focus handling', () => {
  const parkedBatch = {
    choices: null,
    multiSelect: false,
    question: '',
    questions: [{ choices: BURIED_CHOICES, multiSelect: false, qid: 'q0', question: BURIED_QUESTION }],
    requestId: 'request-batch-focus',
    sessionId: 'session-1'
  }

  it('moves focus to the batch card region when nothing else is focused', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedBatchProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    act(() => setClarifyRequest(parkedBatch))
    rerender(clarifyTree(<ClarifyTool {...props} />))

    const card = document.querySelector('[data-clarify-batch]')
    expect(card).toBeTruthy()
    expect(document.activeElement).toBe(card)
    // Announced as a card, and a programmatic target only — never a Tab stop.
    expect(card?.getAttribute('aria-label')).toBeTruthy()
    expect(card?.getAttribute('tabindex')).toBe('-1')
  })

  it('never steals focus from a control the user is already using', () => {
    messageRunning = false
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedBatchProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    const composer = document.createElement('textarea')
    document.body.append(composer)
    composer.focus()

    act(() => setClarifyRequest(parkedBatch))
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect(document.activeElement).toBe(composer)
    composer.remove()
  })

  it('leaves focus alone for a batch card parked in a background tab', () => {
    messageRunning = false
    $gateway.set({ request: vi.fn() } as never)

    const props = uncorrelatedBatchProps()

    const hidden = (ui: ReactNode) => (
      <div {...hiddenPaneProps(true)}>
        <SessionViewProvider value={{ ...({} as SessionView), $runtimeId: atom<null | string>('session-1') }}>
          {ui}
        </SessionViewProvider>
      </div>
    )

    const { rerender } = renderClarify(hidden(<ClarifyTool {...props} />))

    act(() => setClarifyRequest(parkedBatch))
    rerender(clarifyTree(hidden(<ClarifyTool {...props} />)))

    expect(document.activeElement).toBe(document.body)
  })
})

describe('ClarifyTool batch staged state survives the correlation gap', () => {
  const parkedBatch = {
    choices: null,
    multiSelect: false,
    question: '',
    questions: [{ choices: null, multiSelect: false, qid: 'q0', question: BURIED_QUESTION }],
    requestId: 'request-batch-draft',
    sessionId: 'session-1'
  }

  function openEndedBatchProps(): ToolCallMessagePartProps {
    const args = { questions: [{ question: BURIED_QUESTION }] }

    return { ...uncorrelatedSingleProps(), args, argsText: JSON.stringify(args), toolCallId: 'clarify-batch-draft' }
  }

  it('keeps a typed draft and a picked choice across request → args-only → replay', async () => {
    // Batch staged state used to be keyed by the server qid (`q0`). Clearing
    // the request swaps the rendered questions to synthetic `args-0` ids, so
    // the same mounted card rendered an empty field — the user watched their
    // answer disappear while the transport flapped.
    const request = vi.fn().mockResolvedValue({ ok: true })

    $activeSessionId.set('session-1')
    $gateway.set({ request } as never)
    setClarifyRequest(parkedBatch)

    const props = openEndedBatchProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    // Staged while ARMED (a real user can only type into an enabled field).
    const field = screen.getByRole('textbox') as HTMLTextAreaElement
    expect(field.disabled).toBe(false)
    fireEvent.change(field, { target: { value: 'keep this draft' } })

    // The transport drops: the parked request is cleared, the card goes inert.
    act(() => clearClarifyRequest('request-batch-draft', 'session-1'))
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('keep this draft')

    // Replay: same card, same draft, now answerable and submittable.
    act(() => setClarifyRequest(parkedBatch))
    rerender(clarifyTree(<ClarifyTool {...props} />))

    expect(document.querySelectorAll('[data-clarify-batch]')).toHaveLength(1)
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('keep this draft')

    fireEvent.click(screen.getByRole('button', { name: /Confirm and continue/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('clarify.respond', {
        answer: 'keep this draft',
        question_id: 'q0',
        request_id: 'request-batch-draft'
      })
    })
  })

  it('keeps a staged CHOICE and its note across the same gap', () => {
    $activeSessionId.set('session-1')
    $gateway.set({ request: vi.fn().mockResolvedValue({ ok: true }) } as never)
    setClarifyRequest({
      ...parkedBatch,
      questions: [{ choices: BURIED_CHOICES, multiSelect: false, qid: 'q0', question: BURIED_QUESTION }]
    })

    const props = uncorrelatedBatchProps()
    const { rerender } = renderClarify(<ClarifyTool {...props} />)

    const choice = [...document.querySelectorAll<HTMLButtonElement>('[data-choice]')][1]
    expect(choice.hasAttribute('disabled')).toBe(false)
    fireEvent.click(choice)
    expect(document.querySelector('[data-clarify-answered]')).toBeTruthy()

    act(() => clearClarifyRequest('request-batch-draft', 'session-1'))
    rerender(clarifyTree(<ClarifyTool {...props} />))

    // The pick is still visibly staged (the block stays in its answered state)
    // even though the rendered question ids swapped to the synthetic ones.
    expect(document.querySelector('[data-clarify-answered]')).toBeTruthy()
    expect(document.querySelector('[data-clarify-restoring]')).toBeTruthy()
  })
})
