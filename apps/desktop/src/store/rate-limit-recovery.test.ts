/**
 * Phase 2.12 (Desktop half) — store-level unit tests for the rate-limit
 * recovery helpers: fallback-candidate resolution, resume-job scheduling
 * dedupe/cancel bookkeeping, and the default-recovery preference cache.
 * The React-side countdown/action-row behavior is covered separately in
 * assistant-message's rate-limit test (fake timers).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hermes')>()

  return {
    ...actual,
    createCronJob: vi.fn(),
    deleteCronJob: vi.fn(),
    getCronJobs: vi.fn(),
    getHermesConfigRecord: vi.fn(),
    saveHermesConfig: vi.fn()
  }
})

import { createCronJob, deleteCronJob, getCronJobs, getHermesConfigRecord, saveHermesConfig } from '@/hermes'

import {
  $rateLimitDefaultRecovery,
  $scheduledResumeJobs,
  cancelScheduledResume,
  loadRateLimitDefaultRecovery,
  nextFallbackCandidate,
  scheduleResumeAtReset,
  setRateLimitDefaultRecovery
} from './rate-limit-recovery'
import { $rateLimitedSessionIds } from './session-dot-state'

const mockCreateCronJob = vi.mocked(createCronJob)
const mockDeleteCronJob = vi.mocked(deleteCronJob)
const mockGetCronJobs = vi.mocked(getCronJobs)
const mockGetHermesConfigRecord = vi.mocked(getHermesConfigRecord)
const mockSaveHermesConfig = vi.mocked(saveHermesConfig)

beforeEach(() => {
  vi.clearAllMocks()
  $scheduledResumeJobs.set({})
  $rateLimitedSessionIds.set({})
  $rateLimitDefaultRecovery.set('ask')
  mockGetCronJobs.mockResolvedValue([])
})

describe('nextFallbackCandidate', () => {
  it('returns the first complete entry that is not the failed provider/model', () => {
    const chain = [
      { provider: 'openai', model: 'gpt-5' },
      { provider: 'anthropic', model: 'claude-opus' }
    ]

    expect(nextFallbackCandidate(chain, 'openai', 'gpt-5')).toEqual({ provider: 'anthropic', model: 'claude-opus' })
  })

  it('skips the failed route even when it recurs later in the chain', () => {
    const chain = [
      { provider: 'openai', model: 'gpt-5' },
      { provider: 'openai', model: 'gpt-5' },
      { provider: 'anthropic', model: 'claude-opus' }
    ]

    expect(nextFallbackCandidate(chain, 'openai', 'gpt-5')).toEqual({ provider: 'anthropic', model: 'claude-opus' })
  })

  it('skips incomplete entries missing a provider or model', () => {
    const chain = [{ provider: 'openai', model: '' }, { provider: '', model: 'x' }, { provider: 'p', model: 'm' }]

    expect(nextFallbackCandidate(chain, 'other', 'other')).toEqual({ provider: 'p', model: 'm' })
  })

  it('returns null when every entry is the failed route or nothing remains', () => {
    expect(nextFallbackCandidate([{ provider: 'openai', model: 'gpt-5' }], 'openai', 'gpt-5')).toBeNull()
    expect(nextFallbackCandidate([], 'openai', 'gpt-5')).toBeNull()
  })

  it('matches case-insensitively', () => {
    const chain = [{ provider: 'OpenAI', model: 'GPT-5' }]

    expect(nextFallbackCandidate(chain, 'openai', 'gpt-5')).toBeNull()
  })
})

describe('scheduleResumeAtReset', () => {
  it('creates a one-shot job with the resetAt ISO timestamp and marks the session rate-limited', async () => {
    mockCreateCronJob.mockResolvedValue({ id: 'job-1', enabled: true, name: 'hermes-rate-limit-resume:msg-1' })

    const resetAt = 1_700_000_000
    const result = await scheduleResumeAtReset({ messageId: 'msg-1', resetAt, sessionId: 'sess-1' })

    expect(result.deduped).toBe(false)
    expect(mockCreateCronJob).toHaveBeenCalledTimes(1)
    const payload = mockCreateCronJob.mock.calls[0][0]
    expect(payload.schedule).toBe(new Date(resetAt * 1000).toISOString())
    expect(payload.name).toBe('hermes-rate-limit-resume:msg-1')
    expect(payload.prompt).toContain('sess-1')
    // No secrets/transcript content — just a session id + instruction.
    expect(payload.prompt).not.toMatch(/api[-_]?key/i)

    expect($scheduledResumeJobs.get()['msg-1']).toEqual({ jobId: 'job-1', resetAt })
    expect($rateLimitedSessionIds.get()['sess-1']).toEqual({ resetAt })
  })

  it('dedupes against an in-memory scheduled job for the same message', async () => {
    mockCreateCronJob.mockResolvedValue({ id: 'job-1', enabled: true })

    await scheduleResumeAtReset({ messageId: 'msg-1', resetAt: 1_700_000_000, sessionId: 'sess-1' })
    mockGetCronJobs.mockResolvedValue([{ id: 'job-1', enabled: true }])

    const second = await scheduleResumeAtReset({ messageId: 'msg-1', resetAt: 1_700_000_000, sessionId: 'sess-1' })

    expect(second.deduped).toBe(true)
    expect(mockCreateCronJob).toHaveBeenCalledTimes(1)
  })

  it('dedupes against a job found on the backend from a prior app run (cold-start guard)', async () => {
    mockGetCronJobs.mockResolvedValue([
      { id: 'existing-job', enabled: true, name: 'hermes-rate-limit-resume:msg-2' }
    ])

    const result = await scheduleResumeAtReset({ messageId: 'msg-2', resetAt: 1_700_000_000, sessionId: 'sess-2' })

    expect(result.deduped).toBe(true)
    expect(result.job.id).toBe('existing-job')
    expect(mockCreateCronJob).not.toHaveBeenCalled()
  })
})

describe('cancelScheduledResume', () => {
  it('deletes the backend job and clears local bookkeeping', async () => {
    mockCreateCronJob.mockResolvedValue({ id: 'job-1', enabled: true })
    mockDeleteCronJob.mockResolvedValue({ ok: true })
    await scheduleResumeAtReset({ messageId: 'msg-1', resetAt: 1_700_000_000, sessionId: 'sess-1' })

    await cancelScheduledResume('msg-1', 'sess-1')

    expect(mockDeleteCronJob).toHaveBeenCalledWith('job-1')
    expect($scheduledResumeJobs.get()['msg-1']).toBeUndefined()
    expect($rateLimitedSessionIds.get()['sess-1']).toBeUndefined()
  })

  it('is a no-op when nothing was scheduled for the message', async () => {
    await cancelScheduledResume('unknown', 'sess-1')

    expect(mockDeleteCronJob).not.toHaveBeenCalled()
  })
})

describe('rate_limit_default_recovery preference', () => {
  it('loads and normalizes the config value, defaulting unknown values to ask', async () => {
    mockGetHermesConfigRecord.mockResolvedValue({ sessions: { rate_limit_default_recovery: 'resume_at_reset' } })

    expect(await loadRateLimitDefaultRecovery()).toBe('resume_at_reset')
    expect($rateLimitDefaultRecovery.get()).toBe('resume_at_reset')

    mockGetHermesConfigRecord.mockResolvedValue({ sessions: { rate_limit_default_recovery: 'garbled' } })
    expect(await loadRateLimitDefaultRecovery()).toBe('ask')
  })

  it('persists the preference through the shared config record round-trip', async () => {
    mockGetHermesConfigRecord.mockResolvedValue({ sessions: {}, other: 'untouched' })
    mockSaveHermesConfig.mockResolvedValue({ ok: true })

    await setRateLimitDefaultRecovery('resume_at_reset')

    expect(mockSaveHermesConfig).toHaveBeenCalledWith(
      { sessions: { rate_limit_default_recovery: 'resume_at_reset' }, other: 'untouched' },
      undefined
    )
    expect($rateLimitDefaultRecovery.get()).toBe('resume_at_reset')
  })
})
