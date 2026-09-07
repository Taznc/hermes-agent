import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { stubThreadEnvironment, ThreadRuntime } from '@/components/assistant-ui/test-utils'
import { I18nProvider } from '@/i18n'
import { mainComposerScope } from '@/store/composer'

import type { ComposerRecommendContext } from './types'

import { ChatBar } from './index'

// The seam that lets the fork's Recommend surface reach the composer without
// forking the composer itself. What matters is not that the prop exists — it
// is that the composer MOUNTS it, hands it the LIVE draft (read from the DOM,
// the same source Enter uses), and hands it this composer's attachment chips.
//
// A unit test of the fork component alone cannot prove any of that: it would
// pass just as happily with the seam unwired, which is precisely the failure
// mode ("the component is perfect and never renders").

const noop = () => {}

function renderChatBar(recommendRender?: (ctx: ComposerRecommendContext) => React.ReactNode, disabled = false) {
  return render(
    <MemoryRouter>
      <I18nProvider configClient={null} initialLocale="en">
        <ThreadRuntime messages={[]}>
          <ChatBar
            busy={false}
            disabled={disabled}
            onCancel={noop}
            onSubmit={() => true}
            state={{
              model: { canSwitch: true, model: 'm', provider: 'p', recommendRender },
              tools: { enabled: false, label: '' },
              voice: { active: false, enabled: false }
            }}
          />
        </ThreadRuntime>
      </I18nProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  stubThreadEnvironment()
})

afterEach(() => {
  cleanup()
  mainComposerScope.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('composer recommendation seam', () => {
  it('renders nothing extra when the owner supplies no seam (older/unwired owner)', async () => {
    renderChatBar(undefined)

    await waitFor(() => expect(screen.getByRole('textbox')).toBeTruthy())
    expect(screen.queryByTestId('seam-probe')).toBeNull()
  })

  it('mounts the surface the owner supplies, inside the composer', async () => {
    renderChatBar(() => <div data-testid="seam-probe">probe</div>)

    const probe = await screen.findByTestId('seam-probe')

    expect(probe.closest('[data-slot="composer-surface"]')).toBeTruthy()
  })

  it('hands the seam a getter that reads the LIVE editor text, not a stale render value', async () => {
    let readDraft: (() => string) | null = null

    renderChatBar(ctx => {
      readDraft = ctx.getDraft

      return <div data-testid="seam-probe" />
    })

    await screen.findByTestId('seam-probe')

    const editor = screen.getByRole('textbox')

    expect(readDraft!()).toBe('')

    // Mutate the DOM WITHOUT an input event — exactly the fast-typing/IME race
    // that makes React composer state lag the DOM by a render. A captured
    // string would still read '' here.
    editor.textContent = 'typed just now'
    expect(readDraft!()).toBe('typed just now')

    // And it stays a read: the seam has no way to write the draft back.
    fireEvent.input(editor)
    await waitFor(() => expect(editor.textContent).toBe('typed just now'))
  })

  it('hands the seam this composer scope’s attachment chips', async () => {
    const seen: ComposerRecommendContext[] = []

    renderChatBar(ctx => {
      seen.push(ctx)

      return <div data-testid="seam-probe" />
    })

    await screen.findByTestId('seam-probe')
    expect(seen.at(-1)?.attachments).toEqual([])

    mainComposerScope.add({ id: 'a1', kind: 'file', label: 'brief.pdf' })

    await waitFor(() => expect(seen.at(-1)?.attachments.map(a => a.label)).toEqual(['brief.pdf']))
  })

  it('hands the seam a draft subscription that fires on real edits', async () => {
    let ctx: ComposerRecommendContext | null = null
    const changes: string[] = []

    renderChatBar(received => {
      ctx = received

      return <div data-testid="seam-probe" />
    })

    await screen.findByTestId('seam-probe')

    // The surface must be able to notice an edit without the composer
    // re-rendering it — the composer deliberately keeps typing out of React,
    // so a render-time check alone can be arbitrarily stale.
    const unsubscribe = ctx!.subscribeDraft?.(() => changes.push(ctx!.getDraft()))

    expect(unsubscribe).toBeTypeOf('function')

    const editor = screen.getByRole('textbox')

    editor.textContent = 'a draft only this test writes'
    fireEvent.input(editor)

    await waitFor(() => expect(changes.at(-1)).toBe('a draft only this test writes'))

    unsubscribe?.()
    editor.textContent = 'and more'
    fireEvent.input(editor)

    await waitFor(() => expect(screen.getByRole('textbox').textContent).toBe('and more'))
    expect(changes.at(-1)).toBe('a draft only this test writes')
  })

  it('propagates the composer disabled state so the control cannot be used offline', async () => {
    const seen: ComposerRecommendContext[] = []

    renderChatBar(ctx => {
      seen.push(ctx)

      return <div data-testid="seam-probe" />
    }, true)

    await screen.findByTestId('seam-probe')
    expect(seen.at(-1)?.disabled).toBe(true)
  })
})
