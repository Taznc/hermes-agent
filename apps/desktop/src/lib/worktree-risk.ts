import type { HermesRepoStatus } from '@/global'

// A worktree's "not yet safe to discard" classification, driven purely by the
// existing repo-status probe. Precedence matters — a lane can be simultaneously
// dirty AND unpushed AND unmerged, and only the worst single dot should show.
export type WorktreeRisk = 'clean' | 'uncommitted' | 'unpushed' | 'unmerged' | 'conflicted' | 'unknown'

/**
 * Classify a worktree's risk from its `HermesRepoStatus`. First match wins:
 * no status (probe hasn't landed / non-repo) → `unknown`; a merge conflict
 * beats everything else (it blocks a plain discard outright); any uncommitted
 * change (staged/unstaged/untracked) → `uncommitted`; commits not yet pushed
 * → `unpushed`; a resolved-but-unmerged branch tip → `unmerged`; an
 * unresolvable merge comparison → `unknown`; otherwise `clean`.
 */
export function worktreeRisk(status: HermesRepoStatus | null | undefined): WorktreeRisk {
  if (status == null) {
    return 'unknown'
  }

  if (status.conflicted > 0) {
    return 'conflicted'
  }

  if (status.changed > 0) {
    return 'uncommitted'
  }

  if (status.unpushed > 0) {
    return 'unpushed'
  }

  if (status.mergedIntoBase === false) {
    return 'unmerged'
  }

  if (status.mergedIntoBase === null) {
    return 'unknown'
  }

  return 'clean'
}
