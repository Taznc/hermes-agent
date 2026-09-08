import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { buttonVariants } from '@/components/ui/button'
import { I18nProvider } from '@/i18n'
import { clearNotifications, notify } from '@/store/notifications'

import { NotificationStack, toastTitleClassName } from './notifications'

const LONG_TITLE = 'This turn is no longer in server history (it may have been compressed away).'
const DETAIL = 'target user message is no longer in session history'

describe('toast titles', () => {
  beforeEach(() => {
    clearNotifications()
  })

  afterEach(() => {
    cleanup()
    clearNotifications()
  })

  it('lets a long error title wrap without creating a nested scrollbar', () => {
    const className = toastTitleClassName()

    expect(className).toMatch(/\bline-clamp-none\b/)
    expect(className).not.toMatch(/\bline-clamp-1\b/)
    expect(className).toMatch(/\bwhitespace-normal\b/)
    expect(className).not.toContain('max-h-[4.5em]')
    expect(className).not.toMatch(/\boverflow-y-auto\b/)
  })

  it('renders the full title and body instead of truncating them', () => {
    notify({ kind: 'error', title: LONG_TITLE, message: DETAIL })

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <NotificationStack />
      </I18nProvider>
    )

    const title = screen.getByText(LONG_TITLE)

    expect(title.textContent).toBe(LONG_TITLE)
    expect(title.getAttribute('title')).toBe(LONG_TITLE)
    expect(title.className).toMatch(/\bline-clamp-none\b/)
    expect(title.className).not.toMatch(/\bline-clamp-1\b/)
    expect(title.className).not.toMatch(/\boverflow-y-auto\b/)
    expect(screen.getByText(DETAIL)).toBeTruthy()
  })

  it('keeps an action notification visible until the user dismisses it', () => {
    notify({
      message: 'A newer build is ready to install.',
      action: { label: 'Review update', onClick: () => undefined }
    })

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <NotificationStack />
      </I18nProvider>
    )

    return new Promise<void>((resolve, reject) => {
      window.setTimeout(() => {
        try {
          expect(screen.getByRole('button', { name: 'Review update' })).toBeTruthy()
          resolve()
        } catch (error) {
          reject(error)
        }
      }, 5_100)
    })
  })

  it('uses distinct semantic treatments for error, warning, and success notifications', () => {
    const cases = [
      {
        kind: 'error' as const,
        iconClass: 'text-destructive-text',
        borderClass: 'border-l-destructive',
        backgroundToken: 'var(--dt-destructive-tint)',
        fillClass: 'bg-destructive'
      },
      {
        kind: 'warning' as const,
        iconClass: 'text-warning-text',
        borderClass: 'border-l-warning',
        backgroundToken: 'var(--dt-warning-tint)',
        fillClass: 'bg-warning'
      },
      {
        kind: 'success' as const,
        iconClass: 'text-success-text',
        borderClass: 'border-l-success',
        backgroundToken: 'var(--dt-success-tint)',
        fillClass: 'bg-success-solid'
      }
    ]

    for (const { kind, iconClass, borderClass, backgroundToken, fillClass } of cases) {
      notify({ kind, title: `${kind} title`, message: `${kind} message` })

      const view = render(
        <I18nProvider configClient={null} initialLocale="en">
          <NotificationStack />
        </I18nProvider>
      )

      const alert = screen.getByText(`${kind} title`).closest('[data-slot="alert"]')
      const icon = alert?.querySelector('svg')
      const title = alert?.querySelector('[data-slot="alert-title"]')
      const dismiss = [...(alert?.querySelectorAll('button') ?? [])].find(button => button.textContent === 'Dismiss')

      expect(alert?.className).toContain('border-l-4')
      expect(alert?.className).toContain(borderClass)
      expect(alert?.className).toContain(backgroundToken)
      expect(alert?.className).not.toContain('var(--dt-primary)')
      expect(icon?.classList.contains(iconClass)).toBe(true)
      expect(icon?.classList.contains('text-primary')).toBe(false)
      expect(dismiss?.classList.contains(fillClass)).toBe(true)
      expect(title).toBeTruthy()

      view.unmount()
      clearNotifications()
    }
  })

  // The round-1 regression: the tinted surface and the text on it were driven by
  // the SAME token, so both moved together and the ratio never opened up (dark
  // error measured 1.70:1 in the real renderer). The contract is that the
  // reading role (title + icon) and the fill role (surface tint, stripe, filled
  // button) are separate tokens — a fix that re-collapses them fails here even
  // though jsdom computes no colors.
  it('drives severity text from a reading token, never from the fill token', () => {
    const fillRoles = ['bg-destructive', 'bg-warning', 'bg-success', 'bg-success-solid']

    for (const kind of ['error', 'warning', 'success'] as const) {
      notify({ kind, title: `${kind} heading`, message: `${kind} body` })

      const view = render(
        <I18nProvider configClient={null} initialLocale="en">
          <NotificationStack />
        </I18nProvider>
      )

      const alert = screen.getByText(`${kind} heading`).closest('[data-slot="alert"]')
      const icon = alert?.querySelector('svg')
      const dismiss = [...(alert?.querySelectorAll('button') ?? [])].find(button => button.textContent === 'Dismiss')

      // Title + icon wear a `-text` role that is NOT the raw fill token.
      const titleRule = alert?.className.match(/\[&_\[data-slot=alert-title\]\]:(\S+)/)?.[1]
      expect(titleRule).toMatch(/-text$/)
      expect([...(icon?.classList ?? [])].some(c => c.endsWith('-text'))).toBe(true)
      expect([...(icon?.classList ?? [])].some(c => fillRoles.includes(c.replace('text-', 'bg-')))).toBe(false)

      // The filled Dismiss wears a FILL role paired with its own -foreground,
      // never the reading token: solid-under-text and text-on-tint are opposite
      // contrast problems and collapsing them is the round-1 defect.
      const dismissClasses = [...(dismiss?.classList ?? [])]
      expect(dismissClasses.some(c => fillRoles.includes(c))).toBe(true)
      expect(dismissClasses.some(c => c.endsWith('-text'))).toBe(false)
      expect(dismissClasses.some(c => c.endsWith('-foreground'))).toBe(true)

      view.unmount()
      clearNotifications()
    }
  })

  // AC1 contract: `Button` owns the severity chrome. The call site selects a
  // named variant and contributes LAYOUT only — no `bg-*`, `text-*`, or hover
  // classes of its own. Asserted as a set difference against the primitive's
  // own output so it stays true if a variant's declarations are retuned, and
  // fails the moment chrome creeps back into the call site's `className`.
  it('lets Button own every severity chrome class on Dismiss, passing only layout at the call site', () => {
    const expected = {
      error: 'severityError',
      warning: 'severityWarning',
      info: 'severityInfo',
      success: 'severitySuccess'
    } as const

    for (const kind of ['error', 'warning', 'info', 'success'] as const) {
      notify({ kind, title: `${kind} chrome`, message: `${kind} body` })

      const view = render(
        <I18nProvider configClient={null} initialLocale="en">
          <NotificationStack />
        </I18nProvider>
      )

      const dismiss = screen.getByRole('button', { name: 'Dismiss' })
      const variant = expected[kind]

      // The primitive is the source: the rendered button reports the variant it
      // was given, and every class it wears comes from that variant's output.
      expect(dismiss.getAttribute('data-variant')).toBe(variant)

      const fromPrimitive = new Set(buttonVariants({ size: 'default', variant }).split(/\s+/).filter(Boolean))
      const fromCallSite = [...dismiss.classList].filter(c => !fromPrimitive.has(c))

      expect(fromCallSite).toEqual(['ml-auto'])
      expect(fromCallSite.some(c => /^(bg-|text-|hover:)/.test(c))).toBe(false)

      // And the chrome really is present — a variant that stopped emitting a
      // fill would otherwise pass the difference check vacuously.
      expect([...dismiss.classList].some(c => c.startsWith('bg-'))).toBe(true)
      expect([...dismiss.classList].some(c => c.startsWith('hover:bg-'))).toBe(true)

      view.unmount()
      clearNotifications()
    }
  })

  it('renders a severity-filled Dismiss action while preserving the corner close button', () => {
    notify({
      kind: 'success',
      title: 'Task completed',
      message: 'A card reached done.',
      action: { label: 'Open card', onClick: () => undefined }
    })

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <NotificationStack />
      </I18nProvider>
    )

    const open = screen.getByRole('button', { name: 'Open card' })
    const dismiss = screen.getByRole('button', { name: 'Dismiss' })
    const cornerClose = screen.getByRole('button', { name: 'Dismiss notification' })
    const actionRow = open.parentElement

    expect(actionRow).toBe(dismiss.parentElement)
    expect(actionRow?.className).toContain('justify-between')
    expect(dismiss.className).toContain('bg-success-solid')
    expect(cornerClose).toBeTruthy()

    fireEvent.click(dismiss)
    expect(screen.queryByText('A card reached done.')).toBeNull()
  })

  it('renders a compact context card for an object notification', () => {
    notify({
      title: 'Task completed',
      message: 'Make Kanban notifications readable',
      contextCard: {
        eyebrow: 'Done · reviewer',
        summary: 'Landed with focused coverage',
        meta: 'Task ID: t101',
        title: 'Make Kanban notifications readable'
      }
    })

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <NotificationStack />
      </I18nProvider>
    )

    const titleNodes = screen.getAllByText('Make Kanban notifications readable')

    const card = titleNodes.find(node => node.closest('[data-notification-context-card="true"]'))?.closest(
      '[data-notification-context-card="true"]'
    )

    expect(card).toBeTruthy()
    expect(card?.textContent).toContain('Done · reviewer')
    expect(card?.textContent).toContain('Landed with focused coverage')
    expect(card?.textContent).toContain('Task ID: t101')

    const message = titleNodes.find(node => node.matches('p[data-notification-message]'))
    expect(message?.className).toMatch(/\bline-clamp-3\b/)
  })
})
