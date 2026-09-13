import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveSshBinary } from './ssh-binary'

test('non-Windows returns bare "ssh" without touching the filesystem', () => {
  const fileExists = () => {
    throw new Error('should not be called off Windows')
  }

  assert.equal(resolveSshBinary({ isWindows: false, env: {}, fileExists }), 'ssh')
})

test('prefers the built-in System32 OpenSSH client when it exists', () => {
  const fileExists = (p: string) => p === 'C:\\Windows\\System32\\OpenSSH\\ssh.exe'

  const result = resolveSshBinary({
    isWindows: true,
    env: { SystemRoot: 'C:\\Windows' },
    fileExists,
    findOnPath: () => null
  })

  assert.equal(result, 'C:\\Windows\\System32\\OpenSSH\\ssh.exe')
})

test('falls back to PATH when System32 OpenSSH is not installed (LTSC/IoT)', () => {
  const fileExists = () => false

  const result = resolveSshBinary({
    isWindows: true,
    env: { SystemRoot: 'C:\\Windows' },
    fileExists,
    findOnPath: command => (command === 'ssh.exe' ? 'D:\\Tools\\ssh.exe' : null)
  })

  assert.equal(result, 'D:\\Tools\\ssh.exe')
})

test('falls back to Git for Windows usr\\bin\\ssh.exe next to a top-level bin\\bash.exe', () => {
  const fileExists = (p: string) => p === 'C:\\Program Files\\Git\\usr\\bin\\ssh.exe'

  const result = resolveSshBinary({
    isWindows: true,
    env: { SystemRoot: 'C:\\Windows' },
    fileExists,
    findOnPath: () => null,
    gitBashPath: 'C:\\Program Files\\Git\\bin\\bash.exe'
  })

  assert.equal(result, 'C:\\Program Files\\Git\\usr\\bin\\ssh.exe')
})

test('falls back to Git for Windows usr\\bin\\ssh.exe next to the MSYS2-layout bash.exe', () => {
  const fileExists = (p: string) => p === 'C:\\Program Files\\Git\\usr\\bin\\ssh.exe'

  const result = resolveSshBinary({
    isWindows: true,
    env: { SystemRoot: 'C:\\Windows' },
    fileExists,
    findOnPath: () => null,
    gitBashPath: 'C:\\Program Files\\Git\\usr\\bin\\bash.exe'
  })

  assert.equal(result, 'C:\\Program Files\\Git\\usr\\bin\\ssh.exe')
})

test('returns null when nothing resolves, so callers can show a clear error', () => {
  const result = resolveSshBinary({
    isWindows: true,
    env: { SystemRoot: 'C:\\Windows' },
    fileExists: () => false,
    findOnPath: () => null,
    gitBashPath: null
  })

  assert.equal(result, null)
})
