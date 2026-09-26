import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PALETTE_AREA } from '@/app/command-palette/contrib'
import { registry } from '@/contrib/registry'

import * as demo from './credits-notice-demo'
import { useCreditsNoticeDemo } from './use-credits-notice-demo'

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

function demoEntries() {
  return registry.getArea(PALETTE_AREA).filter(entry => entry.id === 'dev.creditsNotice')
}

const hook = () => (window as typeof window & { __creditsDemo?: () => void }).__creditsDemo

// Release only the observed import so tests control the cleanup-before-resolution race.
async function release(pending: ReturnType<typeof deferred<typeof demo>>) {
  await act(async () => pending.resolve(demo))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('dev credit-notice demo lifecycle', () => {
  it('installs one trigger on mount, cleans up, and installs one fresh trigger on remount', async () => {
    const logs = vi.spyOn(console, 'info').mockImplementation(() => {})
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const first = renderHook(() => useCreditsNoticeDemo())
    await act(async () => {})
    expect(add.mock.calls.filter(([event]) => event === 'keydown')).toHaveLength(1)
    expect(demoEntries()).toHaveLength(1)
    expect(hook()).toBe(demo.stepCreditsNoticeDemo)
    expect(logs).toHaveBeenCalledTimes(1)

    first.unmount()
    expect(remove.mock.calls.filter(([event]) => event === 'keydown')).toHaveLength(1)
    expect(demoEntries()).toHaveLength(0)
    expect(hook()).toBeUndefined()

    const second = renderHook(() => useCreditsNoticeDemo())
    await act(async () => {})
    expect(add.mock.calls.filter(([event]) => event === 'keydown')).toHaveLength(2)
    expect(demoEntries()).toHaveLength(1)
    expect(hook()).toBe(demo.stepCreditsNoticeDemo)
    expect(logs).toHaveBeenCalledTimes(2)
    second.unmount()
    expect(demoEntries()).toHaveLength(0)
    expect(hook()).toBeUndefined()
  })

  it('ignores an import resolved after cleanup, leaving only the stable mount', async () => {
    const logs = vi.spyOn(console, 'info').mockImplementation(() => {})
    const add = vi.spyOn(window, 'addEventListener')
    const remove = vi.spyOn(window, 'removeEventListener')
    const stale = deferred<typeof demo>()
    const current = deferred<typeof demo>()
    const load = vi.fn().mockReturnValueOnce(stale.promise).mockReturnValueOnce(current.promise)
    const first = renderHook(() => useCreditsNoticeDemo(load))
    first.unmount() // Strict Mode's first effect cleanup, before the import resolves.
    const mounted = renderHook(() => useCreditsNoticeDemo(load))
    expect(load).toHaveBeenCalledTimes(2)
    expect(demoEntries()).toHaveLength(0)
    expect(hook()).toBeUndefined()

    await release(stale)
    expect(add.mock.calls.filter(([event]) => event === 'keydown')).toHaveLength(0)
    expect(demoEntries()).toHaveLength(0)
    expect(hook()).toBeUndefined()
    expect(logs).not.toHaveBeenCalled()

    await release(current)
    expect(add.mock.calls.filter(([event]) => event === 'keydown')).toHaveLength(1)
    expect(demoEntries()).toHaveLength(1)
    expect(hook()).toBe(demo.stepCreditsNoticeDemo)
    expect(logs).toHaveBeenCalledTimes(1)

    mounted.unmount()
    expect(remove.mock.calls.filter(([event]) => event === 'keydown')).toHaveLength(1)
    expect(demoEntries()).toHaveLength(0)
    expect(hook()).toBeUndefined()
  })
})
