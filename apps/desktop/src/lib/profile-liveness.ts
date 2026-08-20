// Missing-profile detection — one resolver for a policy two layers depend on.
//
// Electron's local spawn guard (`assertLocalProfileCanStart`) rejects a request
// for a profile whose directory is gone, or whose DELETE is still in flight.
// Both rejections are PERMANENT for the renderer: no retry can bring that
// backend back, because the profile no longer exists on this machine.
//
// Two callers need the same answer and used to disagree about it:
//
//   * store/gateway.ts fail-stops a secondary socket's reconnect loop (#88769)
//     instead of retrying a backend that can never come up.
//   * the cross-profile session probe (use-session-actions/utils) walks every
//     known profile looking for a stored session's owner. Its catch treated
//     "not on this profile" (a 404 — a legitimate miss worth probing) and
//     "this profile is gone" (permanent) identically, so every lookup re-probed
//     profiles that had already answered "no longer exists" — the repeating
//     `?profile=<dead>` bursts in the dev console.
//
// Keeping the predicate here means a change to the guard's wording updates
// every consumer at once, rather than leaving one call site silently matching
// nothing.

/**
 * True when an error means the named profile is gone for good, rather than
 * merely not holding the thing we asked for.
 *
 * Matches the two messages `assertLocalProfileCanStart` throws. A plain REST
 * 404 ("Session not found", "Profile 'x' does not exist.") is deliberately NOT
 * matched here: the first is a normal probe miss, and the second comes from a
 * backend that is itself running — callers handle those on their own paths.
 */
export function isMissingProfileError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error ?? '')

  return message.includes('no longer exists') || message.includes('is being deleted')
}

// Profiles this renderer has already seen the spawn guard reject. A dead
// profile stays dead for the life of the window: the only way one comes back is
// creating it again, which re-homes the app (a hard reload) and clears this.
const deadProfiles = new Set<string>()

/** Record a profile the spawn guard has rejected as permanently gone. */
export function markProfileMissing(profile: string): void {
  const key = profile.trim().toLowerCase()

  if (key) {
    deadProfiles.add(key)
  }
}

/** True once `markProfileMissing` has recorded this profile. */
export function isProfileKnownMissing(profile: string): boolean {
  return deadProfiles.has(profile.trim().toLowerCase())
}

/**
 * Note an error against a profile and report whether it was the permanent
 * kind. Lets a caller collapse "classify, remember, branch" into one step.
 */
export function noteProfileError(profile: string, error: unknown): boolean {
  if (!isMissingProfileError(error)) {
    return false
  }

  markProfileMissing(profile)

  return true
}

/** Drop every recorded profile. Exported for tests and re-home paths. */
export function __resetMissingProfiles(): void {
  deadProfiles.clear()
}
