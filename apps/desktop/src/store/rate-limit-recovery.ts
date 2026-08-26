/**
 * RATE-LIMIT TURN RECOVERY (Phase 2.12, desktop half) — the small bit of
 * state and side-effect plumbing the error card, sidebar dot, and settings
 * toggle all share for a `rate_limit` / `upstream_rate_limit` terminal
 * failure (see `@/lib/error-surface`'s `resetAt` / `fallbackAvailable`).
 *
 * "Resume at reset" schedules a ONE-SHOT cron job via the existing
 * ISO-timestamp `createCronJob` path (cron/jobs.py already turns an ISO
 * schedule into `repeat: 1`) rather than inventing a new timer. What that
 * job's action can actually DO today is limited: cron has no "resume this
 * exact session and retry its last turn" primitive yet (see the linked
 * backend follow-up card, t_35e7ea9f) — every job spawns its own fresh
 * agent session. Until that seam lands, the scheduled job's prompt is an
 * honest, session-scoped instruction ("resume session <id>, retry the last
 * turn") that a capable agent run can act on; the card and this module never
 * claim a guaranteed resume, only that a job was scheduled for the given
 * time. `attach_to_session` is left on so that, for sessions whose origin
 * IS resolvable (messaging platforms), the run's own output still lands
 * back in the right place — for a bare local Desktop session (no
 * platform/chat_id origin) that mirroring is a no-op, matching current
 * backend capability rather than pretending otherwise.
 */

import { atom } from 'nanostores'

import { createCronJob, deleteCronJob, getCronJobs, getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import type { CronJob, CronJobCreatePayload, HermesConfigRecord } from '@/types/hermes'

import { clearSessionRateLimited, markSessionRateLimited } from './session-dot-state'

export type RateLimitDefaultRecovery = 'ask' | 'resume_at_reset'

/** Cached resolved value of `sessions.rate_limit_default_recovery`. Loaded
 *  once per profile session (see loadRateLimitDefaultRecovery); "ask" is the
 *  safe fallback when config hasn't loaded yet or the key is absent/garbled
 *  — matches the backend resolver's own default (hermes_state.py). */
export const $rateLimitDefaultRecovery = atom<RateLimitDefaultRecovery>('ask')

function normalizeRecovery(value: unknown): RateLimitDefaultRecovery {
  return value === 'resume_at_reset' ? 'resume_at_reset' : 'ask'
}

export async function loadRateLimitDefaultRecovery(profile?: string): Promise<RateLimitDefaultRecovery> {
  try {
    const record = await getHermesConfigRecord(profile)
    const sessions = (record.sessions ?? {}) as Record<string, unknown>
    const next = normalizeRecovery(sessions.rate_limit_default_recovery)

    $rateLimitDefaultRecovery.set(next)

    return next
  } catch {
    return $rateLimitDefaultRecovery.get()
  }
}

export async function setRateLimitDefaultRecovery(
  value: RateLimitDefaultRecovery,
  profile?: string
): Promise<void> {
  const record = await getHermesConfigRecord(profile)
  const sessions = { ...((record.sessions ?? {}) as Record<string, unknown>), rate_limit_default_recovery: value }
  const updated: HermesConfigRecord = { ...record, sessions }

  await saveHermesConfig(updated, profile)
  $rateLimitDefaultRecovery.set(value)
}

// ── Scheduled resume jobs ───────────────────────────────────────────────
//
// Keyed by the FAILED MESSAGE id (not session id): a session can accumulate
// more than one failed turn across its life, and each failure gets its own
// job. Duplicate-guarded per message so re-rendering the error card (a
// prop change, a re-mount) or a double click can never create two jobs for
// the same pending failure.

export interface ScheduledResumeJob {
  jobId: string
  resetAt: number
}

let scheduled: Readonly<Record<string, ScheduledResumeJob>> = {}
export const $scheduledResumeJobs = atom<Readonly<Record<string, ScheduledResumeJob>>>({})

function setScheduled(next: Readonly<Record<string, ScheduledResumeJob>>) {
  scheduled = next
  $scheduledResumeJobs.set(next)
}

const RESUME_JOB_NAME_PREFIX = 'hermes-rate-limit-resume:'

function resumeJobName(messageId: string): string {
  return `${RESUME_JOB_NAME_PREFIX}${messageId}`
}

/** Look for an existing job for this message — covers a resume scheduled in
 *  a PRIOR app run (the in-memory `scheduled` map is empty on cold start)
 *  so a reload can't create a second job for the same pending failure. */
async function findExistingResumeJob(messageId: string, profile?: string): Promise<CronJob | null> {
  try {
    const jobs = await getCronJobs(profile)
    const name = resumeJobName(messageId)

    return jobs.find(job => job.name === name) ?? null
  } catch {
    return null
  }
}

export interface ScheduleResumeParams {
  fallbackAvailable?: boolean
  messageId: string
  model?: string
  profile?: string
  provider?: string
  resetAt: number
  sessionId: string
}

/** Schedule (or reuse) the one-shot resume job for a failed turn. Returns
 *  the job, and whether it was newly created vs. an existing one reused. */
export async function scheduleResumeAtReset(
  params: ScheduleResumeParams
): Promise<{ deduped: boolean; job: CronJob }> {
  const { messageId, profile, resetAt, sessionId } = params

  const inMemory = scheduled[messageId]

  if (inMemory) {
    const jobs = await getCronJobs(profile).catch(() => [] as CronJob[])
    const existing = jobs.find(job => job.id === inMemory.jobId)

    if (existing) {
      return { deduped: true, job: existing }
    }
  }

  const existing = await findExistingResumeJob(messageId, profile)

  if (existing) {
    setScheduled({ ...scheduled, [messageId]: { jobId: existing.id, resetAt } })
    markSessionRateLimited(sessionId, resetAt)

    return { deduped: true, job: existing }
  }

  const iso = new Date(resetAt * 1000).toISOString()
  const payload: CronJobCreatePayload = {
    schedule: iso,
    name: resumeJobName(messageId),
    deliver: 'local',
    prompt:
      `Resume Hermes session ${sessionId}: its last turn ended on a provider rate ` +
      'limit and the reset window has now passed. If a resume/continue capability ' +
      'is available for that exact session, use it to retry the last turn; ' +
      'otherwise note plainly that this session still needs a manual resume.'
  }

  const job = await createCronJob(payload)

  setScheduled({ ...scheduled, [messageId]: { jobId: job.id, resetAt } })
  markSessionRateLimited(sessionId, resetAt)

  return { deduped: false, job }
}

export async function cancelScheduledResume(messageId: string, sessionId: string): Promise<void> {
  const entry = scheduled[messageId]

  const { [messageId]: _dropped, ...rest } = scheduled
  setScheduled(rest)
  clearSessionRateLimited(sessionId)

  if (entry) {
    await deleteCronJob(entry.jobId).catch(() => undefined)
  }
}

export function nextFallbackCandidate(
  chain: readonly { base_url?: string; model?: string; provider?: string }[],
  failedProvider?: string,
  failedModel?: string
): null | { model: string; provider: string } {
  const failedKey = `${(failedProvider || '').toLowerCase()}::${(failedModel || '').toLowerCase()}`

  for (const entry of chain) {
    const provider = (entry.provider || '').trim()
    const model = (entry.model || '').trim()

    if (!provider || !model) {
      continue
    }

    if (`${provider.toLowerCase()}::${model.toLowerCase()}` === failedKey) {
      continue
    }

    return { model, provider }
  }

  return null
}

/** Read the configured fallback chain (`fallback_providers` + legacy
 *  `fallback_model`) and return the first entry that isn't the failed
 *  provider/model — the same route `agent._fallback_chain` would try next,
 *  best-effort from the client side (the backend's own index/availability
 *  bookkeeping is per-turn server state Desktop cannot see). */
export async function resolveNextFallback(
  failedProvider?: string,
  failedModel?: string,
  profile?: string
): Promise<null | { model: string; provider: string }> {
  try {
    const record = await getHermesConfigRecord(profile)
    const raw = record.fallback_providers ?? record.fallback_model
    const entries = Array.isArray(raw) ? raw : raw ? [raw] : []
    const chain = entries.filter(
      (entry): entry is { base_url?: string; model?: string; provider?: string } =>
        !!entry && typeof entry === 'object'
    )

    return nextFallbackCandidate(chain, failedProvider, failedModel)
  } catch {
    return null
  }
}

/**
 * Apply a fallback provider/model to a session and retry — the "Switch
 * model & retry" recovery action. Uses the SAME `config.set` RPC the manual
 * `/model` picker uses (see `use-model-controls.ts::selectModel`), scoped
 * `--session` deliberately: an automated rate-limit recovery must never
 * silently rewrite the user's persisted global default the way a manual
 * Settings → Models pick does. Returns true on success.
 */
export async function applyFallbackAndRetry(
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>,
  sessionId: string,
  provider: string,
  model: string
): Promise<boolean> {
  try {
    await requestGateway('config.set', {
      session_id: sessionId,
      key: 'model',
      value: `${model} --provider ${provider} --session`
    })

    return true
  } catch {
    return false
  }
}
