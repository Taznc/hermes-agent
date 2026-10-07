/**
 * "Keep full context" — the Desktop affordance for skipping a running
 * automatic compaction (spec t_07c75c42 §2; decision note t_8f8f2f7b §3).
 *
 * One `statusBar.right` chip, registered from fork-owned code (0 upstream
 * lines), describing the FOCUSED session (a tile's runtime when a tile has
 * focus, else the primary's — `$focusedRuntimeId`, the same derivation the
 * statusbar uses):
 *
 *  - State A, the session is compacting: **Keep full context** calls
 *    `fork.session.compaction_defer`, which cancels the compaction's commit
 *    fence (not the turn) and raises this session's watermark, so the
 *    compaction retries later at a higher token count.
 *  - State B, idle with an ACTIVE raised watermark: **Compacts at ~N**; a click
 *    calls `fork.session.compaction_watermark {action: "clear"}`.
 *  - Otherwise nothing renders, and no RPC fires while nothing is compacting
 *    except the one `get` per focused session / compaction edge.
 *
 * Strings are English literals by spec (localisation would go through
 * `src/i18n/fork/`). A backend without the fork RPCs renders nothing.
 */

import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { registry } from '@/contrib/registry'
import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { cn } from '@/lib/utils'
import { $compactingSessions } from '@/store/compaction'
import { $gateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { knownSessionOwner, ownerLookupSessionRows } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { requestForSessionProfile, type SessionOwnerScope } from '@/store/session-request-router'
import { $focusedRuntimeId, sessionTileOwnerRoute } from '@/store/session-states'

export const COMPACTION_CHIP_ID = 'fork.compaction-watermark'
export const COMPACTION_CHIP_ORDER = 79

export const DEFER_METHOD = 'fork.session.compaction_defer'
export const WATERMARK_METHOD = 'fork.session.compaction_watermark'

export type DeferStatus = 'busy' | 'deferred' | 'not_running' | 'refused' | 'too_late'
export type DeferReason = 'at_ceiling' | 'manual' | 'overflow' | 'unsupported'

export interface DeferResult {
  ceiling_tokens: null | number
  context_length: null | number
  previous_threshold_tokens: null | number
  reason: DeferReason | null
  status: DeferStatus
  usable_tokens: null | number
  watermark_tokens: null | number
}

export interface WatermarkResult {
  active: boolean
  base_threshold_tokens: null | number
  ceiling_tokens: null | number
  context_length: null | number
  threshold_tokens: null | number
  watermark_tokens: null | number
}

/** `~587K` below 1M (nearest 1K), `~1.2M` at or above it. */
export function formatTokens(tokens: null | number | undefined): string {
  if (typeof tokens !== 'number' || !Number.isFinite(tokens)) {
    return '?'
  }

  if (tokens >= 1_000_000) {
    return `~${(Math.round(tokens / 100_000) / 10).toFixed(1)}M`
  }

  return `~${Math.round(tokens / 1_000)}K`
}

interface Toast {
  kind: 'info' | 'success' | 'warning'
  message: string
  title?: string
}

const REFUSALS: Record<DeferReason, (r: DeferResult) => string> = {
  at_ceiling: r =>
    `Can't skip: this session is close to the model's context limit (${formatTokens(r.ceiling_tokens)} of ${formatTokens(r.usable_tokens).replace(/^~/, '')} usable tokens), so it has to compact now.`,
  manual: () => 'This is a manual /compress — use Stop to cancel it.',
  overflow: () => "Can't skip: the provider rejected the request as too large, so it has to compact now.",
  unsupported: () => "Skipping compaction isn't available for this session."
}

/** The exact toast for a `compaction_defer` result (spec §2 table). */
export function deferToast(result: DeferResult): Toast {
  switch (result.status) {
    case 'deferred':
      return {
        kind: 'success',
        title: 'Compaction skipped',
        message: `Full context kept. This session now compacts at ${formatTokens(result.watermark_tokens)} tokens (was ${formatTokens(result.previous_threshold_tokens)}).`
      }

    case 'too_late':
      return { kind: 'warning', message: 'Too late to skip — the summary is already being saved.' }

    case 'busy':
      return { kind: 'warning', message: "Couldn't skip right now — try again in a moment." }

    case 'not_running':
      return { kind: 'info', message: 'No compaction is running.' }

    case 'refused':
      return { kind: 'warning', message: REFUSALS[result.reason ?? 'unsupported'](result) }
  }
}

/** Reset toast; `threshold` is the post-clear (default) trigger. */
export function resetToast(threshold: null | number): Toast {
  return {
    kind: 'success',
    title: 'Watermark reset',
    message: `This session compacts at ${formatTokens(threshold)} tokens again. If it's already past that, it compacts on your next message.`
  }
}

function ambientRequest<R>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<R> {
  const gateway = $gateway.get()

  if (!gateway) {
    return Promise.reject(new Error('Hermes gateway unavailable'))
  }

  return gateway.request<R>(method, params, timeoutMs, signal)
}

/** Send a session RPC on the socket that owns the focused session: the tile's
 *  recorded owner route, else the loaded row's owner, else the ambient
 *  gateway (the same ladder the tile delegate uses, minus its async probe —
 *  a session the window is showing is always loaded). */
function requestForFocusedSession<T>(
  storedSessionId: null | string,
  method: string,
  params: Record<string, unknown>
): Promise<T> {
  const owner: SessionOwnerScope = storedSessionId
    ? (sessionTileOwnerRoute(storedSessionId) ?? knownSessionOwner(ownerLookupSessionRows(), storedSessionId))
    : null

  return requestForSessionProfile<T>(owner, ambientRequest, method, params)
}

const CHIP_CLASS =
  'inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] tabular-nums transition-colors text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground disabled:cursor-default disabled:opacity-45'

const DEFER_TIP =
  'Skip this summary and keep the full conversation. This session will compact later, at a higher token count.'

const resetTip = (base: null | number) =>
  `You raised this session's compaction point when you skipped a summary. Click to reset it to the default (${formatTokens(base)}).`

export function CompactionWatermarkChip() {
  const runtimeId = useStore($focusedRuntimeId)
  const storedId = useStore($focusedStoredSessionId)
  // Same membership test as `sessionCompacting(id)`, without minting a new
  // computed store per render. The map only changes on compaction edges.
  const compactingSessions = useStore($compactingSessions)
  const compacting = Boolean(runtimeId && Object.hasOwn(compactingSessions, runtimeId))
  const [watermark, setWatermark] = useState<{ id: string; value: WatermarkResult } | null>(null)
  const [pending, setPending] = useState(false)
  // The id the next answer must still describe: a stale reply for a session
  // the user has since left never paints under the new one.
  const currentId = useRef(runtimeId)
  currentId.current = runtimeId

  const refresh = useCallback(
    async (id: string, action: 'clear' | 'get' = 'get') => {
      try {
        const value = await requestForFocusedSession<WatermarkResult>(storedId, WATERMARK_METHOD, {
          action,
          session_id: id
        })

        if (currentId.current === id) {
          setWatermark({ id, value })
        }

        return value
      } catch (error) {
        if (currentId.current === id) {
          setWatermark(null)
        }

        if (action === 'clear' && !isMissingRpcMethod(error)) {
          notifyError(error, "Couldn't reset the compaction point.")
        }

        return null
      }
    },
    [storedId]
  )

  // On focus change, and on every compacting -> idle edge (the `compacted` /
  // `ready` status that ends the attempt), re-read the watermark. The read also
  // performs the backend's one-time durable load for a resumed session.
  useEffect(() => {
    if (runtimeId && !compacting) {
      void refresh(runtimeId)
    }
  }, [compacting, refresh, runtimeId])

  if (!runtimeId) {
    return null
  }

  if (compacting) {
    const onDefer = async () => {
      setPending(true)

      try {
        const result = await requestForFocusedSession<DeferResult>(storedId, DEFER_METHOD, { session_id: runtimeId })
        const toast = deferToast(result)

        notify({ ...toast, id: `fork-compaction-defer:${runtimeId}` })
      } catch (error) {
        if (isMissingRpcMethod(error)) {
          notify({
            ...deferToast({ status: 'refused', reason: 'unsupported' } as DeferResult),
            id: `fork-compaction-defer:${runtimeId}`
          })
        } else {
          notifyError(error, "Couldn't skip the compaction.")
        }
      } finally {
        setPending(false)
      }
    }

    return (
      <Tip label={DEFER_TIP}>
        <button
          aria-description={DEFER_TIP}
          className={cn(CHIP_CLASS, 'text-foreground')}
          data-testid="fork-compaction-defer"
          disabled={pending}
          onClick={() => void onDefer()}
          type="button"
        >
          <Codicon name="debug-pause" size="0.7rem" />
          <span>Keep full context</span>
        </button>
      </Tip>
    )
  }

  const status = watermark?.id === runtimeId ? watermark.value : null

  if (!status?.active) {
    return null
  }

  const onReset = async () => {
    setPending(true)

    try {
      const value = await refresh(runtimeId, 'clear')

      if (value) {
        notify({
          ...resetToast(value.threshold_tokens ?? status.base_threshold_tokens),
          id: `fork-compaction-reset:${runtimeId}`
        })
      }
    } finally {
      setPending(false)
    }
  }

  const tip = resetTip(status.base_threshold_tokens)

  return (
    <Tip label={tip}>
      <button
        aria-description={tip}
        className={CHIP_CLASS}
        data-testid="fork-compaction-watermark"
        disabled={pending}
        onClick={() => void onReset()}
        type="button"
      >
        <Codicon name="fold-up" size="0.7rem" />
        <span>Compacts at {formatTokens(status.watermark_tokens)}</span>
      </button>
    </Tip>
  )
}

let disposeChip: (() => void) | null = null

/** Register the chip once (idempotent; returns the disposer). Called at
 *  module load from `fork/sdk-host.ts`, so it is always mounted. */
export function installCompactionWatermarkChip(): () => void {
  disposeChip ??= registry.register({
    area: 'statusBar.right',
    id: COMPACTION_CHIP_ID,
    order: COMPACTION_CHIP_ORDER,
    render: () => <CompactionWatermarkChip />,
    source: 'fork'
  })

  return () => {
    disposeChip?.()
    disposeChip = null
  }
}
