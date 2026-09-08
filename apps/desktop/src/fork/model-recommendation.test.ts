import { describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'

import {
  fetchModelRecommendations,
  normalizeRecommendationPreset,
  readRecommendationPreset,
  RECOMMENDATION_METHOD,
  RECOMMENDATION_PRESET_CONFIG_KEY,
  RECOMMENDATION_PRESETS,
  recommendationAttachmentMetadata,
  recommendationEligibility,
  writeRecommendationPreset
} from './model-recommendation'

const attachment = (over: Partial<ComposerAttachment> = {}): ComposerAttachment => ({
  id: 'a1',
  kind: 'file',
  label: 'brief.pdf',
  ...over
})

describe('recommendation preset contract', () => {
  it('declares exactly the FINAL v1 preset values', () => {
    expect([...RECOMMENDATION_PRESETS]).toEqual(['balanced', 'save_codex', 'best_quality'])
  })

  it('reads an unknown/missing/miscased stored preset as balanced without inventing a value', () => {
    expect(normalizeRecommendationPreset('save_codex')).toBe('save_codex')
    expect(normalizeRecommendationPreset('Balanced')).toBe('balanced')
    expect(normalizeRecommendationPreset('turbo')).toBe('balanced')
    expect(normalizeRecommendationPreset(undefined)).toBe('balanced')
    expect(normalizeRecommendationPreset(null)).toBe('balanced')
    expect(normalizeRecommendationPreset(7)).toBe('balanced')
  })
})

describe('privacy boundary: attachment metadata mapping', () => {
  it('sends only label→name and kind→kind, never paths, details, refs or preview bytes', () => {
    const metadata = recommendationAttachmentMetadata([
      attachment({
        detail: '/Users/me/secret/brief.pdf',
        label: 'brief.pdf',
        path: '/Users/me/secret/brief.pdf',
        previewUrl: 'data:image/png;base64,AAAA',
        refText: '@file:/Users/me/secret/brief.pdf',
        thumbnailUrl: 'data:image/png;base64,BBBB'
      })
    ])

    expect(metadata).toEqual([{ kind: 'file', name: 'brief.pdf' }])
    expect(JSON.stringify(metadata)).not.toContain('secret')
    expect(JSON.stringify(metadata)).not.toContain('base64')
  })

  it('omits unknown MIME type and size rather than guessing them', () => {
    const [entry] = recommendationAttachmentMetadata([attachment()])

    expect(Object.keys(entry).sort()).toEqual(['kind', 'name'])
  })
})

describe('eligibility is decided before any request', () => {
  it('rejects an empty or whitespace-only draft', () => {
    expect(recommendationEligibility({ attachments: [], draft: '' })).toEqual({
      eligible: false,
      reason: 'empty-draft'
    })

    expect(recommendationEligibility({ attachments: [], draft: '  \n\t ' })).toEqual({
      eligible: false,
      reason: 'empty-draft'
    })
  })

  it('accepts a draft whose only content is surrounded by whitespace, and never trims it', () => {
    expect(recommendationEligibility({ attachments: [], draft: '  hello  ' })).toEqual({ eligible: true })
  })

  it('counts draft length in code points, not UTF-16 units', () => {
    // 60_000 astral code points = 120_000 UTF-16 units: under the 100k
    // code-point cap, so a code-unit count would wrongly reject it.
    const astral = '🙂'.repeat(60_000)

    expect(recommendationEligibility({ attachments: [], draft: astral })).toEqual({ eligible: true })
    expect(recommendationEligibility({ attachments: [], draft: 'a'.repeat(100_001) })).toEqual({
      eligible: false,
      reason: 'draft-too-long'
    })
  })

  it('refuses more than 32 attachments instead of silently dropping the overflow', () => {
    const many = Array.from({ length: 33 }, (_, index) => attachment({ id: `a${index}` }))

    expect(recommendationEligibility({ attachments: many, draft: 'hi' })).toEqual({
      eligible: false,
      reason: 'too-many-attachments'
    })
  })

  it('refuses an over-long label instead of truncating or dropping the attachment', () => {
    expect(
      recommendationEligibility({ attachments: [attachment({ label: 'x'.repeat(257) })], draft: 'hi' })
    ).toEqual({ eligible: false, reason: 'attachment-metadata' })
  })
})

describe('fetchModelRecommendations request boundary', () => {
  it('sends exactly profile, draft, attachment metadata and the explicit policy', async () => {
    const request = vi.fn().mockResolvedValue({ policy: 'save_codex', recommendations: [], status: 'ok' })

    await fetchModelRecommendations({
      attachments: [attachment({ path: '/tmp/secret.txt' })],
      draft: '  build me a parser  ',
      policy: 'save_codex',
      profile: 'work',
      request
    })

    expect(request).toHaveBeenCalledTimes(1)
    const [method, params] = request.mock.calls[0]

    expect(method).toBe(RECOMMENDATION_METHOD)
    expect(Object.keys(params as object).sort()).toEqual(['attachments', 'draft', 'policy', 'profile'])
    expect(params).toEqual({
      attachments: [{ kind: 'file', name: 'brief.pdf' }],
      draft: '  build me a parser  ',
      policy: 'save_codex',
      profile: 'work'
    })
  })

  it('reports an older backend that does not know the method as unsupported, not an error', async () => {
    const request = vi.fn().mockRejectedValue(new Error('Method not found'))

    await expect(
      fetchModelRecommendations({ attachments: [], draft: 'hi', policy: 'balanced', profile: 'default', request })
    ).resolves.toEqual({ status: 'unsupported' })
  })

  it('surfaces an explicit unavailable result with its reason', async () => {
    const request = vi.fn().mockResolvedValue({ reason: 'No router configured', recommendations: [], status: 'unavailable' })

    await expect(
      fetchModelRecommendations({ attachments: [], draft: 'hi', policy: 'balanced', profile: 'default', request })
    ).resolves.toEqual({ reason: 'No router configured', status: 'unavailable' })
  })

  it('treats an ok response with no candidates as unavailable rather than fabricating one', async () => {
    const request = vi.fn().mockResolvedValue({ policy: 'balanced', recommendations: [], status: 'ok' })

    await expect(
      fetchModelRecommendations({ attachments: [], draft: 'hi', policy: 'balanced', profile: 'default', request })
    ).resolves.toEqual({ status: 'unavailable' })
  })

  it('keeps every configured provider result, in backend rank order, without hardcoding a family', async () => {
    const request = vi.fn().mockResolvedValue({
      policy: 'balanced',
      recommendations: [
        { effort: 'medium', model: 'gpt-5.6-terra', provider: 'openai-codex', reason: 'Cheapest adequate' },
        {
          availability: { allowed: true, limit_reached: false, status: 'stale' },
          effort: 'high',
          model: 'claude-opus-5',
          provider: 'anthropic',
          reason: 'Strongest'
        }
      ],
      status: 'ok'
    })

    const result = await fetchModelRecommendations({
      attachments: [],
      draft: 'hi',
      policy: 'balanced',
      profile: 'default',
      request
    })

    expect(result.status).toBe('ok')
    expect(result.status === 'ok' && result.recommendations.map(item => item.provider)).toEqual([
      'openai-codex',
      'anthropic'
    ])
    expect(result.status === 'ok' && result.recommendations[1].availability?.status).toBe('stale')
  })

  it('reports a real call failure as failed, keeping the message', async () => {
    const request = vi.fn().mockRejectedValue(new Error('router timed out'))

    await expect(
      fetchModelRecommendations({ attachments: [], draft: 'hi', policy: 'balanced', profile: 'default', request })
    ).resolves.toEqual({ message: 'router timed out', status: 'failed' })
  })
})

describe('profile-scoped preset persistence uses the parent-owned config contract', () => {
  it('reads through config.get with an explicit profile', async () => {
    const request = vi.fn().mockResolvedValue({ profile: 'work', value: 'best_quality' })

    await expect(readRecommendationPreset({ profile: 'work', request })).resolves.toEqual({
      persisted: true,
      value: 'best_quality'
    })

    expect(request).toHaveBeenCalledWith('config.get', {
      key: RECOMMENDATION_PRESET_CONFIG_KEY,
      profile: 'work'
    })
  })

  it('falls back to balanced and reports persistence unavailable on an older backend', async () => {
    const request = vi.fn().mockRejectedValue(new Error('4002: unknown config key'))

    await expect(readRecommendationPreset({ profile: 'work', request })).resolves.toEqual({
      persisted: false,
      value: 'balanced'
    })
  })

  it('writes through config.set and rejects so a failed save stays visibly failed', async () => {
    const request = vi.fn().mockResolvedValue({
      key: RECOMMENDATION_PRESET_CONFIG_KEY,
      profile: 'work',
      value: 'save_codex'
    })

    await writeRecommendationPreset({ profile: 'work', request, value: 'save_codex' })

    expect(request).toHaveBeenCalledWith('config.set', {
      key: RECOMMENDATION_PRESET_CONFIG_KEY,
      profile: 'work',
      value: 'save_codex'
    })

    const failing = vi.fn().mockRejectedValue(new Error('Could not save model recommendation preset'))

    await expect(writeRecommendationPreset({ profile: 'work', request: failing, value: 'balanced' })).rejects.toThrow(
      /Could not save/
    )
  })
})
