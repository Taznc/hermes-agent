// Fork-owned client for the backend model-recommendation contract.
//
// The gateway owns the routing decision (`docs/model-recommendation-gateway.md`,
// backed by `hermes_fork/model_recommendation/`); this module is the renderer's
// half of that wire contract and nothing more. It deliberately holds NO React,
// NO stores and NO ranking logic: the composer surface renders whatever the
// backend ranked, in the order it ranked it.
//
// Two boundaries are load-bearing and are the reason this is a module rather
// than inline code in the composer:
//
//  1. PRIVACY. The recommender may see the complete unsent draft plus
//     attachment METADATA and nothing else. `recommendationAttachmentMetadata`
//     constructs a fresh allow-listed object per attachment — never a spread of
//     `ComposerAttachment`, which carries local paths, `refText`, preview and
//     thumbnail data URLs. A spread would leak bytes and filesystem layout to a
//     third-party router with no visible symptom.
//
//  2. ELIGIBILITY. The backend rejects an ineligible request with a JSON-RPC
//     validation error; deciding eligibility here means the control is
//     honestly disabled instead of offering a click that always fails. The
//     limits mirror the documented v1 contract exactly (code points, 32
//     attachments, 256-code-point strings) — the renderer must never trim,
//     truncate or drop an entry to manufacture eligibility.

import type { ComposerAttachment } from '@/store/composer'

/** JSON-RPC method the backend registers through its fork gateway anchor. */
export const RECOMMENDATION_METHOD = 'model_recommendation.get'

/** The single allow-listed config key the parent contract added. */
export const RECOMMENDATION_PRESET_CONFIG_KEY = 'model_recommendation.preset'

/** FINAL v1 presets. Order is the order the selector offers them in. */
export const RECOMMENDATION_PRESETS = ['balanced', 'save_codex', 'best_quality'] as const

export type RecommendationPreset = (typeof RECOMMENDATION_PRESETS)[number]

export const DEFAULT_RECOMMENDATION_PRESET: RecommendationPreset = 'balanced'

/** Documented v1 limits. Mirrored, never re-derived from a guess. */
const MAX_DRAFT_CODE_POINTS = 100_000
const MAX_ATTACHMENTS = 32
const MAX_METADATA_CODE_POINTS = 256

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

export interface RecommendationAttachmentMetadata {
  kind?: string
  name?: string
}

export interface RecommendationAvailability {
  allowed?: boolean
  limit_reached?: boolean
  status?: 'failed' | 'fresh' | 'stale' | 'unavailable' | 'unsupported'
}

export interface ModelRecommendation {
  availability?: RecommendationAvailability
  capabilities?: { effort_options?: string[]; fast?: boolean; reasoning?: boolean }
  effort?: string
  model: string
  provider: string
  reason?: string
}

/**
 * What the composer surface may render. `unsupported` is an older backend
 * (capability absence — a disabled control, not an app error); `unavailable`
 * is a configured-but-unusable router; `failed` is a real call failure with a
 * retry path. None of them may be painted as a recommendation.
 */
export type RecommendationResult =
  | { message?: string; status: 'failed' }
  | { policy: RecommendationPreset; recommendations: ModelRecommendation[]; status: 'ok' }
  | { reason?: string; status: 'unavailable' }
  | { status: 'unsupported' }

export type RecommendationIneligibleReason =
  | 'attachment-metadata'
  | 'draft-too-long'
  | 'empty-draft'
  | 'too-many-attachments'

export type RecommendationEligibility =
  | { eligible: false; reason: RecommendationIneligibleReason }
  | { eligible: true }

/** Coerces any stored/echoed value to a known preset. Never invents one. */
export function normalizeRecommendationPreset(value: unknown): RecommendationPreset {
  if (typeof value !== 'string') {
    return DEFAULT_RECOMMENDATION_PRESET
  }

  const normalized = value.trim().toLowerCase()

  return (RECOMMENDATION_PRESETS as readonly string[]).includes(normalized)
    ? (normalized as RecommendationPreset)
    : DEFAULT_RECOMMENDATION_PRESET
}

const withinBounds = (value: string): boolean =>
  value.length > 0 && Array.from(value).length <= MAX_METADATA_CODE_POINTS

/**
 * The allow-list. `label → name`, `kind → kind`, everything else dropped.
 * MIME type and byte size are omitted rather than guessed: the composer does
 * not know them, and statting a file to fill them would cross the boundary
 * this whole feature promises not to cross.
 */
export function recommendationAttachmentMetadata(
  attachments: readonly ComposerAttachment[]
): RecommendationAttachmentMetadata[] {
  return attachments.map(attachment => {
    const entry: RecommendationAttachmentMetadata = {}

    if (typeof attachment.label === 'string' && withinBounds(attachment.label)) {
      entry.name = attachment.label
    }

    if (typeof attachment.kind === 'string' && withinBounds(attachment.kind)) {
      entry.kind = attachment.kind
    }

    return entry
  })
}

/**
 * Whether this draft may be sent at all. The draft is measured but never
 * modified: `trim()` is used only to answer "is there any content", while the
 * COMPLETE original string is what gets forwarded.
 */
export function recommendationEligibility({
  attachments,
  draft
}: {
  attachments: readonly ComposerAttachment[]
  draft: string
}): RecommendationEligibility {
  if (!draft.trim()) {
    return { eligible: false, reason: 'empty-draft' }
  }

  if (Array.from(draft).length > MAX_DRAFT_CODE_POINTS) {
    return { eligible: false, reason: 'draft-too-long' }
  }

  if (attachments.length > MAX_ATTACHMENTS) {
    return { eligible: false, reason: 'too-many-attachments' }
  }

  // An attachment whose label does not fit the contract disables the control
  // and says so, rather than being truncated (which would misdescribe the
  // user's file) or omitted (which would silently understate the payload).
  const metadataFits = attachments.every(attachment => {
    const label = typeof attachment.label === 'string' ? attachment.label : ''

    return label.length === 0 || withinBounds(label)
  })

  return metadataFits ? { eligible: true } : { eligible: false, reason: 'attachment-metadata' }
}

/** True when the backend predates the method (capability absence). */
function isUnknownMethod(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /method not found|-32601|unknown method|no such method/i.test(message)
}

/** True when the backend rejected the config KEY (older backend, no persistence). */
function isUnknownConfigKey(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /4002/.test(message) || /unknown config key|unsupported config key/i.test(message)
}

/**
 * One explicit recommendation request. Exactly four fields cross the wire, and
 * `policy` is always sent explicitly: the backend defaults an OMITTED policy to
 * `balanced`, so relying on the default would silently ignore the user's
 * persisted preset.
 */
export async function fetchModelRecommendations({
  attachments,
  draft,
  policy,
  profile,
  request
}: {
  attachments: readonly ComposerAttachment[]
  draft: string
  policy: RecommendationPreset
  profile: string
  request: RequestGateway
}): Promise<RecommendationResult> {
  let raw: unknown

  try {
    raw = await request<unknown>(RECOMMENDATION_METHOD, {
      attachments: recommendationAttachmentMetadata(attachments),
      draft,
      policy,
      profile
    })
  } catch (error) {
    if (isUnknownMethod(error)) {
      return { status: 'unsupported' }
    }

    return { message: error instanceof Error ? error.message : String(error), status: 'failed' }
  }

  const payload = (raw ?? {}) as {
    policy?: unknown
    reason?: unknown
    recommendations?: unknown
    status?: unknown
  }

  if (payload.status !== 'ok') {
    return typeof payload.reason === 'string' && payload.reason
      ? { reason: payload.reason, status: 'unavailable' }
      : { status: 'unavailable' }
  }

  const recommendations = Array.isArray(payload.recommendations)
    ? (payload.recommendations as ModelRecommendation[]).filter(
        item => typeof item?.provider === 'string' && typeof item?.model === 'string'
      )
    : []

  // An `ok` with nothing in it is not a recommendation. Rendering an empty
  // ranked list as success is exactly the "fabricate / silently default"
  // failure the card forbids.
  if (recommendations.length === 0) {
    return { status: 'unavailable' }
  }

  return { policy: normalizeRecommendationPreset(payload.policy), recommendations, status: 'ok' }
}

/**
 * Reads this profile's persisted preset. A backend without the key is not an
 * error — it is persistence unavailable, and the caller must show the choice
 * as session-local rather than claiming it was saved.
 */
export async function readRecommendationPreset({
  profile,
  request
}: {
  profile: string
  request: RequestGateway
}): Promise<{ persisted: boolean; value: RecommendationPreset }> {
  try {
    const result = await request<{ value?: unknown }>('config.get', {
      key: RECOMMENDATION_PRESET_CONFIG_KEY,
      profile
    })

    return { persisted: true, value: normalizeRecommendationPreset(result?.value) }
  } catch (error) {
    if (isUnknownConfigKey(error)) {
      return { persisted: false, value: DEFAULT_RECOMMENDATION_PRESET }
    }

    // A read failure (corrupt file, unknown profile) is still "we do not know
    // what is stored" — fall back to the documented default and tell the
    // caller nothing is persisted, rather than asserting a stored value.
    return { persisted: false, value: DEFAULT_RECOMMENDATION_PRESET }
  }
}

/**
 * Persists a deliberate selector change. Deliberately REJECTS on failure: a
 * swallowed error here would leave the UI claiming a preference was saved when
 * the backend refused it (managed pin, corrupt file, older backend).
 */
export async function writeRecommendationPreset({
  profile,
  request,
  value
}: {
  profile: string
  request: RequestGateway
  value: RecommendationPreset
}): Promise<void> {
  await request<unknown>('config.set', {
    key: RECOMMENDATION_PRESET_CONFIG_KEY,
    profile,
    value
  })
}
