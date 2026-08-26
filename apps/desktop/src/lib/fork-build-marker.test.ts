import { describe, expect, it } from 'vitest'

import type { DesktopVersionInfo } from '@/global'

import { resolveForkBuildMarker } from './fork-build-marker'

// The marker exists because a feature branch does not bump the app version:
// a local fork build and the official release report the SAME version string,
// so a user testing their own build cannot tell which binary is running.
//
// The invariant that earns this file: an OFFICIAL release must render nothing.
// A marker that leaks into a shipped build brands every normal user's app as a
// fork, which is worse than not having the marker at all.

const version = (over: Partial<DesktopVersionInfo> = {}): DesktopVersionInfo =>
  ({
    appVersion: '0.17.0',
    electronVersion: '38.0.0',
    hermesRoot: '/Users/x/.hermes',
    nodeVersion: '22.0.0',
    platform: 'darwin',
    ...over
  }) as DesktopVersionInfo

describe('resolveForkBuildMarker stays hidden on official builds', () => {
  it('renders nothing without version info at all', () => {
    expect(resolveForkBuildMarker(null)).toBeNull()
    expect(resolveForkBuildMarker(undefined)).toBeNull()
  })

  it('renders nothing when the build carries no provenance (older backend)', () => {
    // A Desktop talking to a runtime that predates the provenance fields must
    // degrade to "no marker", never to a false positive.
    expect(resolveForkBuildMarker(version())).toBeNull()
  })

  it('renders nothing for a clean CI build', () => {
    expect(
      resolveForkBuildMarker(
        version({ buildBranch: 'main', buildCommit: 'a'.repeat(40), buildDirty: false, buildSource: 'ci' })
      )
    ).toBeNull()
  })

  it('renders nothing for a clean CI build off a release branch', () => {
    // Branch name alone must NOT trigger it: CI legitimately builds release
    // branches, and treating "not main" as unofficial would brand real releases.
    expect(
      resolveForkBuildMarker(
        version({ buildBranch: 'release/0.21', buildCommit: 'b'.repeat(40), buildDirty: false, buildSource: 'ci' })
      )
    ).toBeNull()
  })
})

describe('resolveForkBuildMarker shows on unofficial builds', () => {
  it('shows for a locally built binary', () => {
    const marker = resolveForkBuildMarker(
      version({
        buildBranch: 'feat/desktop-palette-connection-switch',
        buildCommit: '64265912a7201f07d522bdbc649ea90aab1ac680',
        buildDirty: false,
        buildSource: 'local'
      })
    )

    expect(marker).not.toBeNull()
    expect(marker?.label).toContain('FORK')
    expect(marker?.label).toContain('feat/desktop-palette-connection-switch')
    // Short sha, not the full 40 chars — it has to fit in a status bar.
    expect(marker?.label).toContain('64265912')
    expect(marker?.label).not.toContain('64265912a7201f07d522bdbc649ea90aab1ac680')
  })

  it('shows for a dirty tree even when the source claims ci', () => {
    // Uncommitted changes mean the binary does not match any commit, which is
    // exactly when a user most needs to know what they are running.
    const marker = resolveForkBuildMarker(
      version({ buildBranch: 'main', buildCommit: 'c'.repeat(40), buildDirty: true, buildSource: 'ci' })
    )

    expect(marker).not.toBeNull()
    expect(marker?.label).toContain('dirty')
  })

  it('still renders with a missing branch or commit', () => {
    // Provenance can be partial (shallow clone, no git); the marker must not
    // vanish just because one field is absent.
    const marker = resolveForkBuildMarker(version({ buildSource: 'local' }))

    expect(marker).not.toBeNull()
    expect(marker?.label).toContain('FORK')
  })

  it('puts full provenance in the tooltip, not the pill', () => {
    const full = 'd'.repeat(40)

    const marker = resolveForkBuildMarker(
      version({
        buildAt: '2026-08-16T22:17:28.413Z',
        buildBranch: 'my-branch',
        buildCommit: full,
        buildDirty: true,
        buildSource: 'local'
      })
    )

    expect(marker?.title).toContain(full)
    expect(marker?.title).toContain('my-branch')
    expect(marker?.title).toContain('2026-08-16T22:17:28.413Z')
    expect(marker?.title).toContain('0.17.0')
    expect(marker?.title.toLowerCase()).toContain('dirty')
  })

  it('is case-insensitive about the source value', () => {
    expect(resolveForkBuildMarker(version({ buildSource: 'LOCAL' }))).not.toBeNull()
  })
})
