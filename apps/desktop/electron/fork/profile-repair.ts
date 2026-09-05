// Fork-owned: self-healing resolution of the stored desktop profile
// preference (active-profile.json).
//
// A stored name must also still EXIST on this machine. The preference
// outlives the profile it names: deleting a profile elsewhere, syncing this
// file between machines, or restoring a backup all leave a name here with no
// directory behind it. Because every profile-scoped consumer funnels through
// the readActiveDesktopProfile anchor — primaryProfileKey(), the
// `hermes:profile:get` IPC the renderer adopts at boot, and the backend
// launch path — an unvalidated name routes EVERY profile-scoped REST call
// (config, env, model info, schema, sessions) at a profile the backend will
// never have. Each one 404s ("Profile 'x' does not exist."), nothing
// self-heals, and the app retries forever.
//
// Format validation alone cannot catch that: `claudeprimary` is a perfectly
// well-formed name for a profile that isn't here. Validate existence at the
// same boundary the local spawn guard uses (assertLocalProfileCanStart),
// then self-heal by clearing the dead preference so the next read is clean
// and the app falls back to the default profile instead of looping.

import { resolveStoredDesktopProfile } from '../profile-delete-routing'

export interface StoredProfileRepairDeps {
  /** Raw stored value out of active-profile.json ('' when absent/malformed). */
  readStoredProfile(): string
  isValidProfileName(name: string): boolean
  profileDirectoryExists(name: string): boolean
  /** Clear the dead preference (writeActiveDesktopProfile(null)). */
  clearStoredProfile(): void
  rememberLog(message: string): void
}

export function repairStoredProfile(deps: StoredProfileRepairDeps): null | string {
  const stored = deps.readStoredProfile()

  const resolved = resolveStoredDesktopProfile(stored, deps.isValidProfileName, deps.profileDirectoryExists)

  // A well-formed name that resolved to nothing means the profile is gone (a
  // malformed/absent file yields an empty `stored` and is not worth logging).
  if (!resolved && stored.trim()) {
    deps.rememberLog(
      `Stored desktop profile "${stored.trim()}" no longer exists — falling back to the default profile`
    )

    try {
      deps.clearStoredProfile()
    } catch {
      // Best-effort self-heal: a read-only userData dir still falls back.
    }
  }

  return resolved
}
