// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getCuratorStatus = vi.fn()
const getMemoryStatus = vi.fn()
const getActionStatus = vi.fn()

vi.mock('@/hermes', () => ({
  getActionStatus: (name: string, lines?: number) => getActionStatus(name, lines),
  getCuratorStatus: () => getCuratorStatus(),
  getMemoryStatus: () => getMemoryStatus(),
  resetMemory: vi.fn(),
  runBackup: vi.fn(),
  runCurator: vi.fn(),
  runDebugShare: vi.fn(),
  runDoctor: vi.fn(),
  runSecurityAudit: vi.fn(),
  setCuratorPaused: vi.fn()
}))

vi.mock('@/store/activity', () => ({
  upsertDesktopActionTask: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

async function renderMaintenance() {
  const { MaintenancePanel } = await import('./maintenance')
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(<MaintenancePanel />)
  })

  return result!
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MaintenancePanel — swallowed-error infinite loaders', () => {
  it('renders an error row + working Retry instead of an eternal loader when curator status rejects', async () => {
    getCuratorStatus.mockRejectedValueOnce(new Error('boom'))
    getMemoryStatus.mockResolvedValue({ active: null, builtin_files: { memory: 0, user: 0 } })

    await renderMaintenance()

    // No PageLoader branch reachable on a settled-rejected query: the status
    // element must be the error row, never the perpetual "Loading" status.
    await waitFor(() => expect(screen.getByText('Could not load curator status')).toBeTruthy())

    const retry = screen.getByRole('button', { name: 'Retry' })
    expect(retry).toBeTruthy()

    getCuratorStatus.mockResolvedValueOnce({ enabled: true, paused: false, last_run_at: null })

    await act(async () => {
      fireEvent.click(retry)
    })

    await waitFor(() => expect(screen.queryByText('Could not load curator status')).toBeNull())
    expect(await screen.findByText('Active')).toBeTruthy()
  })

  it('renders an error row + working Retry when memory status rejects', async () => {
    getCuratorStatus.mockResolvedValue({ enabled: false, paused: false, last_run_at: null })
    getMemoryStatus.mockRejectedValueOnce(new Error('network down'))

    await renderMaintenance()

    await waitFor(() => expect(screen.getByText('Could not load memory data')).toBeTruthy())

    getMemoryStatus.mockResolvedValueOnce({ active: null, builtin_files: { memory: 10, user: 0 } })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    })

    await waitFor(() => expect(screen.queryByText('Could not load memory data')).toBeNull())
  })

  it('stops tailing and shows a lost-track message after repeated action-status failures', async () => {
    getCuratorStatus.mockResolvedValue({ enabled: false, paused: false, last_run_at: null })
    getMemoryStatus.mockResolvedValue({ active: null, builtin_files: { memory: 0, user: 0 } })
    getActionStatus.mockRejectedValue(new Error('status endpoint down'))

    const { runDoctor } = await import('@/hermes')
    vi.mocked(runDoctor).mockResolvedValue({ name: 'doctor-1' } as never)

    await renderMaintenance()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Run doctor' }))
    })

    // Retry-with-backoff is bounded (3 attempts, full-jitter delays under a
    // few seconds) — real timers keep this deterministic without fighting
    // the component's own async poll() microtask chain.
    expect(
      await screen.findByText('Lost track of this task — view in activity rail', {}, { timeout: 10_000 })
    ).toBeTruthy()
  }, 15_000)
})
