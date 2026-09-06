import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getLocalModelsJobs } from '@/hermes'
import {
  $localRuntimeJobs,
  POLL_ACTIVE_MS,
  resetLocalRuntimeJobsForTests,
  watchLocalRuntimeJobs
} from '@/store/local-runtime-jobs'
import type { LocalRuntimeJob } from '@/types/hermes'

vi.mock('@/hermes', () => ({
  getLocalModelsJobs: vi.fn(),
  getLocalModelsStatus: vi.fn(async () => ({ enabled: true }))
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const mockedGetJobs = vi.mocked(getLocalModelsJobs)

function runningJob(id: string): LocalRuntimeJob {
  return {
    job_id: id,
    kind: 'model-download',
    target: 'some/model',
    model_id: 'some/model',
    status: 'running',
    phase: 'downloading',
    detail: '',
    total_bytes: 100,
    done_bytes: 1,
    error: null
  }
}

beforeEach(() => {
  resetLocalRuntimeJobsForTests()
  $localRuntimeJobs.set([])
  mockedGetJobs.mockReset()
})

describe('local-runtime-jobs poll cancellation', () => {
  // The poll loop is deliberately self-perpetuating so a download survives the
  // settings pane unmounting. The contract this pins is the other half of that
  // deal: once cancelled, a poll whose request was ALREADY in flight must not
  // touch shared state or schedule another timer. Without it, a poll awaiting
  // its response when a jsdom environment is torn down resumes inside the NEXT
  // test file and throws `ReferenceError: window is not defined` into an
  // unrelated test (the moving-failure bug this test exists to prevent).
  it('an in-flight poll cancelled mid-request never applies its result', async () => {
    let releaseResponse: (jobs: readonly LocalRuntimeJob[]) => void = () => {}

    const inFlight = new Promise<{ jobs: readonly LocalRuntimeJob[] }>(resolve => {
      releaseResponse = jobs => resolve({ jobs })
    })

    mockedGetJobs.mockReturnValue(inFlight as ReturnType<typeof getLocalModelsJobs>)
    watchLocalRuntimeJobs()

    // Cancel while the request is still outstanding — the exact window a file
    // teardown lands in.
    resetLocalRuntimeJobsForTests()
    releaseResponse([runningJob('job-1')])
    await inFlight
    await Promise.resolve()

    expect($localRuntimeJobs.get()).toEqual([])
  })

  it('a cancelled poll schedules no further timer', async () => {
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout')

    mockedGetJobs.mockResolvedValue({ jobs: [runningJob('job-2')] })
    watchLocalRuntimeJobs()
    resetLocalRuntimeJobsForTests()

    // Let the resolved request and every microtask after it drain. This drain
    // is itself a setTimeout, so match on the poll cadence rather than call
    // count.
    await new Promise(resolve => setTimeout(resolve, 0))

    // A running job would normally re-arm the loop at POLL_ACTIVE_MS;
    // cancellation must win.
    const pollTimers = setTimeoutSpy.mock.calls.filter(([, delay]) => delay === POLL_ACTIVE_MS)

    expect(pollTimers).toEqual([])
    setTimeoutSpy.mockRestore()
  })

  it('an uncancelled poll still follows a running job', async () => {
    mockedGetJobs.mockResolvedValue({ jobs: [runningJob('job-3')] })
    watchLocalRuntimeJobs()

    await new Promise(resolve => setTimeout(resolve, 0))

    expect($localRuntimeJobs.get().map(j => j.job_id)).toEqual(['job-3'])
    resetLocalRuntimeJobsForTests()
  })
})
