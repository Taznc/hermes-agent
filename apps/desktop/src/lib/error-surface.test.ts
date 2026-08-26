import { describe, expect, it } from 'vitest'

import { formatErrorDiagnostics, parseErrorSurface } from './error-surface'

describe('parseErrorSurface', () => {
  it('accepts a valid descriptor', () => {
    expect(parseErrorSurface({ layer: 'streaming', code: 'stream_drop', retryable: true })).toEqual({
      layer: 'streaming',
      code: 'stream_drop',
      retryable: true
    })
  })

  it('accepts every documented layer', () => {
    for (const layer of ['provider', 'endpoint', 'streaming', 'auth', 'billing', 'gateway', 'runtime', 'disk']) {
      expect(parseErrorSurface({ layer, code: 'x', retryable: false })?.layer).toBe(layer)
    }
  })

  it('rejects unknown layers and non-objects', () => {
    expect(parseErrorSurface({ layer: 'blockchain', code: 'x', retryable: true })).toBeNull()
    expect(parseErrorSurface('provider')).toBeNull()
    expect(parseErrorSurface(null)).toBeNull()
    expect(parseErrorSurface(undefined)).toBeNull()
    expect(parseErrorSurface(7)).toBeNull()
  })

  it('defaults code and retryable when missing', () => {
    expect(parseErrorSurface({ layer: 'gateway' })).toEqual({ layer: 'gateway', code: 'unknown', retryable: true })
  })

  it('honors retryable=false', () => {
    expect(parseErrorSurface({ layer: 'auth', code: 'auth_permanent', retryable: false })?.retryable).toBe(false)
  })

  it('carries the failing session identity when present', () => {
    const surface = parseErrorSurface({
      layer: 'provider',
      code: 'rate_limit',
      retryable: true,
      provider: 'openrouter',
      model: 'test/m1'
    })

    expect(surface?.provider).toBe('openrouter')
    expect(surface?.model).toBe('test/m1')
    // Absent identity yields no keys, not empty strings.
    expect(parseErrorSurface({ layer: 'provider', code: 'x', retryable: true })?.provider).toBeUndefined()
  })
})

describe('parseErrorSurface — Phase 2.12 rate-limit fields', () => {
  it('parses reset_at (wire) into resetAt (epoch seconds)', () => {
    const surface = parseErrorSurface({
      layer: 'provider',
      code: 'rate_limit',
      retryable: true,
      reset_at: 1_700_000_000
    })

    expect(surface?.resetAt).toBe(1_700_000_000)
  })

  it('parses fallback_available (wire) into fallbackAvailable', () => {
    expect(
      parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true, fallback_available: true })
        ?.fallbackAvailable
    ).toBe(true)
    expect(
      parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true, fallback_available: false })
        ?.fallbackAvailable
    ).toBe(false)
  })

  it('omits both fields when absent — tolerant of older backends', () => {
    const surface = parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true })

    expect(surface?.resetAt).toBeUndefined()
    expect(surface?.fallbackAvailable).toBeUndefined()
    expect('resetAt' in (surface ?? {})).toBe(false)
    expect('fallbackAvailable' in (surface ?? {})).toBe(false)
  })

  it('rejects a garbled reset_at (NaN/Infinity/non-number) rather than fabricating a time', () => {
    expect(parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true, reset_at: NaN })?.resetAt).toBeUndefined()
    expect(
      parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true, reset_at: Infinity })?.resetAt
    ).toBeUndefined()
    expect(
      parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true, reset_at: 'soon' })?.resetAt
    ).toBeUndefined()
  })

  it('ignores a non-boolean fallback_available', () => {
    expect(
      parseErrorSurface({ layer: 'provider', code: 'rate_limit', retryable: true, fallback_available: 'yes' })
        ?.fallbackAvailable
    ).toBeUndefined()
  })
})

describe('formatErrorDiagnostics', () => {
  it('includes layer, code, model and error', () => {
    const text = formatErrorDiagnostics({
      errorText: 'boom',
      model: 'anthropic/claude-opus-4.6',
      surface: { layer: 'provider', code: 'rate_limit', retryable: true }
    })

    expect(text).toContain('layer: provider')
    expect(text).toContain('code: rate_limit')
    expect(text).toContain('model: anthropic/claude-opus-4.6')
    expect(text).toContain('error: boom')
  })

  it('prefers the descriptor identity over the caller fallback', () => {
    const text = formatErrorDiagnostics({
      errorText: 'boom',
      // Foreground composer atom — potentially stale by click time.
      model: 'some/other-model',
      surface: { layer: 'provider', code: 'rate_limit', retryable: true, provider: 'openrouter', model: 'failed/model' }
    })

    expect(text).toContain('provider: openrouter')
    expect(text).toContain('model: failed/model')
    expect(text).not.toContain('some/other-model')
  })

  it('omits absent fields without leaving blank lines', () => {
    const text = formatErrorDiagnostics({ errorText: 'boom' })

    expect(text).not.toContain('layer:')
    expect(text).not.toContain('model:')
    expect(text.split('\n').every(line => line.trim().length > 0)).toBe(true)
  })

  it('includes reset_at and fallback_available when the surface carries them (Phase 2.12)', () => {
    const text = formatErrorDiagnostics({
      errorText: 'rate limited',
      surface: {
        layer: 'provider',
        code: 'rate_limit',
        retryable: true,
        resetAt: 1_700_000_000,
        fallbackAvailable: true
      }
    })

    expect(text).toContain('reset_at: ')
    expect(text).toContain('fallback_available: true')
  })

  it('omits reset_at/fallback_available lines when the surface lacks them', () => {
    const text = formatErrorDiagnostics({
      errorText: 'boom',
      surface: { layer: 'provider', code: 'unknown', retryable: true }
    })

    expect(text).not.toContain('reset_at:')
    expect(text).not.toContain('fallback_available:')
  })
})
