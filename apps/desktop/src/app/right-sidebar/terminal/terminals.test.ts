import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'hermes.desktop.terminals.v1'
const bufferKey = (id: string) => `hermes.desktop.terminal-buffer.v1.${id}`

async function loadTerminalStore() {
  const $currentCwd = atom('/workspace')

  vi.doMock('@/store/session', () => ({
    $currentCwd
  }))

  return { ...(await import('./terminals')), $currentCwd }
}

describe('terminal store persistence', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('restores user tabs, active tab, and history on module load', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-two',
        terminals: [
          { auto: false, cwd: '/repo/one', id: 'term-one', title: 'zsh' },
          { auto: true, cwd: '/repo/two', id: 'term-two', title: 'Terminal' }
        ]
      })
    )
    window.localStorage.setItem(bufferKey('term-one'), JSON.stringify({ reviveBuffer: 'last output' }))

    const { $activeTerminalId, $terminals, getTerminalBuffer } = await loadTerminalStore()

    expect($activeTerminalId.get()).toBe('term-two')
    expect($terminals.get()).toEqual([
      { auto: false, cwd: '/repo/one', id: 'term-one', kind: 'user', title: 'zsh' },
      { auto: true, cwd: '/repo/two', id: 'term-two', kind: 'user', title: 'Terminal' }
    ])
    expect(getTerminalBuffer('term-one')).toEqual({ reviveBuffer: 'last output' })
  })

  it('migrates a legacy inline reviveBuffer/restoreCwd into the per-tab buffer store', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-one',
        terminals: [
          { auto: false, cwd: '/repo', id: 'term-one', restoreCwd: '/repo/api', reviveBuffer: 'hist', title: 'zsh' }
        ]
      })
    )

    const { $terminals, getTerminalBuffer } = await loadTerminalStore()

    expect($terminals.get()[0]).toEqual({ auto: false, cwd: '/repo', id: 'term-one', kind: 'user', title: 'zsh' })
    expect(getTerminalBuffer('term-one')).toEqual({ restoreCwd: '/repo/api', reviveBuffer: 'hist' })
  })

  it('persists user tabs as pure metadata, skipping agent mirrors and buffer bytes', async () => {
    const { createTerminal, ensureAgentTerminal, renameTerminal, selectTerminal, updateTerminalReviveBuffer } =
      await loadTerminalStore()

    const userId = createTerminal('/repo')
    renameTerminal(userId, 'server')
    updateTerminalReviveBuffer(userId, 'recent scrollback')
    ensureAgentTerminal('proc-1', 'background task')
    selectTerminal(userId)

    // No flush/tick: the metadata list persists synchronously (this is what
    // makes app-quit restore reliable). The buffer write is throttled
    // separately and is asserted via getTerminalBuffer, not this key.
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({
      activeTerminalId: userId,
      terminals: [{ auto: false, cwd: '/repo', id: userId, title: 'server' }]
    })
  })

  it('never attaches a revive buffer to an agent tab', async () => {
    const { ensureAgentTerminal, getTerminalBuffer, updateTerminalReviveBuffer } = await loadTerminalStore()

    const agentId = ensureAgentTerminal('proc-1', 'background task')!
    updateTerminalReviveBuffer(agentId, 'should be ignored')

    expect(getTerminalBuffer(agentId)).toBeUndefined()
  })

  it('tail-trims an oversized revive buffer to stay under the storage budget', async () => {
    const { createTerminal, getTerminalBuffer, updateTerminalReviveBuffer } = await loadTerminalStore()

    const userId = createTerminal('/repo')
    const huge = 'x'.repeat(60_000)
    updateTerminalReviveBuffer(userId, huge)

    const stored = getTerminalBuffer(userId)?.reviveBuffer ?? ''
    expect(stored.length).toBe(48_000)
    expect(stored).toBe(huge.slice(-48_000))
  })

  it('clears remembered tabs when all terminals close', async () => {
    const { closeAllTerminals, createTerminal } = await loadTerminalStore()

    createTerminal('/repo')
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull()

    closeAllTerminals()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('frees a closed tab buffer entry from the map and its own storage key', async () => {
    vi.useFakeTimers()

    const { closeTerminal, createTerminal, getTerminalBuffer, updateTerminalReviveBuffer } = await loadTerminalStore()

    const userId = createTerminal('/repo')
    updateTerminalReviveBuffer(userId, 'scrollback')
    await vi.runAllTimersAsync()
    expect(window.localStorage.getItem(bufferKey(userId))).not.toBeNull()

    closeTerminal(userId)

    expect(getTerminalBuffer(userId)).toBeUndefined()
    expect(window.localStorage.getItem(bufferKey(userId))).toBeNull()

    vi.useRealTimers()
  })

  it('restores and persists the last observed cwd so a reopened tab lands where the user cd-d', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-one',
        terminals: [{ auto: false, cwd: '/repo', id: 'term-one', title: 'zsh' }]
      })
    )
    window.localStorage.setItem(bufferKey('term-one'), JSON.stringify({ restoreCwd: '/repo/packages/api' }))

    vi.useFakeTimers()

    const { getTerminalBuffer, updateTerminalRestoreCwd } = await loadTerminalStore()

    expect(getTerminalBuffer('term-one')?.restoreCwd).toBe('/repo/packages/api')

    updateTerminalRestoreCwd('term-one', '/repo/packages/web')
    expect(getTerminalBuffer('term-one')?.restoreCwd).toBe('/repo/packages/web')

    await vi.runAllTimersAsync()
    expect(JSON.parse(window.localStorage.getItem(bufferKey('term-one')) ?? '{}').restoreCwd).toBe(
      '/repo/packages/web'
    )

    vi.useRealTimers()
  })

  it('never attaches a restore cwd to an agent tab and ignores empty values', async () => {
    const { createTerminal, ensureAgentTerminal, getTerminalBuffer, updateTerminalRestoreCwd } =
      await loadTerminalStore()

    const userId = createTerminal('/repo')
    const agentId = ensureAgentTerminal('proc-1', 'background task')!

    updateTerminalRestoreCwd(agentId, '/somewhere')
    updateTerminalRestoreCwd(userId, '   ')

    expect(getTerminalBuffer(agentId)).toBeUndefined()
    expect(getTerminalBuffer(userId)?.restoreCwd).toBeUndefined()
  })
})

describe('session cwd → terminal tab linking', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('re-selects the tab already pointed at the new session cwd (trailing slash tolerated)', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal } = await loadTerminalStore()

    const repoTab = createTerminal('/repo')
    const otherTab = createTerminal('/elsewhere')
    expect($activeTerminalId.get()).toBe(otherTab)

    $currentCwd.set('/repo/')
    expect($activeTerminalId.get()).toBe(repoTab)
  })

  it('matches the live shell cwd (restoreCwd) over the launch dir', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal, updateTerminalRestoreCwd } = await loadTerminalStore()

    const movedTab = createTerminal('/repo')
    updateTerminalRestoreCwd(movedTab, '/repo/packages/api')
    const otherTab = createTerminal('/elsewhere')
    expect($activeTerminalId.get()).toBe(otherTab)

    $currentCwd.set('/repo/packages/api')
    expect($activeTerminalId.get()).toBe(movedTab)

    // The launch dir no longer describes where that shell lives.
    $currentCwd.set('/repo')
    expect($activeTerminalId.get()).toBe(movedTab)
  })

  it('leaves the active tab alone when no tab lives in the session cwd or the cwd is empty', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal } = await loadTerminalStore()

    createTerminal('/repo')
    const activeTab = createTerminal('/elsewhere')

    $currentCwd.set('/unrelated')
    expect($activeTerminalId.get()).toBe(activeTab)

    $currentCwd.set('')
    expect($activeTerminalId.get()).toBe(activeTab)
  })

  it('stays put when the active tab already lives in the target cwd, and never matches agent tabs', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal, ensureAgentTerminal, selectTerminal } =
      await loadTerminalStore()

    const first = createTerminal('/repo')
    const second = createTerminal('/repo')
    ensureAgentTerminal('proc-1', 'background task')
    selectTerminal(second)

    // Both tabs match; the one already active keeps focus (no first-match steal).
    $currentCwd.set('/repo')
    expect($activeTerminalId.get()).toBe(second)

    selectTerminal(first)
    $currentCwd.set('/repo')
    expect($activeTerminalId.get()).toBe(first)
  })
})
