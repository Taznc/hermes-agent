import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import fs from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { afterEach, beforeEach, expect, it } from 'vitest'

import { isRelevantBackendPythonChange } from './dev-backend-watch'

// Real-loop demonstration (not a unit test of the pure filter — that's
// dev-backend-watch.test.ts). This exercises an ACTUAL fs.watch(recursive)
// against real files, piping real event filenames through the real filter,
// mirroring exactly what watchDevBackendPython() does in main.ts against
// agent/ tui_gateway/ tools/ hermes_cli/. Confirms the acceptance criteria's
// "touch a backend .py file -> detected" / "touch __pycache__/.pyc/log ->
// not detected" loop end to end, with a real filesystem and a real watcher.
let dir: string

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), 'hermes-dev-backend-watch-'))
  mkdirSync(path.join(dir, '__pycache__'), { recursive: true })
})

afterEach(() => {
  rmSync(dir, { recursive: true, force: true })
})

function waitForRelevantEvent(watchDir: string, timeoutMs = 4000): Promise<string[]> {
  return new Promise((resolve, reject) => {
    const seen: string[] = []

    const timer = setTimeout(() => {
      watcher.close()
      resolve(seen)
    }, timeoutMs)

    const watcher = fs.watch(watchDir, { recursive: true }, (_eventType, filename) => {
      if (isRelevantBackendPythonChange(filename ? String(filename) : null)) {
        seen.push(String(filename))
        clearTimeout(timer)
        watcher.close()
        resolve(seen)
      }
    })
  })
}

it('a real .py write under the watched dir is detected as relevant (affordance would appear)', async () => {
  const pending = waitForRelevantEvent(dir)
  // Give the watcher a tick to attach before the write, same as production.
  await new Promise(resolve => setTimeout(resolve, 50))
  writeFileSync(path.join(dir, 'background_review.py'), '# edited\n')

  const seen = await pending
  expect(seen).toContain('background_review.py')
})

it('a real __pycache__ write under the watched dir is never flagged relevant (affordance stays hidden)', async () => {
  const flagged: string[] = []

  const watcher = fs.watch(dir, { recursive: true }, (_eventType, filename) => {
    if (isRelevantBackendPythonChange(filename ? String(filename) : null)) {
      flagged.push(String(filename))
    }
  })

  await new Promise(resolve => setTimeout(resolve, 50))
  writeFileSync(path.join(dir, '__pycache__', 'server.cpython-312.pyc'), Buffer.from([0, 1, 2]))
  writeFileSync(path.join(dir, 'dev-watch.log'), 'log line\n')
  // Give both writes' fs events time to arrive and be filtered.
  await new Promise(resolve => setTimeout(resolve, 300))
  watcher.close()

  expect(flagged).toEqual([])
})
