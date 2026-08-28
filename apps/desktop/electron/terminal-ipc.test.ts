import assert from 'node:assert/strict'

import { beforeEach, describe, test, vi } from 'vitest'

// Fakes for the two native surfaces registerTerminalIpc touches directly:
// Electron's ipcMain/app and node-pty's spawn. Both live in one vi.hoisted so
// the mock factories below (which vi.mock hoists above these definitions,
// and above the module's own imports) see fully-initialized objects. This is
// also why FakePty implements its own tiny emitter instead of extending
// node:events' EventEmitter — an imported binding is not yet initialized at
// the point vi.mock's hoisted factory runs.
const { electronMock, nodePtyMock } = vi.hoisted(() => {
  const handlers = new Map<string, (event: unknown, ...args: unknown[]) => unknown>()

  const electron = {
    app: {
      getPath: () => '/home/test',
      getVersion: () => '0.0.0-test'
    },
    handlers,
    ipcMain: {
      handle: (channel: string, handler: (event: unknown, ...args: unknown[]) => unknown) => {
        handlers.set(channel, handler)
      }
    }
  }

  class FakePty {
    pid = 4242
    killed = false
    private exitListeners: Array<(info: { exitCode: number; signal?: number }) => void> = []

    onData() {
      // Data forwarding is not exercised by these tests.
    }

    onExit(listener: (info: { exitCode: number; signal?: number }) => void) {
      this.exitListeners.push(listener)
    }

    write() {}

    resize() {}

    kill() {
      this.killed = true
    }

    fireExit(exitCode = 0) {
      for (const listener of this.exitListeners) {
        listener({ exitCode })
      }
    }
  }

  const spawned: InstanceType<typeof FakePty>[] = []

  const nodePty = {
    spawn: vi.fn(() => {
      const pty = new FakePty()
      spawned.push(pty)

      return pty
    }),
    spawned
  }

  return { electronMock: electron, nodePtyMock: nodePty }
})

vi.mock('electron', () => ({ app: electronMock.app, ipcMain: electronMock.ipcMain }))
vi.mock('node-pty', () => ({ default: nodePtyMock }))

const { registerTerminalIpc } = await import('./terminal-ipc')

// Minimal fake webContents/sender: tracks 'destroyed' listener count (the
// regression this whole file exists to pin) and lets a test fire it. A plain
// object with its own listener array — not node:events' EventEmitter — since
// it only needs once()/emit() for a single event name.
function makeFakeSender(id: number) {
  const destroyedListeners: Array<() => void> = []
  let destroyed = false

  return {
    id,
    isDestroyed: () => destroyed,
    listenerCount: (_event: string) => destroyedListeners.length,
    destroy() {
      destroyed = true

      for (const listener of [...destroyedListeners]) {
        listener()
      }
    },
    once: (_event: string, listener: () => void) => {
      destroyedListeners.push(listener)
    },
    send: vi.fn()
  }
}

function makeDeps() {
  return {
    activeSshTerminalTarget: () => null,
    ensureBackend: async () => undefined,
    findOnPath: () => null,
    getSshConnectionState: () => undefined,
    isWindows: false,
    rememberLog: () => {}
  }
}

async function startTerminal(sender: ReturnType<typeof makeFakeSender>) {
  const start = electronMock.handlers.get('hermes:terminal:start')

  if (!start) {
    throw new Error('hermes:terminal:start handler not registered')
  }

  return start({ sender }, {}) as Promise<{ id: string }>
}

beforeEach(() => {
  electronMock.handlers.clear()
  nodePtyMock.spawn.mockClear()
  nodePtyMock.spawned.length = 0
})

describe('registerTerminalIpc destroyed-listener lifecycle', () => {
  test('opening many terminals on one webContents installs exactly one destroyed listener', async () => {
    registerTerminalIpc(makeDeps())
    const sender = makeFakeSender(1)

    for (let i = 0; i < 20; i += 1) {
      // eslint-disable-next-line no-await-in-loop -- sequential IPC calls mirror real start-then-start usage
      await startTerminal(sender)
    }

    assert.equal(
      sender.listenerCount('destroyed'),
      1,
      'one shared destroyed listener regardless of how many terminals were opened'
    )
  })

  test('disposing a session removes it from the webContents group without leaking the listener', async () => {
    const api = registerTerminalIpc(makeDeps())
    const sender = makeFakeSender(2)

    const { id: firstId } = await startTerminal(sender)
    await startTerminal(sender)

    assert.equal(sender.listenerCount('destroyed'), 1)

    api.disposeTerminalSession(firstId)

    // Disposing one of two sessions must not remove the shared listener while
    // a sibling session on the same webContents is still alive.
    assert.equal(sender.listenerCount('destroyed'), 1)
    assert.equal(nodePtyMock.spawned[0].killed, true)
  })

  test('sender destroyed disposes every session that webContents owns', async () => {
    registerTerminalIpc(makeDeps())
    const sender = makeFakeSender(3)

    await startTerminal(sender)
    await startTerminal(sender)
    await startTerminal(sender)

    assert.equal(nodePtyMock.spawned.length, 3)
    assert.ok(nodePtyMock.spawned.every(pty => !pty.killed))

    sender.destroy()

    assert.ok(nodePtyMock.spawned.every(pty => pty.killed), 'every PTY owned by the destroyed sender is killed')
  })

  test('a PTY exit on its own removes the session without touching the shared listener', async () => {
    registerTerminalIpc(makeDeps())
    const sender = makeFakeSender(4)

    await startTerminal(sender)
    await startTerminal(sender)

    nodePtyMock.spawned[0].fireExit(0)

    assert.equal(sender.listenerCount('destroyed'), 1, 'exit of one session leaves the shared listener installed')

    // The still-alive sibling must still be torn down when the sender goes away.
    sender.destroy()
    assert.equal(nodePtyMock.spawned[1].killed, true)
  })

  test('independent webContents each get their own destroyed listener', async () => {
    registerTerminalIpc(makeDeps())
    const senderA = makeFakeSender(10)
    const senderB = makeFakeSender(11)

    await startTerminal(senderA)
    await startTerminal(senderB)

    assert.equal(senderA.listenerCount('destroyed'), 1)
    assert.equal(senderB.listenerCount('destroyed'), 1)

    senderA.destroy()

    assert.equal(nodePtyMock.spawned[0].killed, true)
    assert.equal(nodePtyMock.spawned[1].killed, false, 'destroying sender A must not affect sender B sessions')
  })
})
