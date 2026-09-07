import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import type {
  ModelSelectionOutcome,
  RecommendedModelSelection
} from '@/app/session/hooks/use-model-controls'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { SegmentedControl, type SegmentedControlOption } from '@/components/ui/segmented-control'
import { useI18n } from '@/i18n'
import type { ComposerAttachment } from '@/store/composer'

import {
  fetchModelRecommendations,
  type ModelRecommendation,
  RECOMMENDATION_PRESETS,
  recommendationEligibility,
  type RecommendationPreset,
  type RecommendationResult
} from './model-recommendation'
import { useRecommendationPresetPreference } from './use-recommendation-preset'

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

export interface ComposerRecommendProps {
  attachments: readonly ComposerAttachment[]
  disabled: boolean
  /** Reads the LIVE draft. The draft lives in the composer's contentEditable +
   *  draftRef, so a captured value would be stale; a getter also makes it
   *  structurally impossible for this surface to write it. */
  getDraft: () => string
  /** The existing session-aware model-selection path. Never reimplemented. */
  onSelectModel: (selection: RecommendedModelSelection) => Promise<ModelSelectionOutcome>
  profile: string
  requestGateway: RequestGateway
  /** Runtime id of the surface that owns this composer (null for a draft). */
  sessionId: null | string
  /** The composer's own draft-change notification. Optional: an owner that
   *  cannot supply one still gets correct behaviour, because the snapshot is
   *  re-checked at click AND at Apply — the subscription only makes staleness
   *  visible EARLIER, rather than being the thing that makes it safe. */
  subscribeDraft?: (listener: () => void) => () => void
}

type ApplyState =
  | { kind: 'applying'; row: string }
  | { kind: 'idle'; row?: string }
  | { kind: 'unconfirmed'; row: string }

/** Sentinel `SegmentedControl` value meaning "the stored preset is not known
 *  yet". Deliberately not a preset id, so no row can render as pressed. */
const PRESET_UNRESOLVED = 'unresolved'

type PresetTrackValue = RecommendationPreset | typeof PRESET_UNRESOLVED

/**
 * Exactly the inputs a recommendation was computed FOR. A result is only
 * applicable while every one of them still holds: a recommendation made for a
 * one-line question is not advice about the essay that replaced it, and one
 * made under `Balanced` is not advice under `Best quality`.
 */
interface RecommendationSnapshot {
  attachments: string
  draft: string
  policy: RecommendationPreset
  profile: string
}

const rowKey = (row: ModelRecommendation): string => `${row.provider}::${row.model}`

/** Identity of the attachment SET as the recommender sees it (metadata only). */
const attachmentsKey = (attachments: readonly ComposerAttachment[]): string =>
  JSON.stringify(attachments.map(attachment => [attachment.kind, attachment.label]))

const sameSnapshot = (a: RecommendationSnapshot, b: RecommendationSnapshot): boolean =>
  a.draft === b.draft && a.attachments === b.attachments && a.policy === b.policy && a.profile === b.profile

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
 * Four invariants make this safe to sit next to the send button:
 *
 *  - It NEVER submits. The trigger is `type="button"` (a bare button inside
 *    the composer form defaults to submit, which would send the very draft
 *    the user asked to evaluate), it reads the draft through a getter, and it
 *    has no path that writes the draft or moves focus into/out of the editor.
 *  - It NEVER decides silently. Every non-`ok` backend answer paints its own
 *    explicit state (setup / unavailable / failed-with-retry). Nothing is
 *    defaulted, cached-and-shown-as-live, or invented.
 *  - It NEVER shows stale advice as live. Every result carries the snapshot it
 *    was computed for; when the live draft, attachments, preset or profile no
 *    longer match it, the rows stop being applicable and say so. The composer
 *    deliberately does not re-render per keystroke, so the snapshot is
 *    re-validated at click AND at Apply, not merely during render.
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
  sessionId,
  subscribeDraft
}: ComposerRecommendProps) {
  const copy = useI18n().t.composer.recommend
  const panelId = useId()
  const statusId = `${panelId}-status`

  const { choosePreset, preset, presetPersisted, presetReady, resolvePreset } = useRecommendationPresetPreference(
    profile,
    requestGateway
  )

  const presetOptions = useMemo<readonly SegmentedControlOption<PresetTrackValue>[]>(
    () => RECOMMENDATION_PRESETS.map(id => ({ id, label: copy.presets[id] })),
    [copy.presets]
  )

  const [open, setOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [answer, setAnswer] = useState<null | { result: RecommendationResult; snapshot: RecommendationSnapshot }>(null)
  const [apply, setApply] = useState<ApplyState>({ kind: 'idle' })
  const [blocked, setBlocked] = useState<null | string>(null)
  // Bumped by the composer's draft subscription. It exists ONLY to re-run this
  // render's freshness comparison; the draft text itself never enters React
  // state, so typing still costs one cheap render and no repaint of the panel.
  const [draftRevision, setDraftRevision] = useState(0)
  // Guards a late response from a superseded click (the user can re-request
  // with a different preset while one is in flight).
  const requestEpochRef = useRef(0)
  const lastDraftRef = useRef(getDraft())

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment): lastDraftRef is a plain change-detector for the composer's own subscription, not a mirror of a nanostore
  useEffect(() => {
    if (!subscribeDraft) {
      return
    }

    return subscribeDraft(() => {
      const next = getDraft()

      // Re-render only on a real change: the composer's subscription fires for
      // attachments and programmatic syncs too.
      if (next !== lastDraftRef.current) {
        lastDraftRef.current = next
        setDraftRevision(revision => revision + 1)
      }
    })
  }, [getDraft, subscribeDraft])

  /** The snapshot as it is RIGHT NOW. Reads the live draft every time. */
  const currentSnapshot = useCallback(
    (policy: RecommendationPreset): RecommendationSnapshot => ({
      attachments: attachmentsKey(attachments),
      draft: getDraft(),
      policy,
      profile
    }),
    [attachments, getDraft, profile]
  )

  // A backend without the method can never answer; keep the control visibly
  // unavailable instead of offering a click that always fails.
  const unsupported = answer?.result.status === 'unsupported'

  // Render-time eligibility drives the DISABLED state only. It is advisory:
  // `draftRevision` refreshes it on every real edit, but the authoritative
  // check happens inside the click handler against the exact snapshot sent.
  const renderEligibility = useMemo(
    () => recommendationEligibility({ attachments, draft: getDraft() }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- draftRevision is the reactive input; getDraft is a stable live reader
    [attachments, draftRevision, getDraft]
  )

  const canRequest = !disabled && !unsupported && renderEligibility.eligible

  // Results are only live while the world they were computed for still holds.
  const live = useMemo(() => {
    if (!answer || !presetReady) {
      return null
    }

    return sameSnapshot(answer.snapshot, currentSnapshot(preset)) ? answer.result : null
    // eslint-disable-next-line react-hooks/exhaustive-deps -- draftRevision is the reactive draft input
  }, [answer, currentSnapshot, draftRevision, preset, presetReady])

  // A profile or gateway-route change makes both a previous answer and any
  // suspended async click meaningless. Invalidate BEFORE ordinary effects can
  // resume: an old click waiting on config.get must never issue a paid request
  // into the workspace the user already left.
  useLayoutEffect(() => {
    requestEpochRef.current += 1
    setAnswer(null)
    setBlocked(null)
    setApply({ kind: 'idle' })
    setOpen(false)
    setPending(false)
  }, [profile, requestGateway])

  const stale = !!answer && !live && !pending

  const runRequest = useCallback(async () => {
    const epoch = ++requestEpochRef.current

    setApply({ kind: 'idle' })
    setOpen(true)

    // The PERSISTED preset decides the policy, so a click that lands before
    // the profile-scoped read settles must wait for it. Sending the fallback
    // would silently ignore the user's saved preference on a paid call.
    const policy = await resolvePreset()

    if (requestEpochRef.current !== epoch) {
      return
    }

    // AUTHORITATIVE eligibility. The composer does not re-render per keystroke,
    // so the render-time check that enabled this button can be arbitrarily
    // stale; re-validate the exact snapshot that would cross the wire.
    const snapshot = currentSnapshot(policy)
    const eligibility = recommendationEligibility({ attachments, draft: snapshot.draft })

    if (!eligibility.eligible) {
      setBlocked(eligibility.reason)
      setAnswer(null)
      setPending(false)

      return
    }

    setBlocked(null)
    setPending(true)
    lastDraftRef.current = snapshot.draft

    const result = await fetchModelRecommendations({
      attachments,
      draft: snapshot.draft,
      policy,
      profile,
      request: requestGateway
    })

    if (requestEpochRef.current !== epoch) {
      return
    }

    setAnswer({ result, snapshot })
    setPending(false)
  }, [attachments, currentSnapshot, profile, requestGateway, resolvePreset])

  const applyRecommendation = async (row: ModelRecommendation) => {
    // Last-chance guard: with no draft subscription (or an edit that raced the
    // click) the rows on screen may already describe a draft that is gone.
    // Applying then would change the model for text nobody asked about.
    if (!answer || !sameSnapshot(answer.snapshot, currentSnapshot(preset))) {
      setDraftRevision(revision => revision + 1)
      lastDraftRef.current = getDraft()

      return
    }

    const key = rowKey(row)

    setApply({ kind: 'applying', row: key })

    // ONE call: model + effort travel together through the existing
    // session-aware path, so scoping (this session, never the profile
    // default), the expensive-model confirm handshake, optimistic paint,
    // authoritative reconciliation and rollback are all upstream's.
    const outcome = await onSelectModel({
      effort: row.effort,
      model: row.model,
      provider: row.provider,
      sessionId
    })

    if (outcome.kind !== 'applied') {
      setApply({ kind: 'unconfirmed', row: key })
    } else {
      setApply({ kind: 'idle', row: key })
      setOpen(false)
    }
  }

  const blockedNote =
    blocked === 'too-many-attachments'
      ? copy.tooManyAttachments
      : blocked === 'draft-too-long'
        ? copy.draftTooLong
        : blocked === 'attachment-metadata'
          ? copy.attachmentUnsupported
          : blocked === 'empty-draft'
            ? copy.emptyDraft
            : null

  const ineligibleNote =
    renderEligibility.eligible || disabled
      ? null
      : renderEligibility.reason === 'too-many-attachments'
        ? copy.tooManyAttachments
        : renderEligibility.reason === 'draft-too-long'
          ? copy.draftTooLong
          : renderEligibility.reason === 'attachment-metadata'
            ? copy.attachmentUnsupported
            : null

  return (
    <div
      className="flex min-w-0 flex-col gap-1"
      onKeyDown={event => {
        // Esc dismisses THIS surface and nothing else. It is bound on the
        // WRAPPER, not the panel: opening deliberately leaves focus in the
        // composer (or on the trigger), so a panel-only listener would never
        // see the key and Esc would fall through to the composer's own cancel
        // — halting a running turn the user only meant to stop looking at.
        // When nothing is open it is not ours, and it falls through untouched.
        if (event.key === 'Escape' && open) {
          event.stopPropagation()
          setOpen(false)
        }
      }}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-1">
        <Button
          aria-controls={open ? panelId : undefined}
          aria-describedby={ineligibleNote ? statusId : undefined}
          aria-expanded={open}
          className="h-(--composer-control-size) min-w-0 shrink gap-1 rounded-md px-2 text-xs font-normal"
          data-testid="composer-recommend-trigger"
          disabled={!canRequest}
          onClick={() => void runRequest()}
          type="button"
          variant="ghost"
        >
          <Codicon name="lightbulb" size="0.75rem" />
          <span className="truncate">{copy.trigger}</span>
        </Button>
        <div aria-label={copy.presetLabel} className="flex min-w-0 flex-wrap items-center gap-1" role="group">
          {/* The track is live from the first paint — a user who wants Best
              quality should not have to wait on a config read to say so — but
              until the profile-scoped read settles NOTHING is painted as
              selected. `PRESET_UNRESOLVED` is not an option id, so the
              primitive marks every row unpressed rather than asserting a
              stored choice nobody has read yet. */}
          <SegmentedControl
            className="max-w-full"
            disabled={disabled}
            onChange={value => {
              if (value !== PRESET_UNRESOLVED) {
                void choosePreset(value)
              }
            }}
            options={presetOptions}
            value={presetReady ? preset : PRESET_UNRESOLVED}
          />
          {presetReady ? null : (
            <span className="text-[0.6875rem] text-(--ui-text-tertiary)" data-testid="composer-recommend-preset-loading">
              {copy.presetLoading}
            </span>
          )}
          {presetReady && !presetPersisted ? (
            <span className="text-[0.6875rem] text-(--ui-text-tertiary)" data-testid="composer-recommend-preset-unsaved">
              {copy.presetUnsaved}
            </span>
          ) : null}
        </div>
      </div>
      {presetReady ? (
        <p className="text-[0.6875rem] text-(--ui-text-tertiary)" data-testid="composer-recommend-preset-description">
          {copy.presetDescriptions[preset]}
        </p>
      ) : null}
      {/* Visible, semantic and keyboard-reachable — a native `title=` would be
          an OS tooltip no keyboard user can reach (and DESIGN.md forbids it on
          buttons outright). `aria-describedby` ties it to the disabled trigger. */}
      {ineligibleNote ? (
        <p
          className="text-[0.6875rem] text-(--ui-text-tertiary)"
          data-testid="composer-recommend-ineligible-hint"
          id={statusId}
        >
          {ineligibleNote}
        </p>
      ) : null}
      {open ? (
        <div
          aria-label={copy.resultsLabel}
          className="flex min-w-0 flex-col gap-1 text-xs"
          data-testid="composer-recommend-panel"
          id={panelId}
          role="region"
          tabIndex={-1}
        >
          <p className="text-(--ui-text-tertiary)" data-testid="composer-recommend-privacy">
            {copy.privacy}
          </p>
          {blockedNote ? (
            <p data-testid="composer-recommend-ineligible" role="status">
              {blockedNote}
            </p>
          ) : null}
          {pending ? (
            <p data-testid="composer-recommend-pending" role="status">
              {copy.pending}
            </p>
          ) : null}
          {stale ? (
            <p className="flex flex-wrap items-center gap-2" data-testid="composer-recommend-stale" role="status">
              <span>{copy.stale}</span>
              <Button
                data-testid="composer-recommend-refresh-stale"
                onClick={() => void runRequest()}
                size="sm"
                type="button"
                variant="text"
              >
                {copy.refresh}
              </Button>
            </p>
          ) : null}
          {!pending && live?.status === 'unsupported' ? (
            <p data-testid="composer-recommend-unsupported">{copy.unsupported}</p>
          ) : null}
          {!pending && live?.status === 'unavailable' ? (
            <p data-testid="composer-recommend-unavailable">{live.reason || copy.unavailable}</p>
          ) : null}
          {!pending && live?.status === 'failed' ? (
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
          {!pending && live?.status === 'ok'
            ? live.recommendations.map(row => {
                const key = rowKey(row)
                const availability = row.availability
                // The backend's own verdict on whether this route can be used
                // right now. Rendering Apply on a limit-reached route would
                // offer a switch the gateway will refuse.
                const usable = availability?.allowed !== false && availability?.limit_reached !== true

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
                    {/* Reported states are shown verbatim — INCLUDING `fresh`.
                        Suppressing it made "checked, live" look identical to
                        "never reported", which is the freshness claim the card
                        asks for. Absent availability still prints nothing. */}
                    {availability?.status ? (
                      <span className="text-(--ui-text-tertiary)" data-testid="composer-recommend-availability">
                        {copy.availability[availability.status]}
                      </span>
                    ) : null}
                    {availability?.limit_reached ? (
                      <span className="text-(--ui-text-tertiary)" data-testid="composer-recommend-limit-reached">
                        {copy.limitReached}
                      </span>
                    ) : null}
                    {availability?.allowed === false && !availability.limit_reached ? (
                      <span className="text-(--ui-text-tertiary)" data-testid="composer-recommend-not-allowed">
                        {copy.notAllowed}
                      </span>
                    ) : null}
                    {row.reason ? (
                      <span className="min-w-0 basis-full break-words text-(--ui-text-tertiary)">{row.reason}</span>
                    ) : null}
                    {usable ? (
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
                    ) : null}
                    {apply.kind === 'unconfirmed' && apply.row === key ? (
                      <span data-testid="composer-recommend-apply-unconfirmed">{copy.applyUnconfirmed}</span>
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
