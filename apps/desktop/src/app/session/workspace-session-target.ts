import type { MutableRefObject } from 'react'

import { pinNewChatProfile } from '@/store/profile'
import {
  followActiveSessionCwd,
  projectProfile,
  resolveNewSessionCwd,
  type StartWorkSessionRequest
} from '@/store/projects'
import {
  $newChatWorkspaceTargetGeneration,
  type NewChatWorkspaceTarget,
  setCurrentBranch,
  setCurrentCwd,
  setNewChatWorkspaceTarget
} from '@/store/session'

interface WorkspaceSessionOptions {
  activeSessionIdRef: MutableRefObject<string | null>
  followActiveSessionCwd?: (cwd: string) => void | Promise<void>
  onExplicitWorkspace?: (cwd: string) => void
  path: null | string
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  startFreshSessionDraft: (options?: { workspaceTarget: NewChatWorkspaceTarget }) => void
}

interface ConsumeStartWorkSessionRequestOptions {
  insertDraft: (draft: string, options: { target: 'active' | 'main' }) => void
  isCurrent: () => boolean
  mainChatIsOccupied: boolean
  openFreshSurface: (path: null | string) => Promise<void>
  request: StartWorkSessionRequest
  startMainSurface: (path: null | string) => void
}

/** Consume the store request at the same boundary the renderer wiring uses.
 * Contextual requests carry `freshSurface`, so an unsent prior draft can never
 * receive the next request's text. */
export async function consumeStartWorkSessionRequest({
  insertDraft,
  isCurrent,
  mainChatIsOccupied,
  openFreshSurface,
  request,
  startMainSurface
}: ConsumeStartWorkSessionRequestOptions): Promise<void> {
  const openedInTab = Boolean(request.freshSurface || (request.openTab && mainChatIsOccupied))

  if (openedInTab) {
    await openFreshSurface(request.path)
  } else {
    startMainSurface(request.path)
  }

  if (request.draft && isCurrent()) {
    insertDraft(request.draft, { target: openedInTab ? 'active' : 'main' })
  }
}

export function startWorkspaceSession({
  activeSessionIdRef,
  followActiveSessionCwd: followCwd = followActiveSessionCwd,
  onExplicitWorkspace,
  path,
  requestGateway,
  startFreshSessionDraft
}: WorkspaceSessionOptions): void {
  // The project tree is rendered under one profile; the "+" belongs to it.
  // Pin that intent now — otherwise desktopSessionCreateParams falls back to
  // $activeGatewayProfile, which a still-settling profile swap can move
  // between this click and Send (#79005). All-profiles view has no owner.
  const profile = projectProfile()

  if (profile) {
    pinNewChatProfile(profile)
  }

  // Home's "+" passes path=null on purpose ("no folder"). That must stay
  // detached — do NOT fall through to resolveNewSessionCwd(), which can still
  // return a default/remembered project folder and re-attach the last repo
  // (digitwo: New session in Home still shows `main`).
  if (path === null) {
    startFreshSessionDraft({ workspaceTarget: null })

    return
  }

  // A worktree lane carries its own path. Empty string (legacy/path-less trunk)
  // can fall back to the active project's root, but null was handled above.
  const explicitTarget = path.trim()
  const target = explicitTarget || resolveNewSessionCwd()

  startFreshSessionDraft(target ? { workspaceTarget: target } : undefined)

  if (!target) {
    return
  }

  const workspaceGeneration = $newChatWorkspaceTargetGeneration.get()

  setCurrentCwd(target)
  void requestGateway<{ branch?: string; cwd?: string }>('config.get', { key: 'project', cwd: target })
    .then(info => {
      if ($newChatWorkspaceTargetGeneration.get() !== workspaceGeneration || activeSessionIdRef.current) {
        return
      }

      const resolved = info.cwd || target

      setCurrentCwd(resolved)
      setNewChatWorkspaceTarget(resolved)
      setCurrentBranch(info.branch || '')

      if (explicitTarget) {
        onExplicitWorkspace?.(resolved)
        void followCwd(resolved)
      }
    })
    .catch(() => {
      if ($newChatWorkspaceTargetGeneration.get() === workspaceGeneration && !activeSessionIdRef.current) {
        setCurrentBranch('')
      }
    })
}
