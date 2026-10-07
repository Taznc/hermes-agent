import { describe, expect, it, vi } from 'vitest'

import * as tails from '@/fork/transcript-tail-index'
import type { TranscriptTailState } from '@/store/transcript-tail'

const state: TranscriptTailState = { nextOffset: 1, possiblyTruncated: true }

function legacy([key]: [string, TranscriptTailState]): boolean {
  if (key === 'target') {
    return true
  }

  try {
    const parsed: unknown = JSON.parse(key)

    return Array.isArray(parsed) && parsed.length === 3 && parsed[2] === 'target'
  } catch {
    return false
  }
}

describe('tail source bridge', () => {
  it('returns indexed matches without calling the legacy predicate on supported snapshots', () => {
    const record = { target: state, '["remote","work","target"]': state, other: state }
    const predicate = vi.fn(legacy)

    expect(tails.tailEntriesForSession(record, 'target').matching(predicate)).toEqual([
      ['target', state],
      ['["remote","work","target"]', state]
    ])
    expect(predicate).not.toHaveBeenCalled()
  })

  it('uses the untouched legacy predicate for unsupported record prototypes', () => {
    const record = Object.assign(Object.create({ inherited: state }) as Record<string, TranscriptTailState>, {
      target: state,
      other: state
    })

    const predicate = vi.fn(legacy)

    expect(tails.tailEntriesForSession(record, 'target').matching(predicate)).toEqual([['target', state]])
    expect(predicate).toHaveBeenCalledTimes(2)
  })

  it('does not turn an indexed empty result into a legacy scan', () => {
    const predicate = vi.fn(legacy)

    expect(tails.tailEntriesForSession({ other: state }, 'target').matching(predicate)).toEqual([])
    expect(predicate).not.toHaveBeenCalled()
  })
})
