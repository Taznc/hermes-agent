import { afterEach, describe, expect, it, vi } from 'vitest'

import { host } from '@/sdk'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import { $projectScope, $projectTree, $startWorkSessionRequest, ALL_PROJECTS } from '@/store/projects'
import {
  $currentBranch,
  $currentCwd,
  $newChatWorkspaceTarget,
  type NewChatWorkspaceTarget,
  setCurrentBranch,
  setCurrentCwd,
  setNewChatWorkspaceTarget
} from '@/store/session'

import { deferred } from '../../test/deferred'

import { consumeStartWorkSessionRequest, startWorkspaceSession } from './workspace-session-target'

describe('startWorkspaceSession', () => {
  afterEach(() => {
    setCurrentBranch('')
    setCurrentCwd('')
    setNewChatWorkspaceTarget(undefined)
    $projectScope.set(ALL_PROJECTS)
    $projectTree.set([])
    $startWorkSessionRequest.set(null)
    $activeGatewayProfile.set('default')
    $newChatProfile.set(null)
    vi.restoreAllMocks()
  })

  it('keeps a newer sidebar target when an older project lookup resolves', async () => {
    const first = deferred<{ branch?: string; cwd?: string }>()
    const second = deferred<{ branch?: string; cwd?: string }>()

    const requestGateway = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)

    const activeSessionIdRef = { current: null }

    const startFreshSessionDraft = vi.fn((options?: { workspaceTarget: NewChatWorkspaceTarget }) => {
      setNewChatWorkspaceTarget(options?.workspaceTarget)
      setCurrentCwd(options?.workspaceTarget || '')
    })

    const followActiveSessionCwd = vi.fn()

    startWorkspaceSession({
      activeSessionIdRef,
      followActiveSessionCwd,
      path: '/workspace-a',
      requestGateway,
      startFreshSessionDraft
    })
    startWorkspaceSession({
      activeSessionIdRef,
      followActiveSessionCwd,
      path: '/workspace-b',
      requestGateway,
      startFreshSessionDraft
    })

    first.resolve({ branch: 'stale', cwd: '/normalized-a' })
    await first.promise
    await Promise.resolve()

    expect($newChatWorkspaceTarget.get()).toBe('/workspace-b')
    expect($currentCwd.get()).toBe('/workspace-b')
    expect($currentBranch.get()).not.toBe('stale')

    second.resolve({ branch: 'main', cwd: '/normalized-b' })
    await second.promise
    await Promise.resolve()

    expect($newChatWorkspaceTarget.get()).toBe('/normalized-b')
    expect($currentCwd.get()).toBe('/normalized-b')
    expect($currentBranch.get()).toBe('main')
  })

  it('keeps a Home new-session request detached even when another project scope is active', () => {
    $projectScope.set('p_voice')
    $projectTree.set([
      {
        id: 'p_voice',
        label: 'Voice Assistant',
        path: '/Users/oschmidt/Checkouts/voice-assistant',
        repos: [],
        sessionCount: 0
      }
    ])

    const requestGateway = vi.fn()
    const activeSessionIdRef = { current: null }

    const startFreshSessionDraft = vi.fn((options?: { workspaceTarget: NewChatWorkspaceTarget }) => {
      setNewChatWorkspaceTarget(options?.workspaceTarget)
      setCurrentCwd(options?.workspaceTarget || '')
    })

    startWorkspaceSession({
      activeSessionIdRef,
      path: null,
      requestGateway,
      startFreshSessionDraft
    })

    expect(startFreshSessionDraft).toHaveBeenCalledWith({ workspaceTarget: null })
    expect(requestGateway).not.toHaveBeenCalled()
    expect($newChatWorkspaceTarget.get()).toBeNull()
    expect($currentCwd.get()).toBe('')
  })

  // #79005 flaw 3: the project "+" must pin the profile the tree is shown
  // under; otherwise session.create reads $activeGatewayProfile after a swap.
  it('pins the new chat to the profile the project tree is displayed under', () => {
    $activeGatewayProfile.set('work')
    $newChatProfile.set(null)

    startWorkspaceSession({
      activeSessionIdRef: { current: null },
      path: '/workspace-work',
      requestGateway: vi.fn(() => new Promise<never>(() => {})),
      startFreshSessionDraft: vi.fn()
    })

    $activeGatewayProfile.set('personal')

    expect($newChatProfile.get()).toBe('work')
  })

  it('allocates a distinct contextual surface when the previous draft is still unsent', async () => {
    const roots = new Map<string, null | string>()
    const composers = new Map<string, string>()
    let activeSurface = ''
    let nextSurface = 0

    const consumeCurrentRequest = async () => {
      const request = $startWorkSessionRequest.get()

      expect(request).not.toBeNull()

      if (!request) {
        return
      }

      await consumeStartWorkSessionRequest({
        insertDraft: draft => {
          composers.set(activeSurface, `${composers.get(activeSurface) ?? ''}${draft}`)
        },
        isCurrent: () => $startWorkSessionRequest.get()?.token === request.token,
        mainChatIsOccupied: false,
        openFreshSurface: async path => {
          activeSurface = `context-${++nextSurface}`
          roots.set(activeSurface, path)
        },
        request,
        startMainSurface: () => {
          throw new Error('contextual requests must not reuse the main draft surface')
        }
      })
    }

    host.newChatWithContext({ cwd: '/workspace-a', draft: 'First card prompt' })
    await consumeCurrentRequest()
    const firstSurface = activeSurface

    host.newChatWithContext({ cwd: undefined, draft: 'Second card prompt' })
    await consumeCurrentRequest()
    const secondSurface = activeSurface

    expect(secondSurface).not.toBe(firstSurface)
    expect(roots.get(firstSurface)).toBe('/workspace-a')
    expect(roots.get(secondSurface)).toBeNull()
    expect(composers.get(firstSurface)).toBe('First card prompt')
    expect(composers.get(secondSurface)).toBe('Second card prompt')
  })
})
