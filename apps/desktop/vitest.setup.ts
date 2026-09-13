import { configure } from '@testing-library/react'
import { afterEach } from 'vitest'

// Node 26 defines its own `localStorage` accessor on the global object, which
// returns `undefined` unless the process was started with --localstorage-file
// (it warns: "localStorage is not available because --localstorage-file was
// not provided"). In the jsdom environment `globalThis` IS the window, so that
// accessor shadows jsdom's Storage and every `localStorage.getItem(...)` in a
// test throws "Cannot read properties of undefined". Install a real in-memory
// Storage when the global resolves to nothing, before any test module reads it.
if (typeof (globalThis as any).localStorage === 'undefined') {
  const store = new Map<string, string>()
  const storage: Storage = {
    get length() {
      return store.size
    },
    key: (i: number) => [...store.keys()][i] ?? null,
    getItem: (k: string) => store.get(String(k)) ?? null,
    setItem: (k: string, v: string) => void store.set(String(k), String(v)),
    removeItem: (k: string) => void store.delete(String(k)),
    clear: () => store.clear(),
  }
  for (const target of [globalThis, (globalThis as any).window].filter(Boolean)) {
    Object.defineProperty(target, 'localStorage', {
      value: storage,
      configurable: true,
      writable: true,
    })
  }
}

// React 19 + Testing Library 16: opt into the act environment so render(),
// fireEvent(), and findBy* queries automatically flush state updates without
// spurious "not wrapped in act(...)" warnings.
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true

// findBy*/waitFor default to a 1000ms deadline — too tight for async-heavy
// panels (radix menus, refetch chains) when the full suite runs under xdist
// CPU contention in CI. Success still resolves the instant the node appears;
// the wider deadline only absorbs a starved runner, killing timing flakes.
// 5s proved insufficient on saturated runners (2026-08-31: gateway-settings,
// messaging, session-unread-tile, toolset-config-panel each tripped a
// waitFor(mock-called) deadline on runs whose only common factor was load —
// including a plugins-only commit on main). 12s mirrors the same reasoning
// as the 15s testTimeout above it while still finishing below it, so a
// genuinely hung await still surfaces as this assertion, not a test timeout.
configure({ asyncUtilTimeout: 12_000 })

// A component's poll loop (store/local-runtime-jobs.ts's watchLocalRuntimeJobs)
// is deliberately designed to outlive its mounting component — that's what lets
// a download survive the settings pane unmounting. In this suite the loop must
// still die with the FILE that started it: vitest's pool workers reuse one
// process across many test files, and a timer left running past its owning
// file's teardown fires while an unrelated file executes, throwing
// `ReferenceError: window is not defined` on whatever test happens to be
// running (#t_fc026713 — moved between files/worker counts, which was the
// signature that the fault wasn't in the file the error surfaced in). One
// global afterEach here — rather than a per-file afterEach every caller has to
// remember — guarantees no run's poll survives past its test.
afterEach(async () => {
  const { resetLocalRuntimeJobsForTests } = await import('@/store/local-runtime-jobs')

  resetLocalRuntimeJobsForTests()
})
