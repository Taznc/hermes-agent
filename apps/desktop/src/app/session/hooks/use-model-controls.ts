import { type QueryClient } from '@tanstack/react-query'
import { useCallback, useRef } from 'react'

import type { ModelSelection } from '@/app/shell/model-menu-panel'
import { getGlobalModelInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { isBusySessionModelSwitch } from '@/lib/gateway-rpc'
import { surfaceModelSwitchConfirm } from '@/lib/guarded-model-switch'
import { manualPickRemoved, modelOptionsQueryKey } from '@/lib/model-options'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $currentModel,
  $currentProvider,
  $currentReasoningEffort,
  getComposerSelectionGeneration,
  getCurrentModelSource,
  markComposerSelectionManual,
  setCurrentModel,
  setCurrentModelSource,
  setCurrentProvider,
  setCurrentReasoningEffort
} from '@/store/session'
import { $sessionStates, sessionTileDelegate } from '@/store/session-states'
import type { ModelOptionsResponse } from '@/types/hermes'

interface ModelControlsOptions {
  cacheOwnerConnectionId?: string
  cacheProfile?: string
  queryClient: QueryClient
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
}

interface ModelSwitchResponse {
  confirm_message?: string
  confirm_required?: boolean
  deferred?: boolean
}

/** How a switch that failed AFTER the model write left the backend. */
export type ModelSelectionRecovery = 'not_needed' | 'restore_failed' | 'restored'

/**
 * How a `confirmation_pending` outcome eventually resolved. A confirmation is
 * a SUSPENDED decision, not a terminal answer: a surface that told the user to
 * confirm has to be able to replace that guidance once they do, so the pending
 * outcome carries the promise of its own settlement.
 *
 * `superseded` is neither success nor failure — the user made a newer choice,
 * the staleness guard dismissed the prompt, and nothing was sent.
 */
export type ConfirmationSettlement =
  { kind: 'applied' } | { kind: 'failed'; recovery: ModelSelectionRecovery } | { kind: 'superseded' }

export type ModelSelectionOutcome =
  | { kind: 'applied' }
  | { kind: 'confirmation_pending'; settled: Promise<ConfirmationSettlement> }
  | { kind: 'failed'; recovery: ModelSelectionRecovery }

export interface RecommendedModelSelection extends ModelSelection {
  sessionId: null | string
}

export function useModelControls({
  cacheOwnerConnectionId,
  cacheProfile,
  queryClient,
  requestGateway
}: ModelControlsOptions) {
  const { t } = useI18n()
  const copy = t.desktop
  const profileRefreshEpochRef = useRef(0)

  // All callbacks here read reactive session state from the store (.get())
  // rather than capturing it as a prop. The actions bag in wiring.tsx mutates
  // in place to keep a stable identity, so memoized surfaces capture these
  // callbacks once and never re-evaluate — a captured prop would be stale
  // forever. The store read is always current.
  const updateModelOptionsCache = useCallback(
    (
      sessionId: null | string,
      provider: string,
      model: string,
      includeGlobal: boolean,
      profile = cacheProfile || $activeGatewayProfile.get(),
      ownerConnectionId = cacheOwnerConnectionId
    ) => {
      const patch = (prev: ModelOptionsResponse | undefined) => {
        // Selection state can update before the catalog query has resolved.
        // Keep that optimistic cache structurally complete; the composer
        // interprets a response without `providers` as an empty catalog.
        const providers = prev?.providers?.length
          ? prev.providers
          : provider && model
            ? [{ models: [model], name: provider, slug: provider }]
            : []

        return { ...prev, provider, model, providers }
      }

      queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, sessionId, ownerConnectionId), patch)

      if (includeGlobal) {
        queryClient.setQueryData<ModelOptionsResponse>(modelOptionsQueryKey(profile, null, ownerConnectionId), patch)
      }
    },
    [cacheOwnerConnectionId, cacheProfile, queryClient]
  )

  // Settings → Model writes the profile default, which the backend applies to
  // new sessions only. Keep a live session's renderer state and session-scoped
  // model-options cache authoritative instead of briefly painting the saved
  // default as if the active agent had switched. Marking the composer as
  // default-derived still lets the next fresh draft reseed from profile config.
  const applySavedMainModel = useCallback(
    (provider: string, model: string) => {
      const liveSessionId = $activeSessionId.get()

      setCurrentModelSource('default')

      if (!liveSessionId) {
        setCurrentProvider(provider)
        setCurrentModel(model)
      }

      // A null session id is the profile-global model-options key. Never patch
      // the live session key here: only config.set --session may change it.
      updateModelOptionsCache(null, provider, model, false)
    },
    [updateModelOptionsCache]
  )

  // Seed the composer's model state from the profile default. `force` reseeds
  // for a profile swap (the new profile has its own default); otherwise this
  // only fills an EMPTY selection so a user's pick (plain UI state in
  // $currentModel) survives the lifecycle refreshes that fire on boot / fresh
  // draft / session events. A live session owns the footer, so skip entirely.
  const refreshCurrentModel = useCallback(
    async (force = false) => {
      // A forced profile swap opens a new intent epoch; an older in-flight
      // response for a previous profile must stand down when it resolves.
      if (force) {
        profileRefreshEpochRef.current += 1
      }

      const profileRefreshEpoch = profileRefreshEpochRef.current
      const profile = $activeGatewayProfile.get()

      try {
        if ($activeSessionId.get()) {
          return
        }

        // A manual pick stays sticky UNLESS it was removed from the catalog (its
        // model no longer exists on the provider), in which case keeping it would
        // 404 every new chat — fall through to reseed from the profile default.
        // Reads the model-options cache the composer already populated; an
        // unknown/not-yet-loaded catalog conservatively preserves the pick.
        const keepManualPick = () => {
          if (force || !$currentModel.get() || getCurrentModelSource() !== 'manual') {
            return false
          }

          const options = queryClient.getQueryData<ModelOptionsResponse>(
            modelOptionsQueryKey(cacheProfile || $activeGatewayProfile.get(), null, cacheOwnerConnectionId)
          )

          return !manualPickRemoved(options?.providers, $currentProvider.get(), $currentModel.get())
        }

        if (keepManualPick()) {
          return
        }

        // Snapshot the selection generation before awaiting so a picker click
        // that lands while getGlobalModelInfo is in flight wins over this older
        // default — value comparisons alone miss re-selecting the same row.
        const selectionGeneration = getComposerSelectionGeneration()
        const result = await getGlobalModelInfo(profile)

        if (
          profileRefreshEpochRef.current !== profileRefreshEpoch ||
          $activeSessionId.get() ||
          getComposerSelectionGeneration() !== selectionGeneration ||
          keepManualPick()
        ) {
          return
        }

        if (typeof result.model === 'string') {
          setCurrentModel(result.model)
        }

        if (typeof result.provider === 'string') {
          setCurrentProvider(result.provider)
        }

        if (typeof result.model === 'string' || typeof result.provider === 'string') {
          setCurrentModelSource('default')
        }
      } catch {
        // The delayed session.info event still updates this once the agent is ready.
      }
    },
    [cacheOwnerConnectionId, cacheProfile, queryClient]
  )

  // The internal path returns a discriminated outcome so the recommendation
  // surface can distinguish a pending confirmation from a real failure. The
  // legacy `selectModel` adapter below preserves the model picker's boolean API.
  // The composer model is plain UI state: with no live session it's just
  // stored (and shipped on the next session.create); with one it's scoped to
  // that session via config.set. It NEVER writes the profile default — that
  // lives in Settings → Model — so picking a model here can't silently mutate
  // global config.
  //
  // `selection.sessionId` targets a specific surface (tile). When omitted, the
  // primary `$activeSessionId` is used (overlay / legacy callers). A tile
  // switch must not touch the primary globals — and must not be blocked by a
  // busy primary turn.
  const selectModelWithOutcome = useCallback(
    async (selection: ModelSelection, forceSessionScope = false): Promise<ModelSelectionOutcome> => {
      const primaryRuntimeId = $activeSessionId.get()
      const liveSessionId = 'sessionId' in selection ? (selection.sessionId ?? null) : primaryRuntimeId
      const touchesPrimary = !liveSessionId || liveSessionId === primaryRuntimeId

      const prevModel = touchesPrimary ? $currentModel.get() : ($sessionStates.get()[liveSessionId!]?.model ?? '')

      const prevProvider = touchesPrimary
        ? $currentProvider.get()
        : ($sessionStates.get()[liveSessionId!]?.provider ?? '')

      const prevSource = getCurrentModelSource()
      const liveGatewayProfile = cacheProfile || $activeGatewayProfile.get()

      // >>> FORK ANCHOR: composer-model-recommendation <<<
      // Effort snapshot for the SAME surface the model switch targets, so an
      // optional effort rides the switch's own scoping and rollback.
      const prevEffort = touchesPrimary
        ? $currentReasoningEffort.get()
        : ($sessionStates.get()[liveSessionId!]?.reasoningEffort ?? '')

      const paintEffort = (effort: string) => {
        if (touchesPrimary) {
          setCurrentReasoningEffort(effort)
        } else if (liveSessionId) {
          sessionTileDelegate()?.updateSession(liveSessionId, state => ({ ...state, reasoningEffort: effort }))
        }
      }

      const paintSelection = () => {
        if (touchesPrimary) {
          setCurrentModel(selection.model)
          setCurrentProvider(selection.provider)
          markComposerSelectionManual()
        } else if (liveSessionId) {
          // Optimistic tile paint — session.info will confirm; rollback on error.
          sessionTileDelegate()?.updateSession(liveSessionId, state => ({
            ...state,
            model: selection.model,
            provider: selection.provider
          }))
        }

        // >>> FORK ANCHOR: composer-model-recommendation <<<
        if (selection.effort !== undefined) {
          paintEffort(selection.effort)
        }
      }

      const cacheSelection = (provider: string, model: string) => {
        updateModelOptionsCache(liveSessionId, provider, model, touchesPrimary && !liveSessionId, liveGatewayProfile)
      }

      const rollbackSelection = () => {
        if (touchesPrimary) {
          setCurrentModel(prevModel)
          setCurrentProvider(prevProvider)
          setCurrentModelSource(prevSource)
        } else if (liveSessionId) {
          sessionTileDelegate()?.updateSession(liveSessionId, state => ({
            ...state,
            model: prevModel,
            provider: prevProvider
          }))
        }

        // >>> FORK ANCHOR: composer-model-recommendation <<<
        if (selection.effort !== undefined) {
          paintEffort(prevEffort)
        }

        cacheSelection(prevProvider, prevModel)
      }

      paintSelection()
      cacheSelection(selection.provider, selection.model)

      // No live session yet: the pick is pure UI state. session.create reads
      // $currentModel/$currentProvider and applies it as that session's override.
      if (!liveSessionId) {
        return { kind: 'applied' }
      }

      // The PRIMARY profile's main agent lets the gateway decide persistence
      // (resolve_persist_behavior): session-only by default, persisted when
      // model.persist_switch_by_default is true or when no default has ever
      // been configured (the first-ever pick, so resolve_provider never falls
      // through to a leftover OPENAI_API_KEY env var — #86414). A plain pick
      // no longer silently rewrites config.yaml (#90235); Settings → Model
      // remains the explicit "set as default" door.
      //
      // Three things stay --session, deliberately:
      //  - a SECONDARY chat tile: picking a model there must not rewrite the
      //    profile default (the cross-session-contamination guard).
      //  - MoA (mixture-of-agents) presets: a transient orchestration choice
      //    that must never become the persisted global gateway default.
      //  - composer recommendations: advisory choices that are absolute
      //    session overrides and must never alter profile defaults.
      const isSessionOnlyPreset = (selection.provider || '').toLowerCase() === 'moa'
      const scope = forceSessionScope || !touchesPrimary || isSessionOnlyPreset ? ' --session' : ''

      const requestSwitch = (confirmExpensiveModel = false) =>
        requestGateway<ModelSwitchResponse>('config.set', {
          session_id: liveSessionId,
          key: 'model',
          value: `${selection.model} --provider ${selection.provider}${scope}`,
          ...(confirmExpensiveModel ? { confirm_expensive_model: true } : {})
        })

      // The gateway has no atomic model+effort method. If the model write wins
      // and the effort write fails, restore the previous model with one bounded
      // session-scoped compensation. The prior model already ran in this exact
      // session, so confirming it cannot introduce a new expensive-model risk.
      const restorePreviousModel = async (): Promise<boolean> => {
        if (!prevModel || !prevProvider) {
          return false
        }

        try {
          const restored = await requestGateway<ModelSwitchResponse>('config.set', {
            confirm_expensive_model: true,
            key: 'model',
            session_id: liveSessionId,
            value: `${prevModel} --provider ${prevProvider} --session`
          })

          return !restored?.confirm_required
        } catch {
          return false
        }
      }

      const paintAcceptedModelWithPreviousEffort = () => {
        paintSelection()
        paintEffort(prevEffort)
        cacheSelection(selection.provider, selection.model)
        void queryClient.invalidateQueries({
          queryKey: modelOptionsQueryKey(liveGatewayProfile, liveSessionId, cacheOwnerConnectionId)
        })
      }

      // >>> FORK ANCHOR: composer-model-recommendation <<<
      // The effort half of a combined selection. Always session-scoped (that
      // is what `config.set reasoning` with a session_id means) and always
      // AFTER the model switch is accepted, so a rejected/confirm-pending
      // switch never leaves the session on a new effort for an old model.
      const requestEffort = async () => {
        if (selection.effort === undefined) {
          return
        }

        await requestGateway('config.set', { key: 'reasoning', session_id: liveSessionId, value: selection.effort })
      }

      const finishSwitch = (result: ModelSwitchResponse | undefined) => {
        // A pick made DURING a turn is queued by the gateway and applied at the
        // next turn start (`deferred`). Re-fetching now would answer with the
        // model still running and repaint the old name over the user's choice —
        // the switch publishes session.info when it lands, and that is what
        // re-syncs every surface.
        if (!result?.deferred) {
          void queryClient.invalidateQueries({
            queryKey: modelOptionsQueryKey(liveGatewayProfile, liveSessionId, cacheOwnerConnectionId)
          })
        }
      }

      try {
        const result = await requestSwitch()

        if (result?.confirm_required) {
          rollbackSelection()
          let confirmedRestoreFailed = false
          // What the post-confirm compensation did, when it ran at all. Null
          // means the confirmed switch never reached the effort write, so a
          // failure is a plain refusal with nothing to compensate.
          let confirmedRecovery: ModelSelectionRecovery | null = null

          // The confirmation's own settlement. The executor runs synchronously,
          // so `settleConfirmation` is assigned before any callback below can
          // fire; `resolve` is idempotent, so the first terminal event wins and
          // a later one is a no-op rather than a contradiction.
          let settleConfirmation!: (settlement: ConfirmationSettlement) => void

          const settled = new Promise<ConfirmationSettlement>(resolve => {
            settleConfirmation = resolve
          })

          // ONE shared applier for guarded switches (#95293): the same
          // confirm flow the Bots editor routes through — never fork this
          // logic per surface.
          surfaceModelSwitchConfirm({
            confirmLabel: t.common.confirm,
            confirmMessage: result.confirm_message,
            failureMessage: copy.modelSwitchFailed,
            finish: confirmedResult => {
              finishSwitch(confirmedResult)
              settleConfirmation({ kind: 'applied' })
            },
            // Staleness guard — the warning can linger while the user picks
            // a different model or switches sessions. Clicking Confirm must
            // not clobber the newer choice: bail if the live state no longer
            // matches the snapshot this notification was created for.
            isStale: () => {
              const stale = touchesPrimary
                ? $activeSessionId.get() !== liveSessionId ||
                  $currentModel.get() !== prevModel ||
                  $currentProvider.get() !== prevProvider
                : !liveSessionId ||
                  $sessionStates.get()[liveSessionId]?.model !== prevModel ||
                  $sessionStates.get()[liveSessionId]?.provider !== prevProvider

              if (stale) {
                // Nothing was sent and nothing failed: the user simply moved
                // on. A caller showing confirm guidance must drop it without
                // reporting an error that never happened.
                settleConfirmation({ kind: 'superseded' })
              }

              return stale
            },
            repaint: () => {
              paintSelection()
              cacheSelection(selection.provider, selection.model)
            },
            requestConfirmed: async () => {
              const confirmed = await requestSwitch(true)

              // >>> FORK ANCHOR: composer-model-recommendation <<<
              // Only once the guarded model is genuinely accepted. A second
              // confirm_required is treated as a failure by the shared applier.
              if (!confirmed?.confirm_required) {
                try {
                  await requestEffort()
                } catch (err) {
                  confirmedRestoreFailed = !(await restorePreviousModel())
                  confirmedRecovery = confirmedRestoreFailed ? 'restore_failed' : 'restored'

                  if (confirmedRestoreFailed) {
                    paintAcceptedModelWithPreviousEffort()
                  }

                  throw err
                }
              }

              return confirmed
            },
            rollback: () => {
              if (!confirmedRestoreFailed) {
                rollbackSelection()
              }

              settleConfirmation({ kind: 'failed', recovery: confirmedRecovery ?? 'not_needed' })
            }
          })

          return { kind: 'confirmation_pending', settled }
        }

        // >>> FORK ANCHOR: composer-model-recommendation <<<
        // The model write already succeeded. If effort fails, restore the
        // backend model too — repainting local atoms alone creates split-brain.
        try {
          await requestEffort()
        } catch (err) {
          const restored = await restorePreviousModel()

          if (restored) {
            rollbackSelection()
          } else {
            // Never repaint the old model and claim rollback when the gateway
            // refused it. Keep the accepted model visible at the previous
            // effort and trigger authoritative reconciliation.
            paintAcceptedModelWithPreviousEffort()
          }

          notifyError(err, copy.modelSwitchFailed)

          return { kind: 'failed', recovery: restored ? 'restored' : 'restore_failed' }
        }

        finishSwitch(result)

        return { kind: 'applied' }
      } catch (err) {
        // An OLDER gateway refuses a mid-turn switch outright (4009) instead of
        // deferring it. Don't punish the user for a backend they haven't
        // updated: keep the pick painted as the composer's selection, which is
        // what the NEXT turn runs anyway. Current gateways never take this
        // path — they answer `deferred`.
        if (isBusySessionModelSwitch(err)) {
          return { kind: 'applied' }
        }

        rollbackSelection()
        notifyError(err, copy.modelSwitchFailed)

        return { kind: 'failed', recovery: 'not_needed' }
      }
    },
    [
      cacheOwnerConnectionId,
      cacheProfile,
      copy.modelSwitchFailed,
      queryClient,
      requestGateway,
      t.common.confirm,
      updateModelOptionsCache
    ]
  )

  const selectModel = useCallback(
    async (selection: ModelSelection): Promise<boolean> =>
      (await selectModelWithOutcome(selection)).kind === 'applied',
    [selectModelWithOutcome]
  )

  const selectRecommendedModel = useCallback(
    (selection: RecommendedModelSelection): Promise<ModelSelectionOutcome> => selectModelWithOutcome(selection, true),
    [selectModelWithOutcome]
  )

  return { applySavedMainModel, refreshCurrentModel, selectModel, selectRecommendedModel }
}
