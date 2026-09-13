import type { DesktopVersionInfo } from '@/global'

/** What the fork/dev-build pill renders, or null when it must stay hidden. */
export interface ForkBuildMarker {
  /** Short label for the pill itself, e.g. "FORK · my-branch · 64265912". */
  label: string
  /** Fuller provenance for the tooltip. */
  title: string
}

/** Short SHA length used in the pill — enough to identify, short enough to fit. */
const SHORT_SHA = 8

/**
 * Decide whether this build is an unofficial one the user should be able to
 * SEE at a glance, and what to say about it.
 *
 * Why this exists: a feature branch does not bump the app version, so a local
 * fork build and the official release both report the same version string in
 * About. A user testing their own build has no way to confirm which binary is
 * actually running.
 *
 * Visibility rule (deliberately conservative — an official release must look
 * EXACTLY as it does today):
 *   - `buildSource === 'local'`  → a locally built binary, not a CI release.
 *   - `buildDirty === true`      → built from a tree with uncommitted changes.
 * Anything else — including a missing stamp, a `ci` build, or an older backend
 * that does not send provenance at all — renders nothing.
 *
 * Note that the branch name alone is NOT a trigger: CI legitimately builds
 * release branches, and treating "not main" as unofficial would brand real
 * releases.
 */
export function resolveForkBuildMarker(version: DesktopVersionInfo | null | undefined): ForkBuildMarker | null {
  if (!version) {
    return null
  }

  const source = (version.buildSource ?? '').trim().toLowerCase()
  const dirty = version.buildDirty === true

  if (source !== 'local' && !dirty) {
    return null
  }

  const branch = (version.buildBranch ?? '').trim()
  const commit = (version.buildCommit ?? '').trim()
  const shortSha = commit ? commit.slice(0, SHORT_SHA) : ''

  const parts = ['FORK']

  if (branch) {
    parts.push(branch)
  }

  if (shortSha) {
    parts.push(shortSha)
  }

  if (dirty) {
    parts.push('dirty')
  }

  const titleLines = [
    'Unofficial local build — not an official Hermes release.',
    branch ? `branch: ${branch}` : null,
    commit ? `commit: ${commit}` : null,
    version.buildAt ? `built: ${version.buildAt}` : null,
    `source: ${version.buildSource || 'unknown'}`,
    dirty ? 'working tree: dirty (uncommitted changes)' : null,
    `version: ${version.appVersion}`
  ].filter(Boolean)

  return { label: parts.join(' · '), title: titleLines.join('\n') }
}
