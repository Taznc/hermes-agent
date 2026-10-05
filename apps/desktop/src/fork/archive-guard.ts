import { computed } from 'nanostores'

import { $clarifyRequests } from '@/store/clarify'
import { $queuedPromptsBySession } from '@/store/composer-queue'
import { notify } from '@/store/notifications'
import { $projectTree } from '@/store/projects'
import {
  $approvalRequests,
  $secretRequests,
  $sudoRequests,
  $vaultCodeRequests,
  $vaultSaveLoginRequests,
  $vaultUnlockRequests
} from '@/store/prompts'
import { $cronSessions, $messagingSessions, $sessions, lineageAliases } from '@/store/session'
import { $sessionDotStateById } from '@/store/session-dot-state'
import { $sessionStates } from '@/store/session-states'

import { $uiRequests } from './ui-bridge/store'

export type ArchiveBlocker = 'running' | 'pending-input' | 'background-work' | 'queued'

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
export const $archiveBlockers = computed(
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

export const canArchiveSession = (id: string): boolean => !$archiveBlockers.get()[id]

/** Canonical action gate: run before row eviction, pin changes or draft/tab close. */
export function guardForkSessionArchive(id: string): boolean {
  if (canArchiveSession(id)) {
    return true
  }

  notify({ kind: 'warning', message: 'Cannot archive a session with active work. Wait for its work to finish.' })

  return false
}
