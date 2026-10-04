import type { ServerRequest } from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  handleServerRequest,
  type ServerRequestContext
} from '@/app/session/hooks/use-message-stream/gateway-event/server-requests'
import type { ClientSessionState } from '@/app/types'
import { emitGatewayEvent, onGatewayEvent } from '@/contrib/events'
import { registry } from '@/contrib/registry'
import { $diskPluginsScanPending } from '@/contrib/runtime-loader'
import { parkedForRow } from '@/fork/ui-bridge/inline-slot'
import {
  $uiRequests,
  cancelUiRequest,
  isUiRequestDeferred,
  resetUiBridgeForTests,
  respondUiRequest
} from '@/fork/ui-bridge/store'
import { PLUGIN_EVENT_TYPE, UI_REQUEST_AREA } from '@/fork/ui-bridge/types'
import type { ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'

const toolCall = (toolCallId: string, extra: Record<string, unknown> = {}) =>
  ({ args: {}, argsText: '{}', toolCallId, toolName: 'ask', type: 'tool-call', ...extra }) as never

const assistant = (id: string, parts: unknown[]): ChatMessage =>
  ({ id, parts, role: 'assistant', timestamp: 0 }) as unknown as ChatMessage

const user = (id: string): ChatMessage =>
  ({ id, parts: [{ text: 'hi', type: 'text' }], role: 'user', timestamp: 0 }) as unknown as ChatMessage

function harness(transcripts: Record<string, ChatMessage[]>, activeSessionId: null | string) {
  const states = new Map<string, ClientSessionState>(
    Object.entries(transcripts).map(([sid, messages]) => [sid, createClientSessionState(sid, messages)])
  )

  const deps: ServerRequestContext['deps'] = {
    activeSessionIdRef: { current: activeSessionId },
    sessionInterrupted: () => false,
    sessionStateByRuntimeIdRef: { current: states },
    updateSessionState: (sid, update) => {
      const next = update(states.get(sid) ?? createClientSessionState(sid))
      states.set(sid, next)

      return next
    },
    upsertToolCall: () => undefined
  }

  const deliver = (id: string, params: Record<string, unknown>, replayed = false) => {
    const request = {
      fail: vi.fn(),
      id,
      method: 'plugin.request',
      params,
      profile: 'default',
      replayed,
      respond: vi.fn()
    } satisfies ServerRequest & { profile: string }

    return { handled: handleServerRequest(request, deps, activeSessionId), request }
  }

  return { deliver, states }
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

let disposeContribution: () => void = () => undefined

beforeEach(() => {
  resetUiBridgeForTests()
  disposeContribution = registry.register({
    area: UI_REQUEST_AREA,
    data: { kind: 'fork-ask/questions', render: () => null },
    id: 'ask:questions'
  })
})

afterEach(() => {
  disposeContribution()
  resetUiBridgeForTests()
})

describe('plugin.request handler', () => {
  it('answers {unsupported: true} promptly when no Desktop plugin renders the kind', async () => {
    const { deliver } = harness({ 's-a': [] }, 's-a')
    const { handled, request } = deliver('srq-1', { kind: 'other/thing', payload: {}, session_id: 's-a' })

    expect(handled).toBe(true)
    // Synchronous: the backend's `ui.request` must not stall on a kind nobody renders.
    expect(request.respond).toHaveBeenCalledWith({ unsupported: true })
    expect(request.fail).not.toHaveBeenCalled()
    expect($uiRequests.get()).toEqual({})
  })

  it('parks a background session card under that session only — it never lands in the foreground', () => {
    const foreground = [user('u1'), assistant('a1', [toolCall('fg-call')])]
    const background = [user('u2'), assistant('a2', [toolCall('bg-call')])]
    const { deliver, states } = harness({ 's-bg': background, 's-fg': foreground }, 's-fg')

    deliver('srq-bg', { kind: 'fork-ask/questions', payload: { q: 1 }, session_id: 's-bg' })

    const parked = $uiRequests.get()['srq-bg']
    expect(parked).toMatchObject({ kind: 'fork-ask/questions', params: { q: 1 }, sessionId: 's-bg', toolCallId: 'bg-call' })
    // The foreground's own open tool row does not claim it…
    expect(parkedForRow($uiRequests.get(), 's-fg', 'fg-call', () => foreground)).toBeNull()
    // …the background row does, and only the background session is flagged.
    expect(parkedForRow($uiRequests.get(), 's-bg', 'bg-call', () => background)?.requestId).toBe('srq-bg')
    expect(states.get('s-bg')?.needsInput).toBe(true)
    expect(states.get('s-fg')?.needsInput).toBe(false)
  })

  it('answers {payload} through the request and dismisses as unanswered on cancel', () => {
    const { deliver } = harness({ 's-a': [assistant('a1', [toolCall('c1')])] }, 's-a')
    const first = deliver('srq-1', { kind: 'fork-ask/questions', payload: {}, session_id: 's-a' }).request

    expect(respondUiRequest('srq-1', { answers: ['x'] })).toBe(true)
    expect(first.respond).toHaveBeenCalledWith({ payload: { answers: ['x'] } })
    expect(respondUiRequest('srq-1', 'again')).toBe(false)

    const second = deliver('srq-2', { kind: 'fork-ask/questions', payload: {}, session_id: 's-a' }).request
    expect(cancelUiRequest('srq-2')).toBe(true)
    expect(second.fail).toHaveBeenCalledTimes(1)
    expect(second.respond).not.toHaveBeenCalled()
    expect($uiRequests.get()).toEqual({})
  })

  it('re-renders a pending request after a reconnect replay (open_requests), answering the new socket', () => {
    const transcript = [user('u1'), assistant('a1', [toolCall('c1')])]
    const { deliver } = harness({ 's-a': transcript }, 's-a')
    const live = deliver('srq-1', { kind: 'fork-ask/questions', payload: { v: 1 }, session_id: 's-a' }).request
    const replay = deliver('srq-1', { kind: 'fork-ask/questions', payload: { v: 1 }, session_id: 's-a' }, true).request

    expect(Object.keys($uiRequests.get())).toEqual(['srq-1'])
    expect(parkedForRow($uiRequests.get(), 's-a', 'c1', () => transcript)?.requestId).toBe('srq-1')

    respondUiRequest('srq-1', 'ok')
    expect(replay.respond).toHaveBeenCalledWith({ payload: 'ok' })
    expect(live.respond).not.toHaveBeenCalled()
  })

  it('re-arms a row a stop sealed and lets a replay that beat hydration be claimed by the open row', () => {
    const sealed = [user('u1'), assistant('a1', [toolCall('c1', { completedAt: 5 })])]
    const { deliver, states } = harness({ 's-a': sealed, 's-cold': [] }, 's-a')

    deliver('srq-1', { kind: 'fork-ask/questions', payload: {}, session_id: 's-a' }, true)
    const rearmed = states.get('s-a')!.messages[1].parts[0] as { completedAt?: number }
    expect(rearmed.completedAt).toBeUndefined()
    expect($uiRequests.get()['srq-1'].toolCallId).toBe('c1')

    // Cold session: nothing hydrated yet, so the tool call is unresolved at park time…
    deliver('srq-cold', { kind: 'fork-ask/questions', payload: {}, session_id: 's-cold' }, true)
    expect($uiRequests.get()['srq-cold'].toolCallId).toBeUndefined()

    // …and the hydrated transcript's open row claims it; a settled row does not.
    const hydrated = [user('u9'), assistant('a9', [toolCall('done', { result: 'r' }), toolCall('open')])]
    expect(parkedForRow($uiRequests.get(), 's-cold', 'open', () => hydrated)?.requestId).toBe('srq-cold')
    expect(parkedForRow($uiRequests.get(), 's-cold', 'done', () => hydrated)).toBeNull()
  })
})

describe('teardown via the host.onEvent tap', () => {
  it('clears a parked card on interrupt/timeout (request.cancel) and on turn end, per session', () => {
    const { deliver } = harness(
      { 's-a': [assistant('a', [toolCall('ca')])], 's-b': [assistant('b', [toolCall('cb')])] },
      's-a'
    )

    deliver('srq-a1', { kind: 'fork-ask/questions', payload: {}, session_id: 's-a' })
    deliver('srq-b1', { kind: 'fork-ask/questions', payload: {}, session_id: 's-b' })

    emitGatewayEvent({
      payload: { id: 'srq-a1', method: 'plugin.request', reason: 'interrupted' },
      session_id: 's-a',
      type: 'request.cancel'
    })
    expect(Object.keys($uiRequests.get())).toEqual(['srq-b1'])

    deliver('srq-a2', { kind: 'fork-ask/questions', payload: {}, session_id: 's-a' })
    emitGatewayEvent({ payload: { settled: true, text: '' } as never, session_id: 's-b', type: 'message.complete' })
    expect(Object.keys($uiRequests.get())).toEqual(['srq-a2'])

    emitGatewayEvent({
      payload: { reason: 'idle', session_id: 's-a', stored_session_id: 'x' },
      type: 'session.reclaimed'
    })
    expect($uiRequests.get()).toEqual({})
  })

  it('delivers plugin.event to host.onEvent subscribers', async () => {
    const seen = vi.fn()
    const off = onGatewayEvent(PLUGIN_EVENT_TYPE, seen)

    emitGatewayEvent({
      payload: { kind: 'ask/progress', payload: { step: 1 } },
      session_id: 's-a',
      type: PLUGIN_EVENT_TYPE
    } as never)
    off()
    await flush()

    expect(seen).toHaveBeenCalledWith(
      expect.objectContaining({ payload: { kind: 'ask/progress', payload: { step: 1 } } })
    )
  })
})

describe('requests deferred behind the boot disk-plugin scan', () => {
  let disposeLate: () => void = () => undefined

  beforeEach(() => $diskPluginsScanPending.set(true))

  afterEach(() => {
    disposeLate()
    disposeLate = () => undefined
    $diskPluginsScanPending.set(false)
  })

  const registerLate = () => {
    disposeLate = registry.register({
      area: UI_REQUEST_AREA,
      data: { kind: 'late/kind', render: () => null },
      id: 'late'
    })
  }

  it('decides once the scan finishes: parks when a late plugin renders the kind, else {unsupported: true}', () => {
    const { deliver } = harness({ 's-a': [assistant('a', [toolCall('ca')])] }, 's-a')
    const late = deliver('srq-late', { kind: 'late/kind', payload: {}, session_id: 's-a' }).request
    const none = deliver('srq-none', { kind: 'nobody/kind', payload: {}, session_id: 's-a' }).request

    expect(isUiRequestDeferred('srq-late')).toBe(true)
    expect(late.respond).not.toHaveBeenCalled()
    expect($uiRequests.get()).toEqual({})

    registerLate()
    $diskPluginsScanPending.set(false)

    expect(isUiRequestDeferred('srq-late')).toBe(false)
    expect($uiRequests.get()['srq-late']).toMatchObject({ kind: 'late/kind', sessionId: 's-a', toolCallId: 'ca' })
    expect(none.respond).toHaveBeenCalledWith({ unsupported: true })
  })

  it('never parks a request cancelled (request.cancel) before the scan finishes', () => {
    const { deliver } = harness({ 's-a': [assistant('a', [toolCall('ca')])] }, 's-a')
    const { request } = deliver('srq-late', { kind: 'late/kind', payload: {}, session_id: 's-a' })

    emitGatewayEvent({
      payload: { id: 'srq-late', method: 'plugin.request', reason: 'timeout' },
      session_id: 's-a',
      type: 'request.cancel'
    })
    expect(isUiRequestDeferred('srq-late')).toBe(false)

    registerLate()
    $diskPluginsScanPending.set(false)

    expect($uiRequests.get()).toEqual({})
    expect(request.respond).not.toHaveBeenCalled()
    expect(request.fail).not.toHaveBeenCalled()
  })

  it('never parks a request whose session ended before the scan finishes; other sessions still park', () => {
    const { deliver } = harness(
      { 's-a': [assistant('a', [toolCall('ca')])], 's-b': [assistant('b', [toolCall('cb')])] },
      's-a'
    )

    const ended = deliver('srq-a', { kind: 'late/kind', payload: {}, session_id: 's-a' }).request
    deliver('srq-b', { kind: 'late/kind', payload: {}, session_id: 's-b' })
    const reclaimed = deliver('srq-a2', { kind: 'late/kind', payload: {}, session_id: 's-c' }).request

    emitGatewayEvent({ payload: { settled: true, text: '' } as never, session_id: 's-a', type: 'message.complete' })
    emitGatewayEvent({
      payload: { reason: 'idle', session_id: 's-c', stored_session_id: 'x' },
      type: 'session.reclaimed'
    })

    registerLate()
    $diskPluginsScanPending.set(false)

    expect(Object.keys($uiRequests.get())).toEqual(['srq-b'])
    expect(ended.respond).not.toHaveBeenCalled()
    expect(reclaimed.respond).not.toHaveBeenCalled()
  })
})
