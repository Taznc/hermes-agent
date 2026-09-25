import { describe, expect, it } from 'vitest'

import type { HermesRepoStatus } from '@/global'

import { worktreeRisk } from './worktree-risk'

// Minimal valid status; each test overrides only the fields it exercises.
function status(overrides: Partial<HermesRepoStatus>): HermesRepoStatus {
  return {
    added: 0,
    ahead: 0,
    behind: 0,
    branch: 'feature',
    changed: 0,
    conflicted: 0,
    defaultBranch: 'main',
    detached: false,
    files: [],
    mergedIntoBase: true,
    removed: 0,
    staged: 0,
    unpushed: 0,
    unstaged: 0,
    untracked: 0,
    ...overrides
  }
}

describe('worktreeRisk', () => {
  it('is unknown with no status at all', () => {
    expect(worktreeRisk(null)).toBe('unknown')
    expect(worktreeRisk(undefined)).toBe('unknown')
  })

  it('is clean when nothing is outstanding', () => {
    expect(worktreeRisk(status({}))).toBe('clean')
  })

  it('is uncommitted when there are changed files', () => {
    expect(worktreeRisk(status({ changed: 1 }))).toBe('uncommitted')
  })

  it('is unpushed when commits are not yet pushed', () => {
    expect(worktreeRisk(status({ unpushed: 2 }))).toBe('unpushed')
  })

  it('is unmerged when the branch tip is not contained in the default branch', () => {
    expect(worktreeRisk(status({ mergedIntoBase: false }))).toBe('unmerged')
  })

  it('is unknown when the merge comparison could not be resolved', () => {
    expect(worktreeRisk(status({ mergedIntoBase: null }))).toBe('unknown')
  })

  it('is conflicted when conflicts exist, beating uncommitted', () => {
    expect(worktreeRisk(status({ changed: 3, conflicted: 1 }))).toBe('conflicted')
  })

  it('conflicted beats unpushed and unmerged too', () => {
    expect(worktreeRisk(status({ conflicted: 1, mergedIntoBase: false, unpushed: 5 }))).toBe('conflicted')
  })

  it('uncommitted beats unpushed and unmerged', () => {
    expect(worktreeRisk(status({ changed: 1, mergedIntoBase: false, unpushed: 5 }))).toBe('uncommitted')
  })

  it('unpushed beats unmerged', () => {
    expect(worktreeRisk(status({ mergedIntoBase: false, unpushed: 1 }))).toBe('unpushed')
  })
})
