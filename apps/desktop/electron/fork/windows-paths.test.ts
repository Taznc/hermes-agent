/**
 * Behavior contract for the fork Windows Python probe ladder
 * (fork/windows-paths.ts). All platform behavior enters as injected data
 * (env, fileExists, findOnPath, execFileAsync), so these run on any host —
 * the function takes the platform as dependencies, it never sniffs the OS.
 */

import path from 'node:path'

import { expect, test } from 'vitest'

import { findWindowsSystemPython, type WindowsPythonProbeDeps } from './windows-paths'

function deps(overrides: Partial<WindowsPythonProbeDeps> = {}): WindowsPythonProbeDeps {
  return {
    env: {},
    fileExists: () => false,
    findOnPath: () => null,
    execFileAsync: async () => {
      throw Object.assign(new Error('not found'), { code: 1 })
    },
    probeTimeoutMs: 1_000,
    ...overrides
  }
}

/** A registry exec that answers only for the given hive/version keys. */
function registryExec(answers: Record<string, string>): WindowsPythonProbeDeps['execFileAsync'] {
  return async (_file, args) => {
    // runRegQuery routes through PowerShell: the reg args are embedded in the
    // -Command script string.
    const script = args[args.length - 1]

    for (const [key, installPath] of Object.entries(answers)) {
      if (script.includes(key)) {
        return { stdout: `\n    (Default)    REG_SZ    ${installPath}\n` }
      }
    }

    throw Object.assign(new Error('key not present'), { code: 1 })
  }
}

test('pass 1: a PEP 514 registry hit wins in hive-major order (all HKLM versions before HKCU)', async () => {
  const exec = registryExec({
    'HKCU\\SOFTWARE\\Python\\PythonCore\\3.11': 'C:\\Users\\me\\Python311',
    'HKLM\\SOFTWARE\\Python\\PythonCore\\3.12': 'C:\\Program Files\\Python312'
  })

  const result = await findWindowsSystemPython(
    deps({
      execFileAsync: exec,
      fileExists: p => p.endsWith('python.exe')
    })
  )

  // Candidate order matches the old serial loop exactly: HKLM 3.11, HKLM
  // 3.12, HKLM 3.13, then HKCU 3.11... — so an HKLM 3.12 hit outranks an
  // HKCU 3.11 one. (path.join uses the HOST separator; assert the same way.)
  expect(result).toBe(path.join('C:\\Program Files\\Python312', 'python.exe'))
})

test('pass 1: a registry hit whose python.exe does not exist on disk is not trusted', async () => {
  const exec = registryExec({ 'HKLM\\SOFTWARE\\Python\\PythonCore\\3.11': 'C:\\Ghost\\Python311' })

  const result = await findWindowsSystemPython(deps({ execFileAsync: exec, fileExists: () => false }))

  expect(result).toBeNull()
})

test('pass 2: falls back to standard install locations, system-wide before per-user', async () => {
  const perUser = path.join('C:\\Users\\me\\AppData\\Local', 'Programs', 'Python', 'Python312', 'python.exe')

  const result = await findWindowsSystemPython(
    deps({
      env: { ProgramFiles: 'C:\\Program Files', LOCALAPPDATA: 'C:\\Users\\me\\AppData\\Local' },
      fileExists: p => p === perUser
    })
  )

  expect(result).toBe(perUser)
})

test('pass 3: resolves via py.exe with an explicit version flag, keeping version priority', async () => {
  const calls: string[][] = []

  const result = await findWindowsSystemPython(
    deps({
      findOnPath: command => (command === 'py.exe' ? 'C:\\Windows\\py.exe' : null),
      fileExists: p => p === 'C:\\Python313\\python.exe',
      execFileAsync: async (file, args) => {
        if (file === 'C:\\Windows\\py.exe') {
          calls.push(args)

          if (args[0] === '-3.13') {
            return { stdout: 'C:\\Python313\\python.exe\r\n' }
          }

          throw Object.assign(new Error('no such version'), { code: 103 })
        }

        throw Object.assign(new Error('key not present'), { code: 1 })
      }
    })
  )

  expect(result).toBe('C:\\Python313\\python.exe')
  // Only supported versions are ever requested — 3.14 must never appear.
  expect(calls.every(args => ['-3.11', '-3.12', '-3.13'].includes(args[0]))).toBe(true)
})

test('never falls back to bare python.exe on PATH (Store-stub / 3.14 avoidance)', async () => {
  const probed: string[] = []

  const result = await findWindowsSystemPython(
    deps({
      findOnPath: command => {
        probed.push(command)

        // A bare python.exe IS on PATH — it must not be used.
        return command === 'python.exe' || command === 'python' ? 'C:\\WindowsApps\\python.exe' : null
      }
    })
  )

  expect(result).toBeNull()
  expect(probed).toEqual(['py.exe'])
})
