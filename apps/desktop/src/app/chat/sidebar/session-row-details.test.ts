import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { sessionRowIdentity } from './session-row-details'

function makeSession(overrides: Partial<SessionInfo>): SessionInfo {
  return {
    id: 's1',
    last_active: 0,
    started_at: 0,
    ...overrides
  } as unknown as SessionInfo
}

describe('sessionRowIdentity', () => {
  it('shows only the configured family when the served route matches', () => {
    const identity = sessionRowIdentity(
      makeSession({ configured_provider: 'anthropic', served_provider: 'anthropic' })
    )

    expect(identity).toEqual({ configured: 'Claude', served: null })
  })

  it('is case-insensitive when comparing configured vs served', () => {
    const identity = sessionRowIdentity(
      makeSession({ configured_provider: 'Anthropic', served_provider: 'ANTHROPIC' })
    )

    expect(identity).toEqual({ configured: 'Claude', served: null })
  })

  it('surfaces the served family only on a real mismatch (fallback case)', () => {
    const identity = sessionRowIdentity(
      makeSession({ configured_provider: 'anthropic', served_provider: 'openai-codex' })
    )

    expect(identity).toEqual({ configured: 'Claude', served: 'Codex' })
  })

  it('renders nothing for a legacy session with no resolvable provider', () => {
    const identity = sessionRowIdentity(makeSession({ configured_provider: null, served_provider: null }))

    expect(identity).toEqual({ configured: null, served: null })
  })

  it('never infers a mismatch family when only the served side is unresolved', () => {
    const identity = sessionRowIdentity(makeSession({ configured_provider: 'anthropic', served_provider: null }))

    expect(identity).toEqual({ configured: 'Claude', served: null })
  })

  it('falls back to a title-cased label for an unrecognized provider, never a false Claude/Codex identity', () => {
    const identity = sessionRowIdentity(
      makeSession({ configured_provider: 'my-custom-endpoint', served_provider: 'my-custom-endpoint' })
    )

    expect(identity).toEqual({ configured: 'My Custom Endpoint', served: null })
  })

  it('treats a bare billing bucket as unresolved rather than a provider identity', () => {
    const identity = sessionRowIdentity(makeSession({ configured_provider: 'auto', served_provider: 'auto' }))

    expect(identity).toEqual({ configured: null, served: null })
  })
})
