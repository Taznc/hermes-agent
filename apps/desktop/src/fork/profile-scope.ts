// Fork-owned backend-scope helpers for store/profile.ts.
//
// `store/profile.ts` is upstream's authoritative profile-routing state and the
// extraction plan marks it IRREDUCIBLE: the atoms, the activation ladder, and
// the ensureGateway* doors must stay where upstream edits them. Only the
// fork-specific scope KEY and the fork-specific cache purge move here.
//
// What the fork changed in that file, and why these two pieces are the
// separable part:
//
// Upstream keys its "did the routed backend change?" guard on the profile NAME.
// The fork keys it on the (connection, profile) PAIR, because the same profile
// name routinely exists on several registered sources — local `default` and
// remote `default` are different machines with different sessions — so a
// name-only comparison sees NO CHANGE when you switch machines and leaves the
// previous box's sessions, settings and cron on screen. That is the "I switched
// but I still have all my same conversations" symptom.
//
// The key format and the extra purge it enables are self-contained and
// testable without nanostores; the subscription that applies them is not, and
// stays inline behind the anchor.

/** The local pool (the app-managed runtime on this device) has no connection id. */
export const FORK_LOCAL_SCOPE = 'local'

const SCOPE_SEPARATOR = '::'

/**
 * Identity of the backend the live gateway is routed to.
 *
 * `$activeGatewayProfile` alone cannot answer "which machine am I on", so the
 * scope is the pair. `profileKey` is passed already normalized by the caller,
 * which owns profile-key normalization.
 */
export function forkBackendScopeKey(connectionId: null | string, profileKey: string): string {
  return `${connectionId ?? FORK_LOCAL_SCOPE}${SCOPE_SEPARATOR}${profileKey}`
}

/** The connection half of a scope key produced by {@link forkBackendScopeKey}. */
export function forkScopeConnection(scope: string): string {
  return scope.split(SCOPE_SEPARATOR)[0] ?? FORK_LOCAL_SCOPE
}

/**
 * Whether a scope change crossed a BACKEND boundary rather than just moving
 * between profiles on the same one.
 *
 * Only a backend change needs the session-list wipe: switching profiles within
 * one machine is upstream's existing invalidation path, and wiping there would
 * throw away rows the user is still looking at.
 */
export function forkScopeChangedBackend(previous: string, next: string): boolean {
  return forkScopeConnection(previous) !== forkScopeConnection(next)
}

/**
 * Drops the previous backend's session rows after a backend switch.
 *
 * Sessions live in nanostores, NOT React Query: refreshSessions MERGES into the
 * existing list, so query invalidation alone cannot evict the previous
 * backend's rows — they must be wiped explicitly, exactly as the
 * connection/mode apply path does. Without this the sidebar keeps painting the
 * machine you just left.
 *
 * The import is deferred, and must stay deferred: `store/gateway-switch` reaches
 * `store/session` and `store/layout`, which import back into `store/profile`. A
 * static import closes that cycle and strands `$showAllProfiles` in its temporal
 * dead zone at module-eval time (blank window). Resolving it at call time keeps
 * the module graph acyclic at import.
 */
export function wipeForkScopedSessionLists(): void {
  void import('@/store/gateway-switch').then(m => {
    m.wipeSessionListsForGatewaySwitch()
  })
}
