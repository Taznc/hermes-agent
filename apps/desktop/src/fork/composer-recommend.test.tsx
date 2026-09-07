import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'
import { $activeSessionId, setCurrentReasoningEffort } from '@/store/session'

import { ComposerRecommend } from './composer-recommend'

const OK_TWO_PROVIDERS = {
  policy: 'balanced',
  recommendations: [
    {
      capabilities: { effort_options: ['low', 'medium', 'high'], reasoning: true },
      effort: 'medium',
      model: 'gpt-5.6-terra',
      provider: 'openai-codex',
      reason: 'Cheapest adequate route for a short refactor.'
    },
    {
      availability: { allowed: true, limit_reached: false, status: 'stale' },
      effort: 'high',
      model: 'claude-opus-5',
      provider: 'anthropic',
      reason: 'Strongest route if quality matters more than spend.'
    }
  ],
  status: 'ok'
}

function setup(
  over: {
    attachments?: ComposerAttachment[]
    draft?: string
    disabled?: boolean
    onSelectModel?: ReturnType<typeof vi.fn>
    request?: ReturnType<typeof vi.fn>
    sessionId?: null | string
  } = {}
) {
  const draftRef = { current: over.draft ?? 'Refactor the parser for me' }

  const request =
    over.request ??
    vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'balanced' }
      }

      if (method === 'config.set') {
        return { key: 'model_recommendation.preset', profile: 'work', value: 'balanced' }
      }

      return OK_TWO_PROVIDERS
    })

  const onSelectModel = over.onSelectModel ?? vi.fn().mockResolvedValue(true)

  const utils = render(
    <ComposerRecommend
      attachments={over.attachments ?? []}
      disabled={over.disabled ?? false}
      getDraft={() => draftRef.current}
      onSelectModel={onSelectModel as never}
      profile="work"
      requestGateway={request as never}
      sessionId={over.sessionId ?? null}
    />
  )

  return { draftRef, onSelectModel, request, utils }
}

const recommendButton = () => screen.getByTestId('composer-recommend-trigger')

const openResults = async () => {
  fireEvent.click(recommendButton())
  await screen.findByTestId('composer-recommend-panel')
}

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  setCurrentReasoningEffort('')
  vi.restoreAllMocks()
})

describe('Recommend control: manual invocation only', () => {
  it('makes no request until the user clicks it', async () => {
    const { request } = setup()

    // The preset read is allowed (it is what the selector displays); a
    // recommendation must never be requested without an explicit click.
    await waitFor(() => expect(request).toHaveBeenCalled())
    expect(request.mock.calls.every(([method]) => method !== 'model_recommendation.get')).toBe(true)
  })

  it('is disabled on an empty draft and enabled once there is content', async () => {
    const { utils } = setup({ draft: '   ' })

    expect(recommendButton().hasAttribute('disabled')).toBe(true)
    utils.unmount()

    setup({ draft: 'hello' })
    expect(recommendButton().hasAttribute('disabled')).toBe(false)
  })

  it('is disabled when the composer is disabled', () => {
    setup({ disabled: true })
    expect(recommendButton().hasAttribute('disabled')).toBe(true)
  })

  it('sends the complete untrimmed draft plus attachment metadata only, exactly once', async () => {
    const { draftRef, request } = setup({
      attachments: [
        { id: 'a1', kind: 'file', label: 'brief.pdf', path: '/Users/me/private/brief.pdf', refText: '@file:x' }
      ],
      draft: '  keep my whitespace  '
    })

    await openResults()

    const calls = request.mock.calls.filter(([method]) => method === 'model_recommendation.get')

    expect(calls).toHaveLength(1)
    expect(calls[0][1]).toEqual({
      attachments: [{ kind: 'file', name: 'brief.pdf' }],
      draft: '  keep my whitespace  ',
      policy: 'balanced',
      profile: 'work'
    })
    expect(JSON.stringify(calls[0][1])).not.toContain('private')
    // The draft is read, never written.
    expect(draftRef.current).toBe('  keep my whitespace  ')
  })

  it('never submits the prompt', async () => {
    const submit = vi.fn()

    render(
      <form onSubmit={submit}>
        <ComposerRecommend
          attachments={[]}
          disabled={false}
          getDraft={() => 'draft'}
          onSelectModel={vi.fn()}
          profile="work"
          requestGateway={vi.fn().mockResolvedValue(OK_TWO_PROVIDERS) as never}
          sessionId={null}
        />
      </form>
    )

    const trigger = screen.getAllByTestId('composer-recommend-trigger')[0]

    expect(trigger.getAttribute('type')).toBe('button')
    fireEvent.click(trigger)
    await waitFor(() => expect(submit).not.toHaveBeenCalled())
  })
})

describe('Recommend results surface', () => {
  it('renders one row per configured provider, in backend rank order, with no hardcoded family', async () => {
    setup()
    await openResults()

    const rows = screen.getAllByTestId('composer-recommend-row')

    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('openai-codex')
    expect(rows[0].textContent).toContain('gpt-5.6-terra')
    expect(rows[1].textContent).toContain('anthropic')
    expect(rows[1].textContent).toContain('claude-opus-5')
  })

  it('shows the reason and effort each row carries', async () => {
    setup()
    await openResults()

    expect(screen.getByText(/Cheapest adequate route/)).toBeTruthy()
    expect(screen.getAllByTestId('composer-recommend-row')[1].textContent).toContain('high')
  })

  it('marks a stale availability instead of presenting it as live capacity', async () => {
    setup()
    await openResults()

    const rows = screen.getAllByTestId('composer-recommend-row')

    expect(rows[0].querySelector('[data-testid="composer-recommend-availability"]')).toBeNull()
    expect(rows[1].querySelector('[data-testid="composer-recommend-availability"]')?.textContent).toMatch(/stale/i)
  })

  it('states the privacy boundary on the surface', async () => {
    setup()
    await openResults()

    expect(screen.getByTestId('composer-recommend-privacy').textContent).toMatch(/draft/i)
  })
})

describe('Apply is scoped to the active view and never sends', () => {
  it('applies provider, model and effort to this session only', async () => {
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'balanced' }
      }

      if (method === 'config.set') {
        return { ok: true }
      }

      return OK_TWO_PROVIDERS
    })

    const { onSelectModel } = setup({ request, sessionId: 'run-7' })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[1])

    await waitFor(() =>
      expect(onSelectModel).toHaveBeenCalledWith({
        model: 'claude-opus-5',
        provider: 'anthropic',
        sessionId: 'run-7'
      })
    )

    await waitFor(() =>
      expect(
        request.mock.calls.some(
          ([method, params]) =>
            method === 'config.set' &&
            (params as Record<string, unknown>).key === 'reasoning' &&
            (params as Record<string, unknown>).session_id === 'run-7' &&
            (params as Record<string, unknown>).value === 'high'
        )
      ).toBe(true)
    )

    // Never a global/profile-default model write.
    expect(
      request.mock.calls.some(
        ([method, params]) => method === 'config.set' && (params as Record<string, unknown>).key === 'model'
      )
    ).toBe(false)
  })

  it('surfaces a recovery path when the model switch fails and does not claim success', async () => {
    const onSelectModel = vi.fn().mockResolvedValue(false)
    const { draftRef } = setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    await screen.findByTestId('composer-recommend-apply-failed')
    expect(draftRef.current).toBe('Refactor the parser for me')
  })
})

describe('Unavailable and failure states are explicit', () => {
  it('explains setup when no router is configured', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'balanced' }
      }

      return { reason: 'No recommendation router configured', recommendations: [], status: 'unavailable' }
    })

    setup({ request })
    await openResults()

    expect(screen.getByTestId('composer-recommend-unavailable')).toBeTruthy()
    expect(screen.queryAllByTestId('composer-recommend-row')).toHaveLength(0)
  })

  it('disables the control with an unavailable note when the backend lacks the capability', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'balanced' }
      }

      throw new Error('Method not found')
    })

    setup({ request })
    await openResults()

    expect(screen.getByTestId('composer-recommend-unsupported')).toBeTruthy()
    await waitFor(() => expect(recommendButton().hasAttribute('disabled')).toBe(true))
  })

  it('offers a retry when the call fails outright', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'balanced' }
      }

      throw new Error('router timed out')
    })

    setup({ request })
    await openResults()

    expect(screen.getByTestId('composer-recommend-failed')).toBeTruthy()
    expect(screen.getByTestId('composer-recommend-retry')).toBeTruthy()
  })
})

describe('Preset selector', () => {
  it('shows the persisted preset before a request is made', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'best_quality' }
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    await waitFor(() =>
      expect(screen.getByTestId('composer-recommend-preset-best_quality').getAttribute('aria-pressed')).toBe('true')
    )
  })

  it.each(['balanced', 'save_codex', 'best_quality'])('persists and sends the %s preset', async preset => {
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'balanced' }
      }

      if (method === 'config.set') {
        return { key: 'model_recommendation.preset', profile: 'work', value: preset }
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    await waitFor(() => expect(screen.getByTestId(`composer-recommend-preset-${preset}`)).toBeTruthy())
    fireEvent.click(screen.getByTestId(`composer-recommend-preset-${preset}`))

    await waitFor(() =>
      expect(
        request.mock.calls.some(
          ([method, params]) =>
            method === 'config.set' &&
            (params as Record<string, unknown>).key === 'model_recommendation.preset' &&
            (params as Record<string, unknown>).profile === 'work' &&
            (params as Record<string, unknown>).value === preset
        )
      ).toBe(true)
    )

    await openResults()

    const call = request.mock.calls.find(([method]) => method === 'model_recommendation.get')

    expect((call?.[1] as Record<string, unknown>).policy).toBe(preset)
  })

  it('keeps the choice visible but reports it unsaved on a backend without the config key', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'config.get') {
        throw new Error('4002: unknown config key')
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    await waitFor(() =>
      expect(screen.getByTestId('composer-recommend-preset-balanced').getAttribute('aria-pressed')).toBe('true')
    )
    expect(screen.getByTestId('composer-recommend-preset-unsaved')).toBeTruthy()
  })
})

describe('Keyboard and focus behaviour', () => {
  it('closes the results on Escape without touching the draft', async () => {
    const { draftRef } = setup()

    await openResults()
    fireEvent.keyDown(screen.getByTestId('composer-recommend-panel'), { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('composer-recommend-panel')).toBeNull())
    expect(draftRef.current).toBe('Refactor the parser for me')
  })

  it('labels the trigger semantically rather than relying on an icon tooltip', () => {
    setup()

    expect(recommendButton().getAttribute('aria-label')).toBeTruthy()
    expect(recommendButton().textContent?.trim()).toBeTruthy()
  })
})
