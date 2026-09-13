import { beforeEach, expect, test, vi } from 'vitest'

import { $notifications, clearNotifications, dismissNotification, isDiskFullErrorMessage, notify, notifyError } from './notifications'

beforeEach(() => {
  clearNotifications()
})

function lastMessage(): string {
  return $notifications.get()[0]?.message ?? ''
}

// Regression for #39365: a gateway auth 401 (bad API_SERVER_KEY) must not be
// summarized as a provider (OpenAI/OpenRouter) API key problem.
test('gateway_auth_failed error is summarized as gateway auth, not provider key', () => {
  notifyError(
    new Error(
      '401 {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}'
    ),
    'Request failed'
  )

  expect(lastMessage()).toContain('API_SERVER_KEY')
  expect(lastMessage()).not.toMatch(/OpenAI/i)
})

test('provider invalid_api_key error still maps to the OpenAI summary', () => {
  notifyError(
    new Error('401 {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}'),
    'Request failed'
  )

  expect(lastMessage()).toMatch(/OpenAI rejected the API key/i)
})

test('disk-full / ENOSPC errors toast a free-space message', () => {
  expect(isDiskFullErrorMessage('OSError: [Errno 28] No space left on device')).toBe(true)
  expect(isDiskFullErrorMessage('sqlite3.OperationalError: database or disk is full')).toBe(true)
  expect(isDiskFullErrorMessage('disk full: session storage could not be written — free some disk space')).toBe(true)
  expect(isDiskFullErrorMessage('This is often a full disk — free some space')).toBe(true)
  expect(isDiskFullErrorMessage('session storage could not be written: permission denied')).toBe(false)
  expect(isDiskFullErrorMessage('network timeout')).toBe(false)

  notifyError(new Error('OSError: [Errno 28] No space left on device: state.db'), 'Prompt failed')

  expect(lastMessage()).toMatch(/Disk full/i)
  expect(lastMessage()).toMatch(/free some space/i)
})

test('session storage write failure is treated as disk-full class', () => {
  notifyError(
    new Error('disk full: session storage could not be written — free some disk space and try again'),
    'Prompt failed'
  )

  expect(lastMessage()).toMatch(/Disk full/i)
})

test('code-skew 503 unwraps to a restart-required summary, not raw IPC JSON', () => {
  notifyError(
    new Error(
      'Error invoking remote method \'hermes:api\': Error: 503: {"detail":"Restart required: This process is running code from 08b4875f4a but the checkout on disk is now 48d2528066."}'
    ),
    'Could not load models'
  )

  expect(lastMessage()).toMatch(/running old code after an update/i)
  expect(lastMessage()).not.toMatch(/hermes:api/)
  expect(lastMessage()).not.toMatch(/systemctl/)
})

// Regression for review t_548d0d33, blocking issue 4: the 4-item stack cap
// must not silently orphan a live pending-undo timer behind a toast the
// user can no longer see or click.
test('a 5th notification within the cap evicts the oldest and fires its onEvict callback', () => {
  const onEvict = vi.fn()

  notify({ id: 'toast-1', kind: 'success', message: 'one', onEvict })
  notify({ id: 'toast-2', kind: 'success', message: 'two' })
  notify({ id: 'toast-3', kind: 'success', message: 'three' })
  notify({ id: 'toast-4', kind: 'success', message: 'four' })

  expect(onEvict).not.toHaveBeenCalled()
  expect($notifications.get().map(n => n.id)).toEqual(['toast-4', 'toast-3', 'toast-2', 'toast-1'])

  // A 5th toast pushes the stack past the cap — toast-1 (oldest, at the
  // back) is evicted and must have its onEvict fired exactly once so any
  // live side effect it represents (e.g. an archive's undo window) commits
  // immediately instead of running invisibly.
  notify({ id: 'toast-5', kind: 'success', message: 'five' })

  expect($notifications.get().map(n => n.id)).toEqual(['toast-5', 'toast-4', 'toast-3', 'toast-2'])
  expect(onEvict).toHaveBeenCalledTimes(1)
})

test('a user dismiss does not fire onEvict (only onDismiss)', () => {
  const onEvict = vi.fn()
  const onDismiss = vi.fn()

  const id = notify({ kind: 'success', message: 'one', onDismiss, onEvict })

  // Regular dismissal path (e.g. the toast's own timeout or close button)
  // never routes through the cap-eviction handler.
  dismissNotification(id)

  expect(onDismiss).toHaveBeenCalledTimes(1)
  expect(onEvict).not.toHaveBeenCalled()
})
