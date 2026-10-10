import { describe, expect, it, vi } from 'vitest'

import { lockDesktopEnv, LOCKED_DESKTOP_ENV } from './env-lockdown'

describe('lockDesktopEnv', () => {
  it('strips every locked override from a packaged app', () => {
    const env: NodeJS.ProcessEnv = { PATH: '/usr/bin', OTHER: 'kept' }

    for (const name of LOCKED_DESKTOP_ENV) {
      env[name] = `/override/${name}`
    }

    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const removed = lockDesktopEnv(env, true)
    warn.mockRestore()

    expect(removed.sort()).toEqual([...LOCKED_DESKTOP_ENV].sort())

    for (const name of LOCKED_DESKTOP_ENV) {
      expect(env[name]).toBeUndefined()
    }

    expect(env).toEqual({ PATH: '/usr/bin', OTHER: 'kept' })
  })

  it('leaves a dev (unpackaged) run untouched', () => {
    const env: NodeJS.ProcessEnv = { HERMES_HOME: '/tmp/h', HERMES_DESKTOP_HERMES_ROOT: '/tmp/wt' }

    expect(lockDesktopEnv(env, false)).toEqual([])
    expect(env).toEqual({ HERMES_HOME: '/tmp/h', HERMES_DESKTOP_HERMES_ROOT: '/tmp/wt' })
  })

  it('covers the overrides that choose the update checkout', () => {
    for (const name of ['HERMES_HOME', 'HERMES_DESKTOP_HERMES_ROOT', 'HERMES_DESKTOP_REMOTE_URL', 'HERMES_DESKTOP_USER_DATA_DIR']) {
      expect(LOCKED_DESKTOP_ENV).toContain(name)
    }
  })
})
