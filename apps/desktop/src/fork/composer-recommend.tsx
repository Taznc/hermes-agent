import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useId, useRef, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import type { ComposerAttachment } from '@/store/composer'
import { setCurrentReasoningEffort } from '@/store/session'
import { sessionTileDelegate } from '@/store/session-states'

import {
  DEFAULT_RECOMMENDATION_PRESET,
  fetchModelRecommendations,
  type ModelRecommendation,
  readRecommendationPreset,
  RECOMMENDATION_PRESETS,
  recommendationEligibility,
  type RecommendationPreset,
  type RecommendationResult,
  writeRecommendationPreset
} from './model-recommendation'

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

interface ModelSelectionRequest {
  model: string
  provider: string
  sessionId?: null | string
}

export interface ComposerRecommendProps {
  attachments: readonly ComposerAttachment[]
  disabled: boolean
  /** Reads the LIVE draft at click time. The draft lives in the composer's
   *  contentEditable + draftRef, so a captured value would be stale; a getter
   *  also makes it structurally impossible for this surface to write it. */
  getDraft: () => string
  /** The existing session-aware model-selection path. Never reimplemented. */
  onSelectModel: (selection: ModelSelectionRequest) => Promise<boolean> | void
  profile: string
  requestGateway: RequestGateway
  /** Runtime id of the surface that owns this composer (null for a draft). */
  sessionId: null | string
}

type ApplyState = { kind: 'failed'; row: string } | { kind: 'idle' } | { kind: 'applying'; row: string }

const rowKey = (row: ModelRecommendation): string => `${row.provider}::${row.model}`

/**
 * The seam both composer owners actually mount: identical props minus
 * `sessionId`, which is resolved from THIS surface's `SessionView` instead of
 * being threaded down.
 *
 * That is not a convenience. The render function is built by the view's owner
 * (shell or tile) and invoked inside the composer, so a `sessionId` captured
 * at build time would be the value at memo-creation — stale the moment a turn
 * starts, and for a side-by-side tile it would be the PRIMARY's runtime id,
 * which is exactly the cross-surface leak `ModelMenuPanel` reads its id from
 * the view to avoid. Reading it here binds Apply to the pane the user clicked
 * in, and keeps it live.
 */
export function ComposerRecommendForView(props: Omit<ComposerRecommendProps, 'sessionId'>) {
  const view = useSessionView()
  const sessionId = useStore(view.$runtimeId)

  return <ComposerRecommend {...props} sessionId={sessionId} />
}

/**
 * COMPOSER RECOMMEND — the manual, advisory model-recommendation flow.
 *
 * Three invariants make this safe to sit next to the send button:
 *
 *  - It NEVER submits. The trigger is `type="button"` (a bare button inside
 *    the composer form defaults to submit, which would send the very draft
 *    the user asked to evaluate), it reads the draft through a getter, and it
 *    has no path that writes the draft or moves focus into/out of the editor.
 *  - It NEVER decides silently. Every non-`ok` backend answer paints its own
 *    explicit state (setup / unavailable / failed-with-retry). Nothing is
 *    defaulted, cached-and-shown-as-live, or invented.
 *  - Apply routes through the caller's existing session-aware selection path,
 *    so scoping (this session, never the profile default), optimistic paint,
 *    authoritative reconciliation and rollback are upstream's, not a second
 *    implementation here.
 *
 * Ranking, eligibility policy and provider coverage belong to the backend
 * contract (`docs/model-recommendation-gateway.md`); this file renders what it
 * is given, in the order it is given.
 */
export function ComposerRecommend({
  attachments,
  disabled,
  getDraft,
  onSelectModel,
  profile,
  requestGateway,
  sessionId
}: ComposerRecommendProps) {
  const copy = useI18n().t.composer.recommend
  const panelId = useId()
  const [preset, setPreset] = useState<RecommendationPreset>(DEFAULT_RECOMMENDATION_PRESET)
  const [presetPersisted, setPresetPersisted] = useState(true)
  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [result, setResult] = useState<RecommendationResult | null>(null)
  const [apply, setApply] = useState<ApplyState>({ kind: 'idle' })
  const panelRef = useRef<HTMLDivElement | null>(null)
  // Guards a late response from a superseded click (the user can re-request
  // with a different preset while one is in flight).
  const requestEpochRef = useRef(0)

  // The persisted preset must be visible BEFORE any request, so it is read on
  // mount rather than lazily on first open. An older backend answers "unknown
  // config key"; that is unsaved-preference, not an error, and the selector
  // stays usable for this session.
  useEffect(() => {
    let cancelled = false

    void readRecommendationPreset({ profile, request: requestGateway }).then(({ persisted, value }) => {
      if (cancelled) {
        return
      }

      setPreset(value)
      setPresetPersisted(persisted)
    })

    return () => {
      cancelled = true
    }
  }, [profile, requestGateway])

  // A backend without the method can never answer; keep the control visibly
  // unavailable instead of offering a click that always fails.
  const unsupported = result?.status === 'unsupported'
  const eligibility = recommendationEligibility({ attachments, draft: getDraft() })
  const canRequest = !disabled && !unsupported && eligibility.eligible

  const runRequest = useCallback(async () => {
    const epoch = ++requestEpochRef.current

    setPending(true)
    setApply({ kind: 'idle' })
    setOpen(true)

    const next = await fetchModelRecommendations({
      attachments,
      draft: getDraft(),
      policy: preset,
      profile,
      request: requestGateway
    })

    if (requestEpochRef.current !== epoch) {
      return
    }

    setResult(next)
    setPending(false)
  }, [attachments, getDraft, preset, profile, requestGateway])

  const choosePreset = async (next: RecommendationPreset) => {
    const previous = preset

    // Optimistic: the selector is a preference, and a failed save rolls it
    // back rather than leaving the UI asserting something was persisted.
    setPreset(next)

    try {
      await writeRecommendationPreset({ profile, request: requestGateway, value: next })
      setPresetPersisted(true)
    } catch {
      setPresetPersisted(false)
      setPreset(previous)
    }
  }

  const applyRecommendation = async (row: ModelRecommendation) => {
    const key = rowKey(row)

    setApply({ kind: 'applying', row: key })

    const applied = await onSelectModel({ model: row.model, provider: row.provider, sessionId })

    if (applied === false) {
      setApply({ kind: 'failed', row: key })

      return
    }

    // Effort rides the same session-scoped `config.set` the model menu uses.
    // With no live session it is composer state only — writing config.set
    // there falls back to profile config and would rewrite the user's default.
    if (row.effort) {
      if (!sessionId) {
        setCurrentReasoningEffort(row.effort)
      } else {
        sessionTileDelegate()?.updateSession(sessionId, state => ({ ...state, reasoningEffort: row.effort as string }))

        try {
          await requestGateway('config.set', { key: 'reasoning', session_id: sessionId, value: row.effort })
        } catch {
          setApply({ kind: 'failed', row: key })

          return
        }
      }
    }

    setApply({ kind: 'idle' })
    setOpen(false)
  }

  const ineligibleNote =
    eligibility.eligible || disabled
      ? null
      : eligibility.reason === 'too-many-attachments'
        ? copy.tooManyAttachments
        : eligibility.reason === 'draft-too-long'
          ? copy.draftTooLong
          : eligibility.reason === 'attachment-metadata'
            ? copy.attachmentUnsupported
            : null

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 flex-wrap items-center gap-1">
        <Button
          aria-controls={open ? panelId : undefined}
          aria-expanded={open}
          aria-label={copy.trigger}
          className="h-(--composer-control-size) min-w-0 shrink gap-1 rounded-md px-2 text-xs font-normal"
          data-testid="composer-recommend-trigger"
          disabled={!canRequest}
          onClick={() => void runRequest()}
          title={ineligibleNote ?? undefined}
          type="button"
          variant="ghost"
        >
          <Codicon name="lightbulb" size="0.75rem" />
          <span className="truncate">{copy.trigger}</span>
        </Button>
        <div aria-label={copy.presetLabel} className="flex min-w-0 flex-wrap items-center gap-0.5" role="group">
          {RECOMMENDATION_PRESETS.map(value => (
            <Button
              aria-pressed={preset === value}
              className={cn(
                'h-(--composer-control-size) shrink-0 rounded-md px-1.5 text-[0.6875rem] font-normal',
                preset === value && 'bg-(--chrome-action-hover) text-foreground'
              )}
              data-testid={`composer-recommend-preset-${value}`}
              disabled={disabled}
              key={value}
              onClick={() => void choosePreset(value)}
              type="button"
              variant="ghost"
            >
              {copy.presets[value]}
            </Button>
          ))}
          {presetPersisted ? null : (
            <span
              className="text-[0.6875rem] text-(--ui-text-tertiary)"
              data-testid="composer-recommend-preset-unsaved"
            >
              {copy.presetUnsaved}
            </span>
          )}
        </div>
      </div>
      {open ? (
        <div
          aria-label={copy.resultsLabel}
          className="flex min-w-0 flex-col gap-1 text-xs"
          data-testid="composer-recommend-panel"
          id={panelId}
          onKeyDown={event => {
            // Esc dismisses THIS surface and nothing else — the composer keeps
            // its own cancel gesture, and focus is left where the user put it.
            if (event.key === 'Escape') {
              event.stopPropagation()
              setOpen(false)
            }
          }}
          ref={panelRef}
          role="region"
          tabIndex={-1}
        >
          <p className="text-(--ui-text-tertiary)" data-testid="composer-recommend-privacy">
            {copy.privacy}
          </p>
          {pending ? (
            <p data-testid="composer-recommend-pending" role="status">
              {copy.pending}
            </p>
          ) : null}
          {!pending && result?.status === 'unsupported' ? (
            <p data-testid="composer-recommend-unsupported">{copy.unsupported}</p>
          ) : null}
          {!pending && result?.status === 'unavailable' ? (
            <p data-testid="composer-recommend-unavailable">{result.reason || copy.unavailable}</p>
          ) : null}
          {!pending && result?.status === 'failed' ? (
            <p className="flex flex-wrap items-center gap-2" data-testid="composer-recommend-failed">
              <span>{copy.failed}</span>
              <Button
                data-testid="composer-recommend-retry"
                onClick={() => void runRequest()}
                size="sm"
                type="button"
                variant="text"
              >
                {copy.retry}
              </Button>
            </p>
          ) : null}
          {!pending && result?.status === 'ok'
            ? result.recommendations.map(row => {
                const key = rowKey(row)
                const availability = row.availability?.status

                return (
                  <div
                    className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5"
                    data-testid="composer-recommend-row"
                    key={key}
                  >
                    <span className="min-w-0 break-words font-medium">
                      {row.provider} · {row.model}
                    </span>
                    {row.effort ? <span className="text-(--ui-text-tertiary)">{row.effort}</span> : null}
                    {availability && availability !== 'fresh' ? (
                      <span className="text-(--ui-text-tertiary)" data-testid="composer-recommend-availability">
                        {copy.availability[availability]}
                      </span>
                    ) : null}
                    {row.reason ? (
                      <span className="min-w-0 basis-full break-words text-(--ui-text-tertiary)">{row.reason}</span>
                    ) : null}
                    <Button
                      data-testid="composer-recommend-apply"
                      disabled={apply.kind === 'applying'}
                      onClick={() => void applyRecommendation(row)}
                      size="sm"
                      type="button"
                      variant="text"
                    >
                      {copy.apply}
                    </Button>
                    {apply.kind === 'failed' && apply.row === key ? (
                      <span data-testid="composer-recommend-apply-failed">{copy.applyFailed}</span>
                    ) : null}
                  </div>
                )
              })
            : null}
        </div>
      ) : null}
    </div>
  )
}
