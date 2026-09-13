// Structured turn-error descriptor forwarded by the gateway (see
// agent/error_surface.py). Names WHICH layer of the stack failed so the error
// card can say "Provider error" / "Gateway error" and offer layer-appropriate
// recovery actions, instead of toasting an opaque string.
//
// Advisory contract: older backends never send this — every consumer must
// keep working when it is absent (legacy string-sniffing stays as fallback).

export const ERROR_SURFACE_LAYERS = [
  'provider',
  'endpoint',
  'streaming',
  'auth',
  'billing',
  'gateway',
  'runtime',
  'disk'
] as const

export type ErrorSurfaceLayer = (typeof ERROR_SURFACE_LAYERS)[number]

export interface ErrorSurface {
  layer: ErrorSurfaceLayer
  /** Specific failure code (a FailoverReason value or site-specific code). */
  code: string
  /** False when retrying unchanged reproduces the same failure. */
  retryable: boolean
  /** The failing session's provider/model, captured at classification time —
   *  preferred over the foreground composer's atoms, which can point at a
   *  different model by the time the user clicks an action. */
  provider?: string
  model?: string
  /** Phase 2.12: best-effort epoch-seconds when a provider/upstream rate
   *  limit is expected to clear (wire `reset_at`, rate_limit /
   *  upstream_rate_limit only — see agent/error_surface.py). Absent when the
   *  backend has no usable reset time; never fabricate one client-side. */
  resetAt?: number
  /** Phase 2.12: tri-state fallback-chain visibility (wire
   *  `fallback_available`). `true` = an untried fallback provider/model
   *  exists for this turn; `false` = a chain was configured but exhausted;
   *  absent = no fallback chain is configured at all. Older backends never
   *  send this — treat absence the same as `false` for gating purposes. */
  fallbackAvailable?: boolean
}

/** Validate a wire payload into an ErrorSurface, or null when absent/garbled. */
export function parseErrorSurface(value: unknown): ErrorSurface | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as {
    code?: unknown
    fallback_available?: unknown
    layer?: unknown
    model?: unknown
    provider?: unknown
    reset_at?: unknown
    retryable?: unknown
  }
  const layer = typeof raw.layer === 'string' ? (raw.layer as ErrorSurfaceLayer) : null

  if (!layer || !ERROR_SURFACE_LAYERS.includes(layer)) {
    return null
  }

  // reset_at: a finite number, epoch seconds. Reject NaN/Infinity so a
  // garbled wire value can never paint a nonsensical "reset time".
  const resetAt = typeof raw.reset_at === 'number' && Number.isFinite(raw.reset_at) ? raw.reset_at : undefined

  return {
    layer,
    code: typeof raw.code === 'string' && raw.code ? raw.code : 'unknown',
    retryable: raw.retryable !== false,
    ...(typeof raw.provider === 'string' && raw.provider ? { provider: raw.provider } : {}),
    ...(typeof raw.model === 'string' && raw.model ? { model: raw.model } : {}),
    ...(resetAt !== undefined ? { resetAt } : {}),
    ...(typeof raw.fallback_available === 'boolean' ? { fallbackAvailable: raw.fallback_available } : {})
  }
}

/** Plain-text error-details blob for the error card's "Copy error details". */
export function formatErrorDiagnostics(input: {
  appVersion?: string
  errorText: string
  model?: string
  provider?: string
  surface?: ErrorSurface | null
}): string {
  // The descriptor's identity (captured when the turn failed) beats the
  // caller-supplied fallback (typically the foreground composer's atoms).
  const provider = input.surface?.provider || input.provider
  const model = input.surface?.model || input.model

  const lines = [
    '── Hermes error details ──',
    `time: ${new Date().toISOString()}`,
    input.surface ? `layer: ${input.surface.layer}` : null,
    input.surface ? `code: ${input.surface.code}` : null,
    input.surface ? `retryable: ${input.surface.retryable}` : null,
    provider ? `provider: ${provider}` : null,
    model ? `model: ${model}` : null,
    // Phase 2.12: surface the rate-limit recovery hints when present so a
    // pasted diagnostics blob shows exactly what the error card offered.
    input.surface?.resetAt !== undefined ? `reset_at: ${new Date(input.surface.resetAt * 1000).toISOString()}` : null,
    input.surface?.fallbackAvailable !== undefined ? `fallback_available: ${input.surface.fallbackAvailable}` : null,
    input.appVersion ? `app: ${input.appVersion}` : null,
    `error: ${input.errorText}`
  ]

  return lines.filter((line): line is string => Boolean(line)).join('\n')
}
