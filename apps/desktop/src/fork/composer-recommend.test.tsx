import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TRANSLATIONS } from '@/i18n/catalog'
import type { ComposerAttachment } from '@/store/composer'
import { $activeSessionId, setCurrentReasoningEffort } from '@/store/session'

import { deferred } from '../test/deferred'

import { ComposerRecommend } from './composer-recommend'

const OK_TWO_PROVIDERS = {
  policy: 'balanced',
  recommendations: [
    {
      availability: { allowed: true, limit_reached: false, status: 'fresh' },
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
    /** `null` simulates a composer that supplies no draft subscription. */
    subscribeDraft?: null
  } = {}
) {
  const draftRef = { current: over.draft ?? 'Refactor the parser for me' }
  const listeners = new Set<() => void>()

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

  // The real contract shape. A bare `true` is the legacy boolean `selectModel`
  // returns, not what `selectRecommendedModel` hands this surface.
  const onSelectModel = over.onSelectModel ?? vi.fn().mockResolvedValue({ kind: 'applied' })

  const subscribeDraft =
    over.subscribeDraft === null
      ? undefined
      : (listener: () => void) => {
          listeners.add(listener)

          return () => listeners.delete(listener)
        }

  const surface = (profile: string, attachments: readonly ComposerAttachment[]) => (
    <ComposerRecommend
      attachments={attachments}
      disabled={over.disabled ?? false}
      getDraft={() => draftRef.current}
      onSelectModel={onSelectModel as never}
      profile={profile}
      requestGateway={request as never}
      sessionId={over.sessionId ?? null}
      subscribeDraft={subscribeDraft}
    />
  )

  const attachments = over.attachments ?? []
  const utils = render(surface('work', attachments))

  return {
    draftRef,
    /** What the composer's own draft subscription does on every edit. */
    notifyDraftChanged: () => act(() => listeners.forEach(listener => listener())),
    onSelectModel,
    request,
    rerenderWithAttachments: (next: ComposerAttachment[]) => utils.rerender(surface('work', next)),
    rerenderWithProfile: (profile: string) => utils.rerender(surface(profile, attachments)),
    utils
  }
}

const recommendButton = () => screen.getByTestId('composer-recommend-trigger')

/** The preset the surface currently claims is selected, or null when none is. */
const presetPressed = (): null | string =>
  screen
    .getAllByRole('button')
    .find(node => node.getAttribute('aria-pressed') === 'true')
    ?.textContent?.trim() ?? null

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

  it('renders every supplied availability state, including a fresh one', async () => {
    setup()
    await openResults()

    const rows = screen.getAllByTestId('composer-recommend-row')

    // The card requires availability/freshness "where supplied". Suppressing
    // `fresh` made a checked-and-live route indistinguishable from one whose
    // availability was never reported at all.
    expect(rows[0].querySelector('[data-testid="composer-recommend-availability"]')?.textContent).toMatch(/live/i)
    expect(rows[1].querySelector('[data-testid="composer-recommend-availability"]')?.textContent).toMatch(/stale/i)
  })

  it('says nothing about availability when the backend supplied none', async () => {
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { value: 'balanced' }
      }

      return {
        policy: 'balanced',
        recommendations: [{ model: 'gpt-5.6-terra', provider: 'openai-codex' }],
        status: 'ok'
      }
    })

    setup({ request })
    await openResults()

    expect(screen.queryByTestId('composer-recommend-availability')).toBeNull()
  })

  it('marks a route the backend says is not allowed, and does not offer to apply it', async () => {
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { value: 'balanced' }
      }

      return {
        policy: 'balanced',
        recommendations: [
          {
            availability: { allowed: false, limit_reached: true, status: 'fresh' },
            effort: 'high',
            model: 'claude-opus-5',
            provider: 'anthropic'
          },
          { effort: 'medium', model: 'gpt-5.6-terra', provider: 'openai-codex' }
        ],
        status: 'ok'
      }
    })

    const { onSelectModel } = setup({ request })

    await openResults()

    const rows = screen.getAllByTestId('composer-recommend-row')

    expect(rows[0].textContent).toMatch(/limit/i)
    expect(rows[0].querySelector('[data-testid="composer-recommend-apply"]')).toBeNull()
    // The other route is unaffected — this is per-row, not a whole-panel gate.
    expect(rows[1].querySelector('[data-testid="composer-recommend-apply"]')).toBeTruthy()
    expect(onSelectModel).not.toHaveBeenCalled()
  })

  it('states the privacy boundary on the surface', async () => {
    setup()
    await openResults()

    expect(screen.getByTestId('composer-recommend-privacy').textContent).toMatch(/draft/i)
  })
})

describe('Apply is scoped to the active view and never sends', () => {
  it('applies provider, model and effort as ONE selection scoped to this view', async () => {
    const { onSelectModel, request } = setup({ sessionId: 'run-7' })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[1])

    // Model AND effort go through the one session-aware selection path, so
    // scoping, the confirm handshake and rollback are not re-implemented here.
    await waitFor(() =>
      expect(onSelectModel).toHaveBeenCalledWith({
        effort: 'high',
        model: 'claude-opus-5',
        provider: 'anthropic',
        sessionId: 'run-7'
      })
    )

    expect(onSelectModel).toHaveBeenCalledTimes(1)

    // No second write from this surface — in particular no direct reasoning
    // write, which is what previously bypassed primary-vs-tile scoping.
    expect(request.mock.calls.some(([method, params]) => method === 'config.set' && params?.key === 'reasoning')).toBe(
      false
    )
    expect(request.mock.calls.some(([method, params]) => method === 'config.set' && params?.key === 'model')).toBe(false)
  })

  it('does not claim failure when the switch is merely awaiting confirmation', async () => {
    // A confirmation that is still open: `settled` never resolves while the
    // user has not answered the prompt.
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'confirmation_pending', settled: new Promise(() => {}) })

    const { draftRef } = setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    const notice = await screen.findByTestId('composer-recommend-apply-unconfirmed')

    expect(notice.textContent).not.toMatch(/could not|failed/i)
    // The results stay open so the user can confirm or choose another row.
    expect(screen.getByTestId('composer-recommend-panel')).toBeTruthy()
    expect(draftRef.current).toBe('Refactor the parser for me')
  })
})

// A real failure and a pending confirmation are DIFFERENT events, and the
// surface must not say "confirm the switch" for either a selection that
// already failed or a confirmation that has since failed. These tests pin the
// three shapes separately, because collapsing any two of them is exactly the
// dishonest state this card exists to remove.
describe('Apply failure is distinct from a pending confirmation', () => {
  it('reports an immediate Apply failure as failed, never as awaiting confirmation', async () => {
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'failed', recovery: 'not_needed' })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    const notice = await screen.findByTestId('composer-recommend-apply-failed')

    // The load-bearing assertion: no confirm guidance for a path that already
    // failed — there is nothing left to confirm.
    expect(screen.queryByTestId('composer-recommend-apply-unconfirmed')).toBeNull()
    expect(notice.textContent).toMatch(/did not|not applied|could not/i)
    expect(notice.textContent).not.toMatch(/confirm/i)

    // The recovery affordance IS the row's own Apply, left usable.
    const applyButton = screen.getAllByTestId('composer-recommend-apply')[0] as HTMLButtonElement

    expect(applyButton.disabled).toBe(false)
    // The panel stays open: closing it would hide the failure the user needs.
    expect(screen.getByTestId('composer-recommend-panel')).toBeTruthy()
  })

  it('says the previous model is still in use when the failure was rolled back', async () => {
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'failed', recovery: 'restored' })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    const notice = await screen.findByTestId('composer-recommend-apply-failed')

    expect(notice.textContent).toMatch(/previous model/i)
    expect(screen.queryByTestId('composer-recommend-apply-unrestored')).toBeNull()
  })

  it('never claims a rollback the gateway refused', async () => {
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'failed', recovery: 'restore_failed' })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    const notice = await screen.findByTestId('composer-recommend-apply-unrestored')

    // An honest half-applied report: it must NOT say the previous model is
    // still in use, because the compensation failed.
    expect(notice.textContent).toMatch(/check|model menu|verify/i)
    expect(screen.queryByTestId('composer-recommend-apply-failed')).toBeNull()
  })

  it('marks only the row that failed', async () => {
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'failed', recovery: 'not_needed' })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[1])

    await screen.findByTestId('composer-recommend-apply-failed')

    expect(screen.getAllByTestId('composer-recommend-apply-failed')).toHaveLength(1)
  })

  it('clears a previous failure when the row is applied again', async () => {
    const onSelectModel = vi
      .fn()
      .mockResolvedValueOnce({ kind: 'failed', recovery: 'not_needed' })
      .mockResolvedValue({ kind: 'applied' })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])
    await screen.findByTestId('composer-recommend-apply-failed')

    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    await waitFor(() => expect(screen.queryByTestId('composer-recommend-panel')).toBeNull())
    expect(screen.queryByTestId('composer-recommend-apply-failed')).toBeNull()
  })
})

// A confirmation the user answered is no longer pending. Leaving "confirm the
// switch" on screen after the confirmed resend failed is the stale guidance
// this card names; leaving it after the resend SUCCEEDED is just as wrong.
describe('A settled confirmation replaces the confirm guidance', () => {
  it('closes the surface when the confirmed switch is finally applied', async () => {
    const settled = deferred<{ kind: 'applied' }>()
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'confirmation_pending', settled: settled.promise })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])
    await screen.findByTestId('composer-recommend-apply-unconfirmed')

    await act(async () => {
      settled.resolve({ kind: 'applied' })
      await settled.promise
    })

    await waitFor(() => expect(screen.queryByTestId('composer-recommend-panel')).toBeNull())
  })

  it('replaces the confirm guidance with a failure when the confirmed switch fails', async () => {
    const settled = deferred<{ kind: 'failed'; recovery: 'not_needed' }>()
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'confirmation_pending', settled: settled.promise })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])
    await screen.findByTestId('composer-recommend-apply-unconfirmed')

    await act(async () => {
      settled.resolve({ kind: 'failed', recovery: 'not_needed' })
      await settled.promise
    })

    await screen.findByTestId('composer-recommend-apply-failed')
    // The exact stale-guidance regression: the confirm prompt must be gone.
    expect(screen.queryByTestId('composer-recommend-apply-unconfirmed')).toBeNull()
  })

  it('reports an unrestored post-confirm compensation failure honestly', async () => {
    const settled = deferred<{ kind: 'failed'; recovery: 'restore_failed' }>()
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'confirmation_pending', settled: settled.promise })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])
    await screen.findByTestId('composer-recommend-apply-unconfirmed')

    await act(async () => {
      settled.resolve({ kind: 'failed', recovery: 'restore_failed' })
      await settled.promise
    })

    await screen.findByTestId('composer-recommend-apply-unrestored')
    expect(screen.queryByTestId('composer-recommend-apply-unconfirmed')).toBeNull()
  })

  it('keeps the confirm guidance while the confirmation is genuinely still pending', async () => {
    const settled = deferred<{ kind: 'applied' }>()
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'confirmation_pending', settled: settled.promise })

    setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    const notice = await screen.findByTestId('composer-recommend-apply-unconfirmed')

    expect(notice.textContent).toMatch(/confirm/i)
    expect(screen.queryByTestId('composer-recommend-apply-failed')).toBeNull()

    settled.resolve({ kind: 'applied' })
  })

  it('ignores a settled confirmation that lands after the workspace changed', async () => {
    const settled = deferred<{ kind: 'failed'; recovery: 'not_needed' }>()
    const onSelectModel = vi.fn().mockResolvedValue({ kind: 'confirmation_pending', settled: settled.promise })
    const { rerenderWithProfile } = setup({ onSelectModel })

    await openResults()
    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])
    await screen.findByTestId('composer-recommend-apply-unconfirmed')

    // The user left this workspace; a stale answer about the profile they left
    // must not paint anything here.
    rerenderWithProfile('home')

    await act(async () => {
      settled.resolve({ kind: 'failed', recovery: 'not_needed' })
      await settled.promise
    })

    expect(screen.queryByTestId('composer-recommend-apply-failed')).toBeNull()
    expect(screen.queryByTestId('composer-recommend-panel')).toBeNull()
  })
})

// The renderer sends attachment `name` AND `kind`
// (`recommendationAttachmentMetadata`), so copy claiming only "names" understates
// what crosses the wire. Every locale that authors this string must describe
// metadata honestly and keep the explicit exclusions.
describe('Privacy disclosure matches what is actually sent', () => {
  // Per-locale terms rather than one English regex: the contract is that each
  // locale says BOTH fields in its own language and keeps all three
  // exclusions, which a language-agnostic assertion cannot check.
  const DISCLOSURE = {
    ar: { exclusions: [/سجل المحادثة/u, /محتويات الملفات/u, /ملفات المشروع/u], sends: [/مسودت/u, /أسماء/u, /أنواع/u] },
    en: {
      exclusions: [/conversation history/i, /file contents/i, /project files/i],
      sends: [/draft/i, /name/i, /type/i]
    },
    ja: {
      exclusions: [/会話履歴/u, /ファイルの内容/u, /プロジェクトファイル/u],
      sends: [/下書き/u, /名前/u, /種類/u]
    },
    zh: { exclusions: [/对话历史/u, /文件内容/u, /项目文件/u], sends: [/草稿/u, /名称/u, /类型/u] },
    'zh-hant': { exclusions: [/對話紀錄/u, /檔案內容/u, /專案檔案/u], sends: [/草稿/u, /名稱/u, /類型/u] }
  } as const

  const LOCALES = Object.keys(DISCLOSURE) as (keyof typeof DISCLOSURE)[]

  it.each(LOCALES)('locale "%s" discloses attachment names AND types, not names alone', locale => {
    const privacy = TRANSLATIONS[locale].composer.recommend.privacy

    expect(typeof privacy).toBe('string')

    for (const term of DISCLOSURE[locale].sends) {
      expect(privacy).toMatch(term)
    }
  })

  it.each(LOCALES)('locale "%s" keeps all three explicit exclusions', locale => {
    const privacy = TRANSLATIONS[locale].composer.recommend.privacy

    for (const term of DISCLOSURE[locale].exclusions) {
      expect(privacy).toMatch(term)
    }
  })

  it('renders the disclosure on the surface itself', async () => {
    setup()
    await openResults()

    const rendered = screen.getByTestId('composer-recommend-privacy').textContent ?? ''

    expect(rendered).toMatch(/draft/i)
    expect(rendered).toMatch(/type/i)
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
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'best_quality' }
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    await waitFor(() => expect(presetPressed()).toBe('Best quality'))
  })

  it('describes what each preset optimizes for, not just its name', async () => {
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { profile: 'work', value: 'save_codex' }
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    const description = await screen.findByTestId('composer-recommend-preset-description')

    // The description must be about the SELECTED preset and say something the
    // three-word label does not.
    expect(description.textContent).toMatch(/Codex/)
    expect(description.textContent!.length).toBeGreaterThan('Save Codex'.length)
  })

  it('offers the presets through the shared SegmentedControl, not a bespoke button row', async () => {
    setup()

    await waitFor(() => expect(presetPressed()).toBe('Balanced'))

    const track = screen.getByRole('button', { name: 'Balanced' }).parentElement!
    const labels = Array.from(track.querySelectorAll('button')).map(node => node.textContent?.trim())

    // The primitive's own shape: one grid track holding exactly the three
    // options, each an aria-pressed button (see components/ui/segmented-control).
    expect(labels).toEqual(['Balanced', 'Save Codex', 'Best quality'])
    expect(track.className).toContain('grid-flow-col')
  })

  it.each([
    ['balanced', 'Balanced'],
    ['save_codex', 'Save Codex'],
    ['best_quality', 'Best quality']
  ])('persists and sends the %s preset', async (preset, label) => {
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

    await waitFor(() => expect(screen.getByRole('button', { name: label })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: label }))

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
    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        throw new Error('4002: unknown config key')
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    await waitFor(() => expect(presetPressed()).toBe('Balanced'))
    expect(screen.getByTestId('composer-recommend-preset-unsaved')).toBeTruthy()
  })
})

describe('Keyboard and focus behaviour', () => {
  it('closes the results on Escape pressed from the element that actually has focus', async () => {
    const { draftRef } = setup()

    const trigger = recommendButton()

    trigger.focus()
    fireEvent.click(trigger)
    await screen.findByTestId('composer-recommend-panel')

    // Opening must NOT move focus (the user is composing). So Escape arrives on
    // whatever they had focused — here the trigger — and the surface must still
    // dismiss. Dispatching on the panel would test a path no user takes.
    expect(document.activeElement).toBe(trigger)
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('composer-recommend-panel')).toBeNull())
    expect(document.activeElement).toBe(trigger)
    expect(draftRef.current).toBe('Refactor the parser for me')
  })

  it('dismisses the surface INSTEAD of the composer’s own cancel gesture', async () => {
    const composerCancel = vi.fn()
    const draftRef = { current: 'Refactor the parser for me' }

    render(
      // The real composer listens for Escape on an ancestor (halt turn / cancel
      // queued edit). One Escape must do exactly one thing.
      <div onKeyDown={event => event.key === 'Escape' && composerCancel()}>
        <ComposerRecommend
          attachments={[]}
          disabled={false}
          getDraft={() => draftRef.current}
          onSelectModel={vi.fn() as never}
          profile="work"
          requestGateway={vi.fn().mockResolvedValue(OK_TWO_PROVIDERS) as never}
          sessionId={null}
        />
      </div>
    )

    const trigger = screen.getByTestId('composer-recommend-trigger')

    trigger.focus()
    fireEvent.click(trigger)
    await screen.findByTestId('composer-recommend-panel')

    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('composer-recommend-panel')).toBeNull())
    expect(composerCancel).not.toHaveBeenCalled()
  })

  it('lets Escape through to the composer when this surface has nothing open', async () => {
    const composerCancel = vi.fn()
    const draftRef = { current: 'Refactor the parser for me' }

    render(
      <div onKeyDown={event => event.key === 'Escape' && composerCancel()}>
        <ComposerRecommend
          attachments={[]}
          disabled={false}
          getDraft={() => draftRef.current}
          onSelectModel={vi.fn() as never}
          profile="work"
          requestGateway={vi.fn().mockResolvedValue(OK_TWO_PROVIDERS) as never}
          sessionId={null}
        />
      </div>
    )

    const trigger = screen.getByTestId('composer-recommend-trigger')

    trigger.focus()
    fireEvent.keyDown(trigger, { key: 'Escape' })

    expect(composerCancel).toHaveBeenCalledTimes(1)
  })

  it('explains an ineligible draft in visible, keyboard-reachable text — never a native tooltip', async () => {
    setup({ draft: 'x'.repeat(100_001) })

    const trigger = recommendButton()
    const hint = await screen.findByTestId('composer-recommend-ineligible-hint')

    expect(trigger.hasAttribute('disabled')).toBe(true)
    expect(trigger.hasAttribute('title')).toBe(false)
    expect(trigger.getAttribute('aria-describedby')).toBe(hint.id)
    expect(hint.textContent?.trim()).toBeTruthy()
  })

  it('labels the trigger with visible text rather than an icon tooltip', () => {
    setup()

    expect(recommendButton().textContent?.trim()).toBeTruthy()
    expect(recommendButton().hasAttribute('title')).toBe(false)
  })
})

describe('Preset resolution races (review round 1, finding 1)', () => {
  const deferred = <T,>() => {
    let resolve!: (value: T) => void
    let reject!: (error: unknown) => void

    const promise = new Promise<T>((res, rej) => {
      resolve = res
      reject = rej
    })

    return { promise, reject, resolve }
  }

  it('never sends the fallback policy while the persisted preset is still being read', async () => {
    const read = deferred<{ value: string }>()

    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return read.promise
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })

    // The user clicks Recommend before the profile-scoped read resolves.
    fireEvent.click(recommendButton())
    await Promise.resolve()

    expect(request.mock.calls.some(([method]) => method === 'model_recommendation.get')).toBe(false)

    read.resolve({ value: 'best_quality' })

    await waitFor(() => expect(request.mock.calls.some(([m]) => m === 'model_recommendation.get')).toBe(true))

    const call = request.mock.calls.find(([m]) => m === 'model_recommendation.get')

    expect((call?.[1] as Record<string, unknown>).policy).toBe('best_quality')
  })

  it('claims no persisted choice until the read resolves', async () => {
    const read = deferred<{ value: string }>()

    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) =>
      method === 'config.get' ? read.promise : OK_TWO_PROVIDERS
    )

    setup({ request })

    await waitFor(() => expect(screen.getByTestId('composer-recommend-preset-loading')).toBeTruthy())
    expect(presetPressed()).toBeNull()

    read.resolve({ value: 'save_codex' })
    await waitFor(() => expect(presetPressed()).toBe('Save Codex'))
  })

  it('does not let a late read overwrite a newer user choice', async () => {
    const read = deferred<{ value: string }>()

    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) =>
      method === 'config.get' ? read.promise : { ok: true }
    )

    setup({ request })
    await waitFor(() => expect(screen.getByTestId('composer-recommend-preset-loading')).toBeTruthy())

    // The user picks while the read is still in flight.
    fireEvent.click(screen.getByRole('button', { name: /Best quality/ }))
    await waitFor(() => expect(presetPressed()).toBe('Best quality'))

    read.resolve({ value: 'balanced' })
    await Promise.resolve()
    await Promise.resolve()

    expect(presetPressed()).toBe('Best quality')
  })

  it('discards a read that resolves after the profile changed', async () => {
    const workRead = deferred<{ value: string }>()
    const homeRead = deferred<{ value: string }>()

    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return params?.profile === 'work' ? workRead.promise : homeRead.promise
      }

      return OK_TWO_PROVIDERS
    })

    const { rerenderWithProfile } = setup({ request })

    await waitFor(() => expect(screen.getByTestId('composer-recommend-preset-loading')).toBeTruthy())
    rerenderWithProfile('home')
    homeRead.resolve({ value: 'save_codex' })
    await waitFor(() => expect(presetPressed()).toBe('Save Codex'))

    // The abandoned profile's read lands late with a different value.
    workRead.resolve({ value: 'best_quality' })
    await Promise.resolve()
    await Promise.resolve()

    expect(presetPressed()).toBe('Save Codex')
  })

  it('keeps the newest write authoritative when two saves resolve out of order', async () => {
    const first = deferred<{ ok: boolean }>()
    const second = deferred<{ ok: boolean }>()
    let writes = 0

    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { value: 'balanced' }
      }

      if (method === 'config.set') {
        writes += 1

        return writes === 1 ? first.promise : second.promise
      }

      return OK_TWO_PROVIDERS
    })

    setup({ request })
    await waitFor(() => expect(presetPressed()).toBe('Balanced'))

    fireEvent.click(screen.getByRole('button', { name: /Save Codex/ }))
    fireEvent.click(screen.getByRole('button', { name: /Best quality/ }))

    // The SUPERSEDED write fails last; it must not roll the newer choice back.
    second.resolve({ ok: true })
    first.reject(new Error('write failed'))

    await Promise.resolve()
    await Promise.resolve()
    await waitFor(() => expect(presetPressed()).toBe('Best quality'))
    expect(screen.queryByTestId('composer-recommend-preset-unsaved')).toBeNull()
  })
})

describe('Click-time eligibility and result freshness (review round 1, finding 2)', () => {
  it('revalidates the live draft at click time, not only at render time', async () => {
    const { draftRef, request } = setup()

    // The composer deliberately does NOT rerender on every keystroke, so the
    // render-time eligibility check can be arbitrarily stale. The draft grows
    // past the documented limit without any rerender.
    draftRef.current = 'x'.repeat(100_001)

    fireEvent.click(recommendButton())

    await waitFor(() => expect(screen.getByTestId('composer-recommend-ineligible')).toBeTruthy())
    expect(request.mock.calls.some(([method]) => method === 'model_recommendation.get')).toBe(false)
  })

  it('sends the exact snapshot it validated', async () => {
    const { draftRef, request } = setup()

    draftRef.current = 'the newest text, typed after the last render'
    fireEvent.click(recommendButton())

    await waitFor(() => expect(request.mock.calls.some(([m]) => m === 'model_recommendation.get')).toBe(true))

    const call = request.mock.calls.find(([m]) => m === 'model_recommendation.get')

    expect((call?.[1] as Record<string, unknown>).draft).toBe('the newest text, typed after the last render')
  })

  it('marks results stale and withdraws Apply when the draft they were made for changes', async () => {
    const { draftRef, notifyDraftChanged, onSelectModel } = setup()

    await openResults()
    expect(screen.queryByTestId('composer-recommend-stale')).toBeNull()
    expect(screen.getAllByTestId('composer-recommend-apply').length).toBeGreaterThan(0)

    draftRef.current = 'a completely different question about database indexes'
    notifyDraftChanged()

    await screen.findByTestId('composer-recommend-stale')
    expect(screen.queryAllByTestId('composer-recommend-apply')).toHaveLength(0)
    expect(onSelectModel).not.toHaveBeenCalled()
  })

  it('stays fresh when the draft notification carries no actual change', async () => {
    const { notifyDraftChanged } = setup()

    await openResults()
    notifyDraftChanged()

    await Promise.resolve()
    expect(screen.queryByTestId('composer-recommend-stale')).toBeNull()
  })

  it('refuses an Apply against a draft that changed, even with no draft subscription', async () => {
    const { draftRef, onSelectModel } = setup({ subscribeDraft: null })

    await openResults()
    draftRef.current = 'a completely different question about database indexes'

    fireEvent.click(screen.getAllByTestId('composer-recommend-apply')[0])

    await screen.findByTestId('composer-recommend-stale')
    expect(onSelectModel).not.toHaveBeenCalled()
  })

  it('marks results stale when the preset they were made for changed', async () => {
    setup()

    await openResults()
    expect(screen.queryByTestId('composer-recommend-stale')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Best quality/ }))

    await screen.findByTestId('composer-recommend-stale')
    expect(screen.queryAllByTestId('composer-recommend-apply')).toHaveLength(0)
  })

  it('marks results stale when the attachments they were made for changed', async () => {
    const { rerenderWithAttachments } = setup()

    await openResults()
    expect(screen.queryByTestId('composer-recommend-stale')).toBeNull()

    rerenderWithAttachments([{ id: 'a1', kind: 'file', label: 'schema.sql' }])

    await screen.findByTestId('composer-recommend-stale')
  })

  it('drops stale results entirely on a profile change rather than showing another profile’s answer', async () => {
    const { rerenderWithProfile } = setup()

    await openResults()
    expect(screen.getAllByTestId('composer-recommend-row')).toHaveLength(2)

    rerenderWithProfile('home')

    await waitFor(() => expect(screen.queryAllByTestId('composer-recommend-row')).toHaveLength(0))
  })

  it('re-requests for the current snapshot when the user refreshes stale results', async () => {
    const { draftRef, notifyDraftChanged, request } = setup()

    await openResults()
    draftRef.current = 'a completely different question'
    notifyDraftChanged()

    await screen.findByTestId('composer-recommend-stale')
    fireEvent.click(screen.getByTestId('composer-recommend-refresh-stale'))

    await waitFor(() => expect(screen.queryByTestId('composer-recommend-stale')).toBeNull())

    const calls = request.mock.calls.filter(([m]) => m === 'model_recommendation.get')

    expect((calls.at(-1)?.[1] as Record<string, unknown>).draft).toBe('a completely different question')
    expect(screen.getAllByTestId('composer-recommend-apply').length).toBeGreaterThan(0)
  })
})

describe('Profile isolation for in-flight recommendations (review round 2, finding 1)', () => {
  const deferred = <T,>() => {
    let resolve!: (value: T) => void

    const promise = new Promise<T>(res => {
      resolve = res
    })

    return { promise, resolve }
  }

  it('does not resume an old-profile click after its pending preset read resolves', async () => {
    const workRead = deferred<{ value: string }>()
    const homeRead = deferred<{ value: string }>()

    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return params?.profile === 'work' ? workRead.promise : homeRead.promise
      }

      return OK_TWO_PROVIDERS
    })

    const { rerenderWithProfile } = setup({ request })

    fireEvent.click(recommendButton())
    rerenderWithProfile('home')
    homeRead.resolve({ value: 'save_codex' })
    await waitFor(() => expect(presetPressed()).toBe('Save Codex'))

    await act(async () => {
      workRead.resolve({ value: 'best_quality' })
      await workRead.promise
      await Promise.resolve()
    })

    expect(
      request.mock.calls.some(
        ([method, params]) => method === 'model_recommendation.get' && params?.profile === 'work'
      )
    ).toBe(false)
    expect(screen.queryAllByTestId('composer-recommend-row')).toHaveLength(0)
  })

  it('does not paint an old-profile recommendation response after switching profiles', async () => {
    const workResult = deferred<typeof OK_TWO_PROVIDERS>()

    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'config.get') {
        return { profile: params?.profile, value: 'balanced' }
      }

      return params?.profile === 'work' ? workResult.promise : OK_TWO_PROVIDERS
    })

    const { rerenderWithProfile } = setup({ request })

    fireEvent.click(recommendButton())
    await waitFor(() =>
      expect(request.mock.calls.some(([method]) => method === 'model_recommendation.get')).toBe(true)
    )
    rerenderWithProfile('home')
    fireEvent.click(recommendButton())
    await waitFor(() => expect(screen.getAllByTestId('composer-recommend-row')).toHaveLength(2))

    await act(async () => {
      workResult.resolve(OK_TWO_PROVIDERS)
      await workResult.promise
      await Promise.resolve()
    })

    expect(screen.getAllByTestId('composer-recommend-row')).toHaveLength(2)
    expect(screen.queryByTestId('composer-recommend-stale')).toBeNull()
  })
})
