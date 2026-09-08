import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

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
        iconClass: 'text-destructive',
        borderClass: 'border-l-destructive',
        backgroundToken: 'var(--dt-destructive)',
        actionClass: 'bg-destructive'
      },
      {
        kind: 'warning' as const,
        iconClass: 'text-warning',
        borderClass: 'border-l-warning',
        backgroundToken: 'var(--dt-warning)',
        actionClass: 'bg-warning'
      },
      {
        kind: 'success' as const,
        iconClass: 'text-success',
        borderClass: 'border-l-success',
        backgroundToken: 'var(--dt-success)',
        actionClass: 'bg-success'
      }
    ]

    for (const { kind, iconClass, borderClass, backgroundToken, actionClass } of cases) {
      notify({ kind, title: `${kind} title`, message: `${kind} message` })

      const view = render(
        <I18nProvider configClient={null} initialLocale="en">
          <NotificationStack />
        </I18nProvider>
      )

      const alert = screen.getByText(`${kind} title`).closest('[data-slot="alert"]')
      const icon = alert?.querySelector('svg')
      const dismiss = [...(alert?.querySelectorAll('button') ?? [])].find(button => button.textContent === 'Dismiss')

      expect(alert?.className).toContain('border-l-4')
      expect(alert?.className).toContain(borderClass)
      expect(alert?.className).toContain(backgroundToken)
      expect(alert?.className).not.toContain('var(--dt-primary)')
      expect(icon?.classList.contains(iconClass)).toBe(true)
      expect(icon?.classList.contains('text-primary')).toBe(false)
      expect(dismiss?.classList.contains(actionClass)).toBe(true)

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
    expect(dismiss.className).toContain('bg-success')
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
