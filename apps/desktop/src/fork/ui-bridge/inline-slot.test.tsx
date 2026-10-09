import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import type { ServerRequest } from '@hermes/shared'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import type { FC } from 'react'
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'

import type { SessionView } from '@/app/chat/session-view'
import type { ServerRequestContext } from '@/app/session/hooks/use-message-stream/gateway-event/server-requests'
import { registry } from '@/contrib/registry'
import type { UiRequestRenderProps, UiToolResultRenderProps } from '@/fork/ui-bridge/types'
import type { ChatMessage } from '@/lib/chat-messages'

// The live card holds its row's disclosure open, which reads the message id from the
// assistant-ui store; the transcript provides it in the app.
vi.mock('@assistant-ui/react', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useAuiState: (select: (state: unknown) => unknown) =>
    select({ message: { id: 'msg-1', status: { type: 'running' } }, thread: { isRunning: true } })
}))

const { SessionViewProvider } = await import('@/app/chat/session-view')
const { handleServerRequest } = await import('@/app/session/hooks/use-message-stream/gateway-event/server-requests')
const { MESSAGE_PARTS_COMPONENTS } = await import('@/components/assistant-ui/thread/message-parts')
const { withUiRequestSlot } = await import('@/fork/ui-bridge/inline-slot')
const { $uiRequests, resetUiBridgeForTests } = await import('@/fork/ui-bridge/store')
const { UI_REQUEST_AREA } = await import('@/fork/ui-bridge/types')
const { I18nProvider } = await import('@/i18n')
const { createClientSessionState } = await import('@/lib/chat-runtime')
const { $activeSessionId } = await import('@/store/session')
type RenderProps = UiRequestRenderProps
type ResultProps = UiToolResultRenderProps

const assistant = (id: string, parts: unknown[]): ChatMessage =>
  ({ id, parts, role: 'assistant', timestamp: 0 }) as unknown as ChatMessage

const openCall = (toolCallId: string) =>
  ({ args: { q: 1 }, argsText: '{"q":1}', toolCallId, toolName: 'ask', type: 'tool-call' }) as never

function view(sessionId: string, messages: ChatMessage[]): SessionView {
  return {
    $awaitingResponse: atom(false),
    $busy: atom(true),
    $cwd: atom(''),
    $fast: atom(false),
    $lastVisibleIsUser: atom(false),
    $messages: atom(messages),
    $messagesEmpty: atom(false),
    $model: atom(''),
    $provider: atom(''),
    $reasoningEffort: atom(''),
    $reasoningEffortPending: atom(false),
    $reasoningEffortWire: atom(''),
    $runtimeId: atom(sessionId),
    $storedId: atom(`stored-${sessionId}`),
    $turnStartedAt: atom(null),
    kind: 'primary'
  }
}

/** Deliver a `plugin.request` through the real SERVER_REQUEST_HANDLERS entry. */
function deliver(id: string, sessionId: string, messages: ChatMessage[], payload: unknown) {
  const request = {
    fail: vi.fn(),
    id,
    method: 'plugin.request',
    params: { kind: 'fork-ask/questions', payload, session_id: sessionId },
    profile: 'default',
    replayed: false,
    respond: vi.fn()
  } satisfies ServerRequest & { profile: string }

  let state = createClientSessionState(sessionId, messages)

  const deps: ServerRequestContext['deps'] = {
    activeSessionIdRef: { current: sessionId },
    sessionInterrupted: () => false,
    sessionStateByRuntimeIdRef: { current: new Map() },
    updateSessionState: (_sid, update) => (state = update(state)),
    upsertToolCall: () => undefined
  }

  handleServerRequest(request, deps, sessionId)

  return request
}

function Card({ params, respond, cancel, isActive, sessionId }: RenderProps) {
  const q = (params as { question?: string } | null)?.question

  return (
    <div data-active={String(isActive)} data-session={sessionId} data-testid="card">
      {q}
      <button onClick={() => respond({ answer: 'blue' })} type="button">
        answer
      </button>
      <button onClick={cancel} type="button">
        dismiss
      </button>
    </div>
  )
}

function Settled({ result, toolCallId }: ResultProps) {
  return <div data-testid="settled">{`${toolCallId}: ${String(result)}`}</div>
}

const StubRow: FC<{ toolCallId: string }> = ({ toolCallId }) => <div data-testid="row">{`row ${toolCallId}`}</div>
const SlottedStub = withUiRequestSlot(StubRow)

const rowProps = (toolCallId: string, result?: unknown): ToolCallMessagePartProps =>
  ({
    addResult: vi.fn(),
    args: { q: 1 },
    argsText: '{"q":1}',
    isError: false,
    respondToApproval: vi.fn(),
    result,
    resume: vi.fn(),
    status: result === undefined ? { type: 'running' } : { type: 'complete' },
    toolCallId,
    toolName: 'ask',
    type: 'tool-call'
  }) as ToolCallMessagePartProps

let dispose: () => void = () => undefined

beforeEach(() => {
  resetUiBridgeForTests()
  dispose = registry.register({
    area: UI_REQUEST_AREA,
    data: { kind: 'fork-ask/questions', render: Card, renderResult: Settled, tool: 'ask' },
    id: 'ask:questions'
  })
})

afterEach(() => {
  cleanup()
  dispose()
  resetUiBridgeForTests()
  $activeSessionId.set(null)
})

describe('inline slot (mounted)', () => {
  it('renders the contributor card under its own tool row and answers {payload} in place', () => {
    const messages = [assistant('a1', [openCall('c1')])]
    const request = deliver('srq-1', 's-a', messages, { question: 'favourite colour?' })
    $activeSessionId.set('s-a')

    render(
      <SessionViewProvider value={view('s-a', messages)}>
        <SlottedStub {...rowProps('c1')} />
      </SessionViewProvider>
    )

    const row = screen.getByTestId('row')
    const card = screen.getByTestId('card')
    expect(card.textContent).toContain('favourite colour?')
    expect(card.dataset.session).toBe('s-a')
    expect(card.dataset.active).toBe('true')
    // The card follows the stock row: the row stays, the card is below it.
    expect(row.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    act(() => fireEvent.click(screen.getByText('answer')))

    expect(request.respond).toHaveBeenCalledWith({ payload: { answer: 'blue' } })
    expect(request.fail).not.toHaveBeenCalled()
    expect($uiRequests.get()).toEqual({})
    expect(screen.queryByTestId('card')).toBeNull()
    expect(screen.getByTestId('row')).toBeTruthy()
  })

  it('dismisses as unanswered (JSON-RPC error) when the card cancels', () => {
    const messages = [assistant('a1', [openCall('c1')])]
    const request = deliver('srq-1', 's-a', messages, {})

    render(
      <SessionViewProvider value={view('s-a', messages)}>
        <SlottedStub {...rowProps('c1')} />
      </SessionViewProvider>
    )

    act(() => fireEvent.click(screen.getByText('dismiss')))

    expect(request.fail).toHaveBeenCalledTimes(1)
    expect(request.respond).not.toHaveBeenCalled()
    expect(screen.queryByTestId('card')).toBeNull()
  })

  it("does not paint a background session's card into the foreground row", () => {
    // Same tool call id in both transcripts: session scoping, not id luck, keeps it out.
    const background = [assistant('b1', [openCall('c1')])]
    deliver('srq-bg', 's-bg', background, { question: 'bg?' })

    render(
      <SessionViewProvider value={view('s-fg', [assistant('f1', [openCall('c1')])])}>
        <SlottedStub {...rowProps('c1')} />
      </SessionViewProvider>
    )

    expect(screen.queryByTestId('card')).toBeNull()
    expect(screen.getByTestId('row')).toBeTruthy()
    expect(Object.keys($uiRequests.get())).toEqual(['srq-bg'])
  })

  it('rehydrates from the tool RESULT alone: renderResult draws the settled row, no request needed', () => {
    // Fresh window: nothing parked, the transcript came back from history with a result.
    expect($uiRequests.get()).toEqual({})

    render(
      <SessionViewProvider value={view('s-a', [])}>
        <SlottedStub {...rowProps('c1', '{"answer":"blue"}')} />
      </SessionViewProvider>
    )

    expect(screen.getByTestId('settled').textContent).toBe('c1: {"answer":"blue"}')
    expect(screen.queryByTestId('card')).toBeNull()
    expect(screen.queryByTestId('row')).toBeNull()
  })

  it('falls back to the stock row for a settled tool no contributor claims', () => {
    dispose()
    dispose = registry.register({
      area: UI_REQUEST_AREA,
      data: { kind: 'fork-ask/questions', render: Card },
      id: 'ask:questions'
    })

    render(
      <SessionViewProvider value={view('s-a', [])}>
        <SlottedStub {...rowProps('c1', 'done')} />
      </SessionViewProvider>
    )

    expect(screen.getByTestId('row')).toBeTruthy()
    expect(screen.queryByTestId('settled')).toBeNull()
  })

  it('is wired into the transcript through the message-parts.tsx anchor', () => {
    // The live stock row plays an enter animation; jsdom has no Web Animations API.
    Object.defineProperty(HTMLElement.prototype, 'animate', { configurable: true, value: vi.fn() })
    onTestFinished(() => void Reflect.deleteProperty(HTMLElement.prototype, 'animate'))
    const messages = [assistant('a1', [openCall('c1')])]
    deliver('srq-1', 's-a', messages, { question: 'via anchor?' })
    const Fallback = MESSAGE_PARTS_COMPONENTS.tools.Fallback

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <SessionViewProvider value={view('s-a', messages)}>
          <Fallback {...rowProps('c1')} />
        </SessionViewProvider>
      </I18nProvider>
    )

    const card = screen.getByTestId('card')
    expect(card.textContent).toContain('via anchor?')
    expect(card.closest('[data-fork-ui-request="fork-ask/questions"]')).toBeTruthy()
  })
})
