import { atom, computed } from 'nanostores'

import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { $clarifyRequests } from '@/store/clarify'
import { $queuedPromptsBySession } from '@/store/composer-queue'
import { $gateway, requestGatewayForProfile } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $projectTree } from '@/store/projects'
import {
  $approvalRequests,
  $secretRequests,
  $sudoRequests,
  $vaultCodeRequests,
  $vaultSaveLoginRequests,
  $vaultUnlockRequests
} from '@/store/prompts'
import {
  $connection,
  $cronSessions,
  $messagingSessions,
  $sessions,
  lineageAliases,
  sessionMatchesStoredId
} from '@/store/session'
import { $sessionDotStateById } from '@/store/session-dot-state'
import { $sessionStates } from '@/store/session-states'

import { $uiRequests } from './ui-bridge/store'

export type ArchiveBlocker = 'running' | 'pending-input' | 'background-work' | 'queued' | 'backend-work'

export const $archiveSessionRows = computed(
  [$sessions, $messagingSessions, $cronSessions, $projectTree],
  (sessions, messaging, cron, projects) => [
    ...sessions,
    ...messaging,
    ...cron,
    ...projects.flatMap(p => [
      ...(p.previewSessions ?? []),
      ...(p.repos ?? []).flatMap(r => r.groups.flatMap(g => g.sessions))
    ])
  ]
)

const $promptOwners = computed(
  [
    $clarifyRequests,
    $approvalRequests,
    $sudoRequests,
    $secretRequests,
    $vaultUnlockRequests,
    $vaultSaveLoginRequests,
    $vaultCodeRequests
  ],
  (...maps) => [...new Set(maps.flatMap(requests => Object.keys(requests).filter(Boolean)))]
)

/** Session work, not tab lifecycle: an idle OPEN runtime is archivable.
 * Dot state already owns the runtime/stored/lineage bridge for active turns,
 * pending input, live descendants (including background children) and processes.
 * Queue/UI-request stores add work which can exist after the parent goes idle. */
const $localArchiveBlockers = computed(
  [$sessionDotStateById, $queuedPromptsBySession, $sessionStates, $archiveSessionRows, $uiRequests, $promptOwners],
  (dots, queues, states, sessions, requests, promptOwners) => {
    const blockers: Record<string, ArchiveBlocker> = {}

    const claim = (id: string, reason: ArchiveBlocker) => {
      for (const alias of lineageAliases(states[id]?.storedSessionId ?? id, sessions)) {
        blockers[alias] = reason
      }
    }

    for (const [id, status] of Object.entries(dots)) {
      if (status === 'working' || status === 'stalled') {
        claim(id, 'running')
      }

      if (status === 'needs-input') {
        claim(id, 'pending-input')
      }

      if (status === 'background') {
        claim(id, 'background-work')
      }
    }

    for (const [id, entries] of Object.entries(queues)) {
      if (entries.length) {
        claim(id, 'queued')
      }
    }

    for (const request of Object.values(requests)) {
      claim(request.sessionId, 'pending-input')
    }

    for (const id of promptOwners) {
      claim(id, 'pending-input')
    }

    return blockers
  }
)

// One observer loop for mounted plugin rows: at most 16 reads per sweep,
// four in flight, no per-row timers. Unknown/transient errors hide the icon;
// a confirmed missing method preserves older gateways' renderer-only hints.
const $remoteArchiveBlockers = atom<Record<string, ArchiveBlocker>>({})
const observed = new Map<string, number>()
const statusCache = new Map<string, boolean>()
const unsupportedProfiles = new Set<string>()
let observationEpoch = 0
let cursor = 0
let observationTimer: ReturnType<typeof setTimeout> | undefined
let observationFlight = false
let unlistenObservation: (() => void)[] = []

function archiveTarget(id: string) {
  const row = $archiveSessionRows.get().find(s => sessionMatchesStoredId(s, id))
  const profile = normalizeProfileKey(row?.profile ?? $activeGatewayProfile.get())
  const live = row?.id ?? id

  return { profile, id: live, key: JSON.stringify([profile, live]) }
}

function publishArchiveObservation() {
  const blockers: Record<string, ArchiveBlocker> = {}
  const keys = new Set<string>()

  for (const id of observed.keys()) {
    const target = archiveTarget(id)
    keys.add(target.key)

    if (statusCache.get(target.key) !== true) {
      for (const alias of lineageAliases(id, $archiveSessionRows.get())) {
        blockers[alias] = 'backend-work'
      }
    }
  }

  for (const key of statusCache.keys()) {
    if (!keys.has(key)) {
      statusCache.delete(key)
    }
  }

  $remoteArchiveBlockers.set(blockers)
}

function scheduleArchiveObservation(delay: number) {
  if (observationTimer !== undefined || !observed.size) {
    return
  }

  observationTimer = setTimeout(() => {
    observationTimer = undefined
    void pollArchiveObservation()
  }, delay)
}

async function pollArchiveObservation() {
  if (observationFlight || !observed.size) {
    return
  }

  observationFlight = true
  const epoch = observationEpoch

  const targets = [
    ...new Map(
      [...observed.keys()].map(id => {
        const target = archiveTarget(id)

        return [target.key, target] as const
      })
    ).values()
  ]

  const queue = Array.from({ length: Math.min(16, targets.length) }, (_, i) => targets[(cursor + i) % targets.length])
  cursor = (cursor + queue.length) % targets.length

  const worker = async () => {
    for (let target = queue.shift(); target; target = queue.shift()) {
      if (epoch !== observationEpoch || !observed.size) {
        break
      }

      let archivable = unsupportedProfiles.has(target.profile)

      try {
        if (archivable) {
          statusCache.set(target.key, true)
          publishArchiveObservation()

          continue
        }

        const status = await requestGatewayForProfile<{ archivable: boolean }>(
          target.profile,
          'fork.session.archive_status',
          { session_id: target.id, profile: target.profile },
          5000
        )

        archivable = status?.archivable === true
      } catch (error) {
        archivable = isMissingRpcMethod(error)

        if (archivable && epoch === observationEpoch) {
          unsupportedProfiles.add(target.profile)
        }
      }

      if (epoch === observationEpoch && [...observed.keys()].some(id => archiveTarget(id).key === target.key)) {
        statusCache.set(target.key, archivable)
        publishArchiveObservation()
      }
    }
  }

  try {
    await Promise.all(Array.from({ length: Math.min(4, queue.length) }, worker))
  } finally {
    observationFlight = false
    scheduleArchiveObservation(epoch === observationEpoch ? 5000 : 0)
  }
}

export function observeArchiveSession(id: string): () => void {
  observed.set(id, (observed.get(id) ?? 0) + 1)

  if (observed.size === 1 && !unlistenObservation.length) {
    const reset = () => {
      observationEpoch++
      statusCache.clear()
      unsupportedProfiles.clear()
      publishArchiveObservation()
      scheduleArchiveObservation(0)
    }

    unlistenObservation = [
      $activeGatewayProfile.listen(reset),
      $connection.listen(reset),
      $gateway.listen(reset),
      $archiveSessionRows.listen(() => {
        publishArchiveObservation()
        scheduleArchiveObservation(0)
      })
    ]
  }

  publishArchiveObservation()
  scheduleArchiveObservation(0)

  return () => {
    const count = (observed.get(id) ?? 1) - 1

    if (count) {
      observed.set(id, count)
    } else {
      observed.delete(id)
    }

    publishArchiveObservation()

    if (!observed.size) {
      observationEpoch++
      clearTimeout(observationTimer)
      observationTimer = undefined
      unlistenObservation.forEach(stop => stop())
      unlistenObservation = []
      statusCache.clear()
      unsupportedProfiles.clear()
    }
  }
}

export const $archiveBlockers = computed([$localArchiveBlockers, $remoteArchiveBlockers], (local, remote) => ({
  ...remote,
  ...local
}))
export const canArchiveSession = (id: string): boolean => !$archiveBlockers.get()[id]

/** Per-attempt refusals let batches distinguish a skipped remote worker from rollback. */
const refusals = new Map<string, ArchiveBlocker>()

export function takeForkArchiveRefusal(id: string): ArchiveBlocker | undefined {
  const reason = refusals.get(id)
  refusals.delete(id)

  return reason
}

/** Canonical action gate: run before row eviction, pin changes or draft/tab close.
 * Old gateways may lack the additive RPC, but transport/auth failures fail closed.
 * Admission is advisory: the backend archive setter must still recheck atomically. */
export async function guardForkSessionArchive(id: string, profile: string | undefined): Promise<boolean> {
  refusals.delete(id)
  profile ??= normalizeProfileKey($activeGatewayProfile.get())

  const refuse = (reason: ArchiveBlocker) => {
    refusals.set(id, reason)
    notify({ kind: 'warning', message: 'Cannot archive a session with active work. Wait for its work to finish.' })

    return false
  }

  const local = $localArchiveBlockers.get()[id]

  if (local) {
    return refuse(local)
  }

  try {
    const status = await requestGatewayForProfile<{ archivable: boolean; blockers: string[]; session_key: string }>(
      profile,
      'fork.session.archive_status',
      { session_id: id, profile }
    )

    if (typeof status?.archivable !== 'boolean') {
      throw new Error('Invalid archive status response')
    }

    if (!status.archivable) {
      return refuse('backend-work')
    }
  } catch (error) {
    if (!isMissingRpcMethod(error)) {
      notifyError(error, 'Could not check session work; archive was not started.')

      return false
    }
  }

  const after = $localArchiveBlockers.get()[id]

  return after ? refuse(after) : true
}
