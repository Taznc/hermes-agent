// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConnectionState } from './json-rpc-gateway'
import { JsonRpcGatewayClient } from './json-rpc-gateway'

/**
 * EventTarget-based WebSocket stand-in driven manually from the tests so the
 * connect() dial lifecycle (open/error/close/timeout) can be exercised under
 * vi.useFakeTimers with no real transport.
 */
class FakeWebSocket extends EventTarget {
  static instances: FakeWebSocket[] = []
  static OPEN = 1

  readyState = 0
  url: string

  constructor(url: string) {
    super()
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  close(): void {
    this.readyState = 3
    this.dispatchEvent(new Event('close'))
  }

  // Test driver
  open(): void {
    this.readyState = 1
    this.dispatchEvent(new Event('open'))
  }

  send(): void {
    // connect-lifecycle tests never exchange frames
  }
}

const CONNECT_TIMEOUT_MS = 15_000

const makeClient = () => {
  const states: ConnectionState[] = []

  const client = new JsonRpcGatewayClient({
    connectTimeoutMs: CONNECT_TIMEOUT_MS,
    heartbeatDeadlineMs: 0,
    heartbeatIntervalMs: 0,
    socketFactory: url => new FakeWebSocket(url) as unknown as WebSocket
  })

  client.onState(state => states.push(state))

  return { client, states }
}

/** Track a connect() promise without letting a rejection go unhandled. */
const trackSettle = (promise: Promise<void>) => {
  const outcome = { value: 'pending' as 'pending' | 'rejected' | 'resolved' }

  promise.then(
    () => (outcome.value = 'resolved'),
    () => (outcome.value = 'rejected')
  )

  return outcome
}

describe('JsonRpcGatewayClient connect() dial lifecycle across socket generations', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeWebSocket.instances = []
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('a stale connect-timeout from an abandoned dial never publishes error over a newer open socket, and close() settles the dial', async () => {
    const { client, states } = makeClient()

    // Dial A: the socket never opens (slow/unreachable backend).
    const first = client.connect('ws://a.test/api/ws')
    const firstOutcome = trackSettle(first)
    expect(FakeWebSocket.instances).toHaveLength(1)

    // User applies a connection switch mid-dial (use-gateway-boot softSwitch):
    // close() then connect() to the new target.
    client.close()
    await vi.advanceTimersByTimeAsync(0)

    // The abandoned dial must settle at close() time, not hang until its
    // 15s timer fires.
    expect(firstOutcome.value).toBe('rejected')

    const second = client.connect('ws://b.test/api/ws')
    expect(FakeWebSocket.instances).toHaveLength(2)
    FakeWebSocket.instances[1].open()
    await second
    expect(client.connectionState).toBe('open')

    // Dial A's connect-timeout window elapses AFTER generation B is healthy.
    // The stale timer must not publish 'error' onto the newer socket.
    await vi.advanceTimersByTimeAsync(CONNECT_TIMEOUT_MS + 1_000)
    expect(client.connectionState).toBe('open')
    expect(states).not.toContain('error')
  })

  it('invalidate() during a dial also settles the dial and defuses its timer', async () => {
    const { client } = makeClient()

    const first = client.connect('ws://a.test/api/ws')
    const firstOutcome = trackSettle(first)

    // Ambiguous transport outcome mid-dial: the owner invalidates the
    // generation, then redials.
    client.invalidate('switching backends')
    await vi.advanceTimersByTimeAsync(0)
    expect(firstOutcome.value).toBe('rejected')

    const second = client.connect('ws://b.test/api/ws')
    FakeWebSocket.instances[1].open()
    await second

    await vi.advanceTimersByTimeAsync(CONNECT_TIMEOUT_MS + 1_000)
    expect(client.connectionState).toBe('open')
  })

  it('a redundant connect() against an already-open socket heals a wrongly published state (scenario)', async () => {
    const { client } = makeClient()

    const first = client.connect('ws://a.test/api/ws')
    trackSettle(first)
    client.close()

    const second = client.connect('ws://b.test/api/ws')
    FakeWebSocket.instances[1].open()
    await second

    // Pre-fix, dial A's stale timer flips the healthy connection to 'error'
    // here; the composer's reconnect loop then calls connect() again, which
    // must republish 'open' rather than early-return and latch the error.
    await vi.advanceTimersByTimeAsync(CONNECT_TIMEOUT_MS + 1_000)
    await client.connect('ws://b.test/api/ws')

    expect(FakeWebSocket.instances).toHaveLength(2) // no third dial: socket B is healthy
    expect(client.connectionState).toBe('open')
  })

  it('a redundant connect() against an already-open socket republishes open even if state was latched stale', async () => {
    const { client, states } = makeClient()

    const dial = client.connect('ws://b.test/api/ws')
    FakeWebSocket.instances[0].open()
    await dial
    expect(client.connectionState).toBe('open')

    // Simulate ANY path that latches a stale published state while the
    // transport itself stays healthy (the socket is still OPEN). Reaching
    // into the private field is deliberate: post-fix no public path produces
    // this state, but the heal must hold for the whole bug class.
    ;(client as unknown as { state: ConnectionState }).state = 'error'
    expect(client.connectionState).toBe('error')

    await client.connect('ws://b.test/api/ws')

    expect(FakeWebSocket.instances).toHaveLength(1) // healed, not redialed
    expect(client.connectionState).toBe('open')
    expect(states[states.length - 1]).toBe('open')
  })
})
