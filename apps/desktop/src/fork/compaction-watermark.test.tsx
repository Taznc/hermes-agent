import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'
import { $compactingSessions, setSessionCompacting } from '@/store/compaction'
import { $notifications } from '@/store/notifications'
import { requestForSessionProfile } from '@/store/session-request-router'
import { sessionTileOwnerRoute } from '@/store/session-states'

import {
  COMPACTION_CHIP_ID,
  CompactionWatermarkChip,
  DEFER_METHOD,
  type DeferResult,
  deferToast,
  formatTokens,
  installCompactionWatermarkChip,
  WATERMARK_METHOD,
  type WatermarkResult
} from './compaction-watermark'

const focus = vi.hoisted(() => ({ runtime: null as unknown, stored: null as unknown }))

// The chip reads the statusbar's focused-session derivation; these tests drive
// it directly (primary vs tile is upstream's `$focusedRuntimeId` contract).
vi.mock('@/store/session-states', async original => {
  const { atom: mkAtom } = await import('nanostores')
  focus.runtime = mkAtom<null | string>(null)

  return {
    ...(await original<Record<string, unknown>>()),
    $focusedRuntimeId: focus.runtime,
    sessionTileOwnerRoute: vi.fn(() => undefined)
  }
})

vi.mock('@/store/session-focus', async original => {
  const { atom: mkAtom } = await import('nanostores')
  focus.stored = mkAtom<null | string>(null)

  return { ...(await original<Record<string, unknown>>()), $focusedStoredSessionId: focus.stored }
})

vi.mock('@/store/session-request-router', async original => ({
  ...(await original<Record<string, unknown>>()),
  requestForSessionProfile: vi.fn()
}))

type StringAtom = ReturnType<typeof atom<null | string>>

const runtime = () => focus.runtime as StringAtom
const stored = () => focus.stored as StringAtom
const request = vi.mocked(requestForSessionProfile)

const IDLE: WatermarkResult = {
  active: false,
  base_threshold_tokens: 484_000,
  ceiling_tokens: 871_200,
  context_length: 1_000_000,
  threshold_tokens: 484_000,
  watermark_tokens: null
}

const RAISED: WatermarkResult = { ...IDLE, active: true, threshold_tokens: 586_800, watermark_tokens: 586_800 }

const deferred = (over: Partial<DeferResult> = {}): DeferResult => ({
  ceiling_tokens: 871_200,
  context_length: 1_000_000,
  previous_threshold_tokens: 484_000,
  reason: null,
  status: 'deferred',
  usable_tokens: 968_000,
  watermark_tokens: 586_800,
  ...over
})

/** Route RPCs by method; `watermark` answers `get`/`clear`. */
function backend({ defer, watermark }: { defer?: () => unknown; watermark?: (action: string) => unknown }) {
  request.mockImplementation(async (_owner, _ambient, method, params) => {
    if (method === DEFER_METHOD) {
      return defer?.()
    }

    if (method === WATERMARK_METHOD) {
      return watermark ? watermark(String(params?.action)) : IDLE
    }

    throw new Error(`unexpected ${method}`)
  })
}

/** Params of every call to `method`, in order. */
const paramsFor = (method: string) => request.mock.calls.filter(call => call[2] === method).map(call => call[3])

// The store prepends: index 0 is the newest toast.
const lastToast = () => $notifications.get()[0]

const DEFER_TIP =
  'Skip this summary and keep the full conversation. This session will compact later, at a higher token count.'

beforeEach(() => {
  $notifications.set([])
  $compactingSessions.set({})
  runtime().set('rt-1')
  stored().set('stored-1')
  request.mockReset()
  vi.mocked(sessionTileOwnerRoute).mockReturnValue(undefined)
})

afterEach(() => {
  cleanup()
})

describe('formatTokens', () => {
  it('rounds to ~K below 1M and ~x.xM at or above', () => {
    expect(formatTokens(586_800)).toBe('~587K')
    expect(formatTokens(484_000)).toBe('~484K')
    expect(formatTokens(1_200_000)).toBe('~1.2M')
    expect(formatTokens(1_000_000)).toBe('~1.0M')
    expect(formatTokens(null)).toBe('?')
  })
})

describe('deferToast', () => {
  it.each([
    [deferred(), 'Full context kept. This session now compacts at ~587K tokens (was ~484K).'],
    [deferred({ status: 'too_late' }), 'Too late to skip — the summary is already being saved.'],
    [deferred({ status: 'busy' }), "Couldn't skip right now — try again in a moment."],
    [deferred({ status: 'not_running' }), 'No compaction is running.'],
    [
      deferred({ status: 'refused', reason: 'at_ceiling' }),
      "Can't skip: this session is close to the model's context limit (~871K of 968K usable tokens), so it has to compact now."
    ],
    [deferred({ status: 'refused', reason: 'manual' }), 'This is a manual /compress — use Stop to cancel it.'],
    [
      deferred({ status: 'refused', reason: 'overflow' }),
      "Can't skip: the provider rejected the request as too large, so it has to compact now."
    ],
    [deferred({ status: 'refused', reason: 'unsupported' }), "Skipping compaction isn't available for this session."]
  ])('%o', (result, message) => {
    expect(deferToast(result).message).toBe(message)
  })

  it('titles the success toast', () => {
    expect(deferToast(deferred()).title).toBe('Compaction skipped')
  })
})

describe('CompactionWatermarkChip', () => {
  it('renders nothing when idle with no raised watermark, and never defers on its own', async () => {
    backend({})
    const { container } = render(<CompactionWatermarkChip />)

    await waitFor(() => expect(paramsFor(WATERMARK_METHOD)).toEqual([{ action: 'get', session_id: 'rt-1' }]))
    expect(container.innerHTML).toBe('')
    expect(paramsFor(DEFER_METHOD)).toEqual([])
  })

  it('renders nothing and calls nothing with no focused session', () => {
    runtime().set(null)
    backend({})
    const { container } = render(<CompactionWatermarkChip />)

    expect(container.innerHTML).toBe('')
    expect(request).not.toHaveBeenCalled()
  })

  it('offers Keep full context while the focused session compacts and defers with its runtime id', async () => {
    backend({ defer: () => deferred() })
    setSessionCompacting('rt-1', true)
    render(<CompactionWatermarkChip />)

    const button = screen.getByRole('button', { name: /Keep full context/ })
    expect(button.getAttribute('aria-description')).toBe(DEFER_TIP)

    fireEvent.click(button)

    await waitFor(() => expect(lastToast()?.title).toBe('Compaction skipped'))
    expect(paramsFor(DEFER_METHOD)).toEqual([{ session_id: 'rt-1' }])
    expect(lastToast()).toMatchObject({
      id: 'fork-compaction-defer:rt-1',
      kind: 'success',
      message: 'Full context kept. This session now compacts at ~587K tokens (was ~484K).'
    })
  })

  it('does not show for another session compacting in the background', () => {
    backend({})
    setSessionCompacting('rt-other', true)
    render(<CompactionWatermarkChip />)

    expect(screen.queryByRole('button', { name: /Keep full context/ })).toBeNull()
  })

  it('routes a focused tile to its owner route with the tile runtime id', async () => {
    const route = { connectionId: 'remote-a', profile: 'work' }
    vi.mocked(sessionTileOwnerRoute).mockReturnValue(route as never)
    runtime().set('rt-tile')
    stored().set('stored-tile')
    backend({ defer: () => deferred({ status: 'too_late' }) })
    setSessionCompacting('rt-tile', true)
    render(<CompactionWatermarkChip />)

    fireEvent.click(screen.getByRole('button', { name: /Keep full context/ }))

    await waitFor(() => expect(lastToast()?.message).toBe('Too late to skip — the summary is already being saved.'))
    expect(vi.mocked(sessionTileOwnerRoute)).toHaveBeenCalledWith('stored-tile')
    expect(request).toHaveBeenCalledWith(route, expect.any(Function), DEFER_METHOD, { session_id: 'rt-tile' })
  })

  it('a backend without the fork RPC answers unsupported, not an error', async () => {
    request.mockRejectedValue(Object.assign(new Error('Method not found'), { code: -32601 }))
    setSessionCompacting('rt-1', true)
    render(<CompactionWatermarkChip />)

    fireEvent.click(screen.getByRole('button', { name: /Keep full context/ }))

    await waitFor(() => expect(lastToast()?.message).toBe("Skipping compaction isn't available for this session."))
  })

  it('after the compaction ends, shows the raised watermark and Reset clears it', async () => {
    let raised = false
    backend({
      defer: () => {
        raised = true

        return deferred()
      },
      watermark: action => {
        if (action === 'clear') {
          raised = false
        }

        return raised ? RAISED : IDLE
      }
    })
    setSessionCompacting('rt-1', true)
    render(<CompactionWatermarkChip />)

    fireEvent.click(screen.getByRole('button', { name: /Keep full context/ }))
    await waitFor(() => expect(lastToast()?.title).toBe('Compaction skipped'))

    // The compacting -> idle edge (compacted/ready) re-reads the watermark.
    act(() => setSessionCompacting('rt-1', false))

    const chip = await screen.findByRole('button', { name: /Compacts at ~587K/ })
    expect(chip.getAttribute('aria-description')).toBe(
      "You raised this session's compaction point when you skipped a summary. Click to reset it to the default (~484K)."
    )

    fireEvent.click(chip)

    await waitFor(() => expect(lastToast()?.title).toBe('Watermark reset'))
    expect(lastToast()?.message).toBe(
      "This session compacts at ~484K tokens again. If it's already past that, it compacts on your next message."
    )
    expect(paramsFor(WATERMARK_METHOD)).toContainEqual({ action: 'clear', session_id: 'rt-1' })
    await waitFor(() => expect(screen.queryByRole('button', { name: /Compacts at/ })).toBeNull())
  })

  it('a resumed session with a durable watermark shows State B on focus', async () => {
    backend({ watermark: () => RAISED })
    render(<CompactionWatermarkChip />)

    expect(await screen.findByRole('button', { name: /Compacts at ~587K/ })).toBeTruthy()
  })

  it('never paints a stale answer for a session the user has left', async () => {
    let resolveFirst: (value: WatermarkResult) => void = () => undefined
    request.mockImplementation((_owner, _ambient, _method, params) =>
      params?.session_id === 'rt-1'
        ? new Promise(resolve => {
            resolveFirst = resolve as typeof resolveFirst
          })
        : Promise.resolve(IDLE)
    )
    render(<CompactionWatermarkChip />)

    act(() => runtime().set('rt-2'))
    await act(async () => resolveFirst(RAISED))

    expect(screen.queryByRole('button', { name: /Compacts at/ })).toBeNull()
  })
})

describe('installCompactionWatermarkChip', () => {
  it('is installed by loading the fork SDK host (always mounted)', async () => {
    await import('./sdk-host')

    expect(registry.getArea('statusBar.right').filter(c => c.id === COMPACTION_CHIP_ID)).toHaveLength(1)
  })

  it('registers one statusBar.right render item at order 79, idempotently', () => {
    const dispose = installCompactionWatermarkChip()
    installCompactionWatermarkChip()

    const mine = registry.getArea('statusBar.right').filter(c => c.id === COMPACTION_CHIP_ID)
    expect(mine).toHaveLength(1)
    expect(mine[0]).toMatchObject({ order: 79 })
    expect(typeof mine[0].render).toBe('function')

    dispose()
    expect(registry.getArea('statusBar.right').some(c => c.id === COMPACTION_CHIP_ID)).toBe(false)
  })
})
