import { LOCAL_CONNECTION_ID } from '@hermes/shared'
import { atom, batch, computed } from 'nanostores'

import type { HermesConnection } from '@/global'
import { getProfiles, hermesApi, setApiRequestProfile, STARTUP_REQUEST_TIMEOUT_MS } from '@/hermes'
import { invalidateProfileScopedQueries } from '@/lib/query-client'
import {
  arraysEqual,
  persistBoolean,
  persistStringArray,
  persistStringRecord,
  storedBoolean,
  storedStringArray,
  storedStringRecord
} from '@/lib/storage'
import { withTimeout } from '@/lib/with-timeout'
import { invalidateCronModelImpactScopeState } from '@/store/cron-model-impact-scope'
import {
  $gateway,
  activeGatewayConnectionId,
  ensureGatewayForAgent,
  ensureGatewayForProfile,
  openGatewayForAgent,
  openGatewayForProfile,
  openSecondaryCount
} from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { $poolLimits } from '@/store/pool-limits'
import { notifyRemoteOverrideAuthFailure } from '@/store/profile-remote-override'
import { clearComposerSelectionOwner, setComposerSelectionOwner, setConnection } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'
import { resetStarmapGraph } from '@/store/starmap'
import type { ProfileInfo } from '@/types/hermes'

// Canonical key for a profile: trimmed, empty → "default". Used everywhere we
// compare a session's owning profile against the live gateway's profile.
export function normalizeProfileKey(name: string | null | undefined): string {
  const value = (name ?? '').trim()

  return value || 'default'
}

// Presentation-only label: the display_name from profile.yaml when set (e.g. a
// renamed default profile), else the canonical name. Never used for
// comparison or routing — canonical `name` remains the identity everywhere.
export function profileLabel(profile: Pick<ProfileInfo, 'display_name' | 'name'>): string {
  return (profile.display_name ?? '').trim() || profile.name
}

// The profile the running local backend is actually scoped to (mirrors
// /api/profiles/active `current`). "default" is the root ~/.hermes. This is the
// display source of truth for the statusbar pill; the desktop's *stored*
// preference (which may be unset) lives in the Electron main process.
export const $activeProfile = atom<string>('default')

// Cached profile list for the picker. Refreshed lazily; the dropdown also
// re-fetches on open so a profile created elsewhere shows up.
export const $profiles = atom<ProfileInfo[]>([])

export function setActiveProfile(name: string): void {
  $activeProfile.set(name || 'default')
}

// ── Stale-fetch invalidation across backend switches ───────────────────────
// $profiles mirrors the ACTIVE backend's /api/profiles. A connection/mode
// apply (the soft re-home) or a profile/agent activation changes which backend
// that is while a fetch may still be in flight — and a late response from the
// PREVIOUS backend must not clobber the list the new backend just served.
// That was #85731's disappearing rail: applying a different remote/Cloud
// connection let the old (often dying, profile-less) backend's response land
// last, collapsing $profiles and hiding the rail. Bumping the epoch strands
// every in-flight fetch: the response still resolves for its caller, but it
// no longer writes the shared cache ("guard against the past").
let profileListEpoch = 0

export function invalidateProfileListFetches(): void {
  profileListEpoch += 1
  // Detach the single-flight slot too: a caller arriving AFTER a backend
  // switch must start a fresh fetch against the new backend, not ride the
  // previous backend's in-flight retry chain.
  refreshInFlight = null
}

// Single-flight guard: on gateway open both useBackgroundSync and the
// activeGatewayProfile-change effect call refreshActiveProfile() at once, and
// the Manage Profiles panel can join mid-flight. Dedupe so concurrent callers
// share one retry chain instead of stampeding /api/profiles (#70679).
let refreshInFlight: Promise<ProfileInfo[]> | null = null

export function refreshProfiles(): Promise<ProfileInfo[]> {
  if (refreshInFlight) {
    return refreshInFlight
  }

  const flight = (async () => {
    const epoch = profileListEpoch
    const MAX_RETRIES = 2

    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const { profiles } = await getProfiles()

        if (epoch === profileListEpoch) {
          $profiles.set(profiles)
        }

        return profiles
      } catch (error) {
        if (attempt === MAX_RETRIES || epoch !== profileListEpoch) {
          // Surface the failure so it's visible in the console — the prior
          // silent catch in refreshActiveProfile() hid global-remote timing
          // races (#70679). A stranded epoch stops retrying against the past.
          console.error(`[profiles] refreshProfiles failed after ${attempt + 1} attempt(s):`, error)

          throw error
        }

        // Back off before retrying: 500ms, then 1000ms. Gives the remote proxy
        // a window to finish routing after WebSocket-ready but pre-HTTP-proxy
        // states (global remote mode, #70679).
        await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)))
      }
    }

    // Unreachable — satisfies TypeScript.
    return []
  })().finally(() => {
    if (refreshInFlight === flight) {
      refreshInFlight = null
    }
  })

  refreshInFlight = flight

  return flight
}

// ── Rail order ─────────────────────────────────────────────────────────────
// User-defined order for the named (non-default) profile squares in the rail.
// Names absent from the list fall back to alphabetical, appended at the tail —
// so a freshly created profile lands at the end until the user drags it.
const PROFILE_ORDER_STORAGE_KEY = 'hermes.desktop.profileOrder'

export const $profileOrder = atom<string[]>(storedStringArray(PROFILE_ORDER_STORAGE_KEY))

$profileOrder.subscribe(value => persistStringArray(PROFILE_ORDER_STORAGE_KEY, [...value]))

export function setProfileOrder(names: string[]): void {
  if (!arraysEqual($profileOrder.get(), names)) {
    $profileOrder.set(names)
  }
}

// Sort items by the stored order; unordered names alphabetise at the tail.
export function sortByProfileOrder<T extends { name: string }>(items: T[], order: string[]): T[] {
  const rank = new Map(order.map((name, index) => [name, index]))

  return [...items].sort((a, b) => {
    const ra = rank.get(a.name)
    const rb = rank.get(b.name)

    if (ra != null && rb != null) {
      return ra - rb
    }

    return ra != null ? -1 : rb != null ? 1 : a.name.localeCompare(b.name)
  })
}

// ── Rail colors ────────────────────────────────────────────────────────────
// Optional per-profile color override (long-press a rail square to pick). Absent
// names fall back to the deterministic hue from profileColor(); a local-only
// cosmetic preference, so single-profile users never touch it.
const PROFILE_COLORS_STORAGE_KEY = 'hermes.desktop.profileColors'

export const $profileColors = atom<Record<string, string>>(storedStringRecord(PROFILE_COLORS_STORAGE_KEY))

$profileColors.subscribe(value => persistStringRecord(PROFILE_COLORS_STORAGE_KEY, value))

// Set (or, with null, clear) a profile's color override.
export function setProfileColor(name: string, color: null | string): void {
  const key = normalizeProfileKey(name)
  const next = { ...$profileColors.get() }

  if (color) {
    next[key] = color
  } else {
    delete next[key]
  }

  $profileColors.set(next)
}

interface ActiveProfileResponse {
  active: string
  current: string
}

// Pull the running backend's current profile + the available profile list.
// Best-effort: failures (backend not up yet) leave the prior values intact.
export async function refreshActiveProfile(): Promise<void> {
  const epoch = profileListEpoch

  // Both calls are scoped to the live backend by hermesApi's ambient
  // connection tag: on a registry connection these enumerate THAT machine's
  // profiles, not the local pool's (#85731).
  try {
    const res = await hermesApi<ActiveProfileResponse>({
      path: '/api/profiles/active',
      timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
    })

    // Same stale-response guard as refreshProfiles: a backend switch mid-fetch
    // means this answer describes the PREVIOUS backend.
    if (epoch === profileListEpoch) {
      setActiveProfile(res.current || 'default')
    }
  } catch {
    // Backend may not be ready; keep the last known value.
  }

  try {
    await refreshProfiles()
  } catch {
    // Leave the cached list in place.
  }
}

// Persist the choice and relaunch the backend under the new HERMES_HOME. The
// main process reloads the window, so this normally never returns to the caller
// (the renderer is torn down). We optimistically reflect the selection first so
// the pill updates instantly if the reload is delayed.
export async function switchProfile(name: string): Promise<void> {
  if (!name || name === $activeProfile.get()) {
    return
  }

  setActiveProfile(name)
  await window.hermesDesktop.profile.set(name)
}

// ── Swap-minimal gateway routing ──────────────────────────────────────────
// One live gateway at a time. When the user opens/sends a session whose profile
// differs from the gateway's current profile, we lazily reconnect the single
// gateway to that profile's backend (spawned on demand by the Electron pool).
// A single-profile user never triggers a swap, so their path is unchanged.

// The profile the live gateway WebSocket is currently connected to. Initialized
// to the primary (window) backend's profile on boot. The gateway registry
// mirrors its own route into this atom via the onActiveRouteChanged callback
// (wired in use-gateway-boot's configureGatewayRegistry), so registry-internal
// eviction fallbacks (idle reap, connection removal, profile delete) can never
// leave this naming a profile the active socket no longer serves (#89206).
export const $activeGatewayProfile = atom<string>('default')

// The REGISTRY CONNECTION the live gateway is dialed through, or null for the
// local pool (the app-managed runtime on this device). $activeGatewayProfile
// alone cannot answer "which machine am I on": the same profile name commonly
// exists on several registered sources, so `default` locally and `default` on a
// remote box are indistinguishable by profile key. Surfaces that switch or
// report the active agent need the pair.
export const $activeGatewayConnection = atom<null | string>(null)

// Profile for the NEXT new chat (chosen via the new-chat picker). null = primary
// / default, so single-profile users are unaffected.
export const $newChatProfile = atom<string | null>(null)
/** The draft's exact owner — the same shape every session-scoped surface
 *  routes by (store/session-request-router SessionOwnerRoute). */
export type AgentProfileRoute = SessionOwnerRoute

// A draft remembers the source it was created for. The active gateway may
// change before the first Send; the draft's owner must not change with it.
export const $newChatRoute = atom<AgentProfileRoute | null>(null)

// The registry source captured TOGETHER with a $newChatProfile intent
// (selectProfile / newSessionInProfile / a connection switch / `/profile`).
// A profile is not a machine-global name: "omar" picked while the remote
// registry source `homelab` is active means homelab::omar — the exact registry
// entry whose WebSocket will mint the runtime. Without this the profile-rail
// path (which deliberately clears $newChatRoute) reduced the owner to the bare
// string "omar", and every follow-up RPC dialed requestGatewayForProfile
// ("omar") — a DIFFERENT socket than the one that created the session —
// and 4001'd "session not found" (#94071). null = the intent dials the legacy
// profile-only path (a v1 primary with no registry identity, or a named
// profile pick on the explicit `local` source — see profilePickConnectionId).
export const $newChatConnectionId = atom<null | string>(null)

/** Capture the registry source a new-chat profile intent lands on — by
 *  default the active one; callers that dial a different door (a profile
 *  pick, see profilePickConnectionId) pass the source that door uses. */
export function captureNewChatSource(connectionId: null | string = activeGatewayConnectionId()): void {
  $newChatConnectionId.set(connectionId)
}

/**
 * The registry source a PROFILE PICK dials, mirroring activateOnCurrentSource:
 * a live remote registry source keeps its connection id. Named picks on the
 * explicit `local` source (and the window primary) take the legacy
 * profile-only path (null) so the main process can resolve a per-profile
 * remote override before falling back to a local backend (#94166).
 *
 * Default on that same `local` source is different: it is also the window
 * primary's profile key. The legacy door would activate the remote primary on
 * a VPS-primary desktop, and Bots would show the VPS as Current Gateway.
 * Keep Default on `local` so This-device home stays on This device.
 */
function profilePickConnectionId(profile?: string): null | string {
  const connectionId = activeGatewayConnectionId()

  if (connectionId && connectionId !== LOCAL_CONNECTION_ID) {
    return connectionId
  }

  if (connectionId === LOCAL_CONNECTION_ID) {
    const key = normalizeProfileKey(profile ?? $newChatProfile.get())

    return key === 'default' ? LOCAL_CONNECTION_ID : null
  }

  return null
}

/**
 * The EXACT owner route the next new chat is created on, or null for the
 * legacy ambient path. An explicit agent route ($newChatRoute) wins; else,
 * whenever a registry source is live, the (connection, profile) pair is
 * derived from the source captured with the profile intent — falling back to
 * the source a profile pick would dial (an uncaptured intent), or to the
 * active source when there is no profile intent at all — so session.create,
 * the owner hint, the optimistic row and every later session-scoped RPC name
 * the same registry entry. A legacy profile-only activation yields null.
 */
export function resolveNewChatOwnerRoute(forProfile?: string): AgentProfileRoute | null {
  const explicit = $newChatRoute.get()

  if (explicit && (!forProfile || normalizeProfileKey(explicit.profile) === normalizeProfileKey(forProfile))) {
    return explicit
  }

  const intentProfile = forProfile ? normalizeProfileKey(forProfile) : $newChatProfile.get()

  const connectionId = (
    (intentProfile
      ? ($newChatConnectionId.get() ?? profilePickConnectionId(intentProfile))
      : activeGatewayConnectionId()) ?? ''
  ).trim()

  if (!connectionId) {
    return null
  }

  return {
    connectionId,
    profile: normalizeProfileKey(intentProfile || $activeGatewayProfile.get())
  }
}

// Bumped whenever the open session should be dropped for a fresh new-session
// draft: a profile switch/create (below), or deleting the project that owns the
// currently-open session (store/projects). The chat controller subscribes and
// resets to the intro draft, so we never strand the user in an orphaned view.
export const $freshSessionRequest = atom(0)

export function requestFreshSession(): void {
  $freshSessionRequest.set($freshSessionRequest.get() + 1)
}

// Route profile-scoped REST settings (config/env/skills/tools/model/…) to the
// profile the live gateway is currently on, and drop cached state from the
// previous backend so pages refetch against the right one.
//
// Keyed on the (connection, profile) PAIR, not the profile name. The same
// profile name routinely exists on several registered sources — local
// `default` and remote `default` are different machines with different
// sessions — so a name-only comparison sees NO CHANGE when you switch
// machines and leaves the previous box's sessions, settings and cron on
// screen. That is the "I switched but I still have all my same
// conversations" symptom.
//
// Fires once immediately (no real change → no invalidation), so single-source
// users are unaffected.
let _lastRoutedScope: string | null = null

const $activeBackendScope = computed(
  [$activeGatewayConnection, $activeGatewayProfile],
  (connectionId, profile) => `${connectionId ?? 'local'}::${normalizeProfileKey(profile)}`
)

$activeBackendScope.subscribe(scope => {
  const profileKey = normalizeProfileKey($activeGatewayProfile.get())
  setApiRequestProfile(profileKey)

  if (_lastRoutedScope !== null && _lastRoutedScope !== scope) {
    invalidateCronModelImpactScopeState()
    // Profile-scoped settings + the unified session list are now stale.
    // Narrowed so account/marketplace/onboarding caches don't refetch on
    // every switch.
    invalidateProfileScopedQueries()
    resetStarmapGraph()
    // /api/profiles now routes to a different backend: strand any in-flight
    // profile-list fetch so the previous backend's late answer can't clobber
    // the rail (the #85731 class — same guard as the connection-apply wipe).
    invalidateProfileListFetches()

    // Sessions live in nanostores, NOT React Query: refreshSessions merges
    // into the existing list, so query invalidation alone cannot evict the
    // previous backend's rows — they must be wiped explicitly, exactly as the
    // connection/mode apply path does. Without this the sidebar keeps
    // painting the machine you just left.
    //
    // Imported lazily: store/gateway-switch reaches store/session and
    // store/layout, which import back into this module. A static import here
    // closes that cycle and strands `$showAllProfiles` in its temporal dead
    // zone at module-eval time (blank window). Deferring to call time keeps
    // the graph acyclic at import.
    if (_lastRoutedScope.split('::')[0] !== scope.split('::')[0]) {
      void import('@/store/gateway-switch').then(m => {
        m.wipeSessionListsForGatewaySwitch()
      })
    }
  }

  _lastRoutedScope = scope
})

// Target profile while a gateway swap is mid-flight (spawning/reconnecting that
// profile's backend), else null. Drives the chat's "waking up <profile>" loader
// so a lazy spawn doesn't read as a hang. Single-profile users never swap.
export const $gatewaySwapTarget = atom<string | null>(null)

// Profile whose wake resolved PAINT-FIRST while the active-profile gate was
// still unsatisfied (#89843): on a shared-remote connection every profile is
// legitimately served through the primary socket, so $activeGatewayProfile
// never moves to the bot's profile and the old gate burned the whole 20s
// hydration budget with the transcript already painted. The stored history is
// shown immediately instead; this atom drives the subtle "Syncing…" affordance
// until the gate catches up (or the next wake supersedes it). Null when no
// paint-first wake is outstanding.
export const $hydrationSyncProfile = atom<string | null>(null)

// ── Hover-intent backend pre-warm ───────────────────────────────────────────
// A cold switch to a profile whose pool backend isn't running pays the full
// spawn (Python boot + port announce + readiness probe — measured ~2.5-3s)
// plus the socket connect before the sidebar can repopulate. The pointer
// entering a profile square in the rail signals the switch a few hundred ms
// before the click lands, so we run the same spawn + connect chain then
// (openGatewayForProfile — without activating). `ensureBackend` in the
// Electron main is idempotent (a pooled profile returns its existing
// connectionPromise), so the real switch joins the in-flight work instead of
// duplicating it — and a pre-warm for an already-open profile is a no-op.
// Throttled per profile so drive-by hovers can't spam spawn attempts; failures
// stay silent here and surface on the real switch, which owns retry/error UX.
const PREWARM_MIN_INTERVAL_MS = 60_000

const prewarmedAt = new Map<string, number>()

export function prewarmProfileBackend(name: string): void {
  const key = normalizeProfileKey(name)

  if (key === normalizeProfileKey($activeGatewayProfile.get())) {
    return
  }

  const now = Date.now()

  if (now - (prewarmedAt.get(key) ?? 0) < PREWARM_MIN_INTERVAL_MS) {
    return
  }

  // Prewarm/cap harmony (#91545): the pool caps spawned backends at the
  // configured max, and a spawn over the cap LRU-evicts the warmest idle
  // backend. A hover sweep across the rail therefore evicted backends for
  // profiles the user was about to click — prewarming caused the exact churn
  // it exists to prevent. Skip speculative spawns once every pool slot is
  // occupied by an open socket; the real click still spawns on demand, it
  // just doesn't get a head start.
  if (openSecondaryCount() + 1 > $poolLimits.get().maxBackends) {
    return
  }

  prewarmedAt.set(key, now)
  openGatewayForProfile(key).catch(() => undefined)
}

let gatewaySwitch: Promise<void> | null = null

// Descriptor lookups are IPC round-trips into Electron main. A wedged main
// (the #93454 class: a ticket mint that never answers) must not latch the
// gatewaySwitch mutex — and, through it, every later profile/source switch
// and the switch barrier — so they are bounded and fail open like any other
// lookup failure.
const DESCRIPTOR_LOOKUP_TIMEOUT_MS = 20_000

// The target profile's connection descriptor (mode / baseUrl / …), resolved
// CONCURRENTLY with the socket work so the switch can publish the profile
// pointer and $connection in one frame. Without this, $connection seeds from
// the PRIMARY backend at boot and only refreshes on sleep/wake — activating a
// *background* profile left it describing the primary, with the wrong `mode`
// for everything that branches on local-vs-remote (#46651: path-based
// `image.attach` against a remote gateway, /api/fs/* and /api/media on the
// wrong machine).
//
// Best-effort BY DESIGN (fail open): a failed lookup resolves null, the prior
// descriptor stays, and boot/reconnect resyncs it later. The earlier
// atomic-publish series (#89483) failed the whole switch closed here instead,
// and its decline path turned routine registry churn into dead profile
// clicks (#89622) — reverted in #89785. Do not reintroduce fail-closed
// switching at this seam.
async function resolveConnectionForProfile(profile: string): Promise<HermesConnection | null> {
  const getConnection = window.hermesDesktop?.getConnection

  if (!getConnection) {
    return null
  }

  try {
    return await withTimeout(
      getConnection(profile),
      DESCRIPTOR_LOOKUP_TIMEOUT_MS,
      `Timed out resolving the connection descriptor for profile "${profile}"`
    )
  } catch (err) {
    console.warn(`[profile] descriptor lookup for "${profile}" failed; keeping the previous connection`, err)

    return null
  }
}

// Make `profile`'s backend the active gateway, lazily opening its socket if it
// isn't live yet. Unlike the old single-socket swap, background profiles keep
// their sockets — so their sessions keep streaming concurrently. A null/empty
// target means "no explicit profile" → keep the current gateway (a plain new
// chat stays put; single-profile users never leave the primary).
export async function ensureGatewayProfile(profile: string | null | undefined): Promise<void> {
  if (profile == null || !String(profile).trim()) {
    // "No explicit profile" = use the current gateway. But if an explicit swap
    // (e.g. the user just picked a profile in the switcher) is still in flight,
    // let it settle first so a new chat doesn't race session.create against a
    // half-open socket and land on the wrong backend.
    if (gatewaySwitch) {
      await gatewaySwitch.catch(() => undefined)
    }

    return
  }

  const target = normalizeProfileKey(profile)

  // "Already here" requires being on the LOCAL POOL too, not just the same
  // profile name. A registry agent can hold the identical profile key (remote
  // `default` vs local `default`), so a key-only comparison would treat
  // returning to this device as a no-op and strand the window on the remote
  // backend — the same (connection, profile) confusion as #85731.
  const onLocalPool = () => $activeGatewayConnection.get() === null

  if (
    onLocalPool() &&
    normalizeProfileKey($activeGatewayProfile.get()) === target &&
    $gateway.get()?.connectionState === 'open'
  ) {
    return
  }

  // Serialize concurrent activations so rapid session switches cannot race the
  // active pointer. Re-acquire after every wake: multiple waiters can observe
  // the same settled switch, and the first one starts the next switch before
  // the others resume.
  while (gatewaySwitch) {
    await gatewaySwitch.catch(() => undefined)
  }

  if (
    onLocalPool() &&
    normalizeProfileKey($activeGatewayProfile.get()) === target &&
    $gateway.get()?.connectionState === 'open'
  ) {
    return
  }

  $gatewaySwapTarget.set(target)
  gatewaySwitch = (async () => {
    // ensureGatewayForProfile opens (or reuses) the target's socket and points
    // the active gateway at it — without closing the profile you came from.
    // The descriptor resolves concurrently so nothing awaits between the
    // activation and the publication below: the old post-activation
    // syncConnectionToActiveProfile await left a window where $gateway
    // already targeted the new backend while $connection still described the
    // previous one, and remote-aware paths announced the wrong mode (#46651).
    const [connection] = await Promise.all([resolveConnectionForProfile(target), ensureGatewayForProfile(target)])

    // ONE publication frame. batch() defers Nanostores' notifications to the
    // end of the callback, so the profile pointer and the connection
    // descriptor become visible together; a null descriptor (no bridge, or a
    // failed best-effort lookup) keeps the previous one — fail open.
    batch(() => {
      // The local pool owns this backend, so the active source is "this
      // device". Published inside the same batch as the profile pointer so
      // the profile/connection-source pair is never observable half-updated.
      $activeGatewayConnection.set(null)

      if (connection) {
        setConnection(connection)
      } else {
        clearComposerSelectionOwner()
      }

      $activeGatewayProfile.set(target)
    })
  })()

  // A failed switch must NOT fall back to the primary socket silently: that
  // would route the user's messages to the wrong profile's backend and cause
  // cross-profile session writes (#81094). The rejection propagates to
  // await-callers (session actions, slash commands) which surface it in their
  // own flows; fire-and-forget callers surface it via their own .catch below.
  try {
    await gatewaySwitch
  } finally {
    gatewaySwitch = null
    $gatewaySwapTarget.set(null)
  }
}

// Registry-aware sibling of syncConnectionToActiveProfile: a connection-scoped
// agent's descriptor comes from getConnectionFor (its SOURCE connection), not
// getConnection (the local pool). Same best-effort, fail-open contract as
// resolveConnectionForProfile: a failed lookup resolves null and keeps the
// previous descriptor.
async function resolveConnectionForAgent(connectionId: string, profile: string): Promise<HermesConnection | null> {
  const getConnectionFor = window.hermesDesktop?.getConnectionFor

  if (!getConnectionFor) {
    return null
  }

  try {
    return await withTimeout(
      getConnectionFor({ connectionId, profile }),
      DESCRIPTOR_LOOKUP_TIMEOUT_MS,
      `Timed out resolving the connection descriptor for agent "${connectionId}:${profile}"`
    )
  } catch (err) {
    console.warn(
      `[profile] descriptor lookup for agent "${connectionId}:${profile}" failed; keeping the previous connection`,
      err
    )

    return null
  }
}

// Phase one of the two-phase source switch (store/connections
// selectConnection): dial the (connectionId, profile) socket WITHOUT activating
// it. The active route, $activeGatewayProfile and $connection are untouched, so
// the previous backend stays fully bound and painted while the target
// spawns/connects — a dead target fails HERE and the current source loses
// nothing. The follow-up ensureGatewayAgent then finds the socket open and
// activates it synchronously, which lets the caller sever the previous
// backend's session bindings and publish the new source in the same tick
// (#93937). An already-open target is a no-op.
export async function openGatewayAgent(connectionId: string, profile: string): Promise<void> {
  const connection = connectionId.trim()

  if (!connection) {
    return
  }

  await openGatewayForAgent(connection, normalizeProfileKey(profile), { activationLease: true })
}

// Activate a connection-scoped agent's gateway — the (connectionId, profile)
// analogue of ensureGatewayProfile, and the door the SDK's ensureAgent goes
// through. Two invariants the raw store call (ensureGatewayForAgent) does not
// provide on its own:
//  - Every activation moves $activeGatewayProfile and resyncs $connection,
//    exactly like the profile path — otherwise activating an ALREADY-OPEN
//    registry agent left both describing the previous backend, routing
//    /api/fs, /api/media and image.attach to the wrong machine (the same
//    class as #46651) and pointing newSessionInProfile at the stale profile.
//  - Activations share the gatewaySwitch mutex with profile switches, so a
//    rapid agent↔profile (or agent↔agent) interleave can't finish out of
//    order and leave the EARLIER setActive() as the last write.
// Only a null connectionId falls through to the legacy profile path. Explicit
// `local` is a registry identity and must use the genuinely-local route.
//
// `beforeActivate` is the commit hook of the two-phase source switch
// (store/connections selectConnection). It runs INSIDE the serialized
// section, synchronously right before the socket is activated — i.e. after
// every earlier switch has published and before this one does — so the caller
// can sever the previous backend's session bindings at exactly that point
// (#93937). Returning false declines: nothing is activated or published, the
// mutex is released. That is how a switch superseded while queued behind
// another one steps aside without a destructive wipe. Not consulted on the
// null-connectionId profile fallthrough.
export interface EnsureGatewayAgentOptions {
  beforeActivate?: () => boolean
  /** Revokes this caller's right to activate or publish after async work. */
  signal?: AbortSignal
}

function releaseWhenAborted(promise: Promise<void>, signal?: AbortSignal): Promise<void> {
  if (!signal) {
    return promise
  }

  if (signal.aborted) {
    return Promise.resolve()
  }

  return new Promise<void>((resolve, reject) => {
    const onAbort = () => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }

    const settle = (callback: () => void) => {
      signal.removeEventListener('abort', onAbort)
      callback()
    }

    signal.addEventListener('abort', onAbort, { once: true })
    promise.then(
      () => settle(resolve),
      error => settle(() => reject(error))
    )
  })
}

export async function ensureGatewayAgent(
  connectionId: null | string,
  profile: string,
  { beforeActivate, signal }: EnsureGatewayAgentOptions = {}
): Promise<void> {
  const target = normalizeProfileKey(profile)
  const connection = (connectionId ?? '').trim() || null

  if (!connection) {
    return ensureGatewayProfile(target)
  }

  // Serialize against any in-flight profile/agent switch (shared mutex). A
  // loop, not a single await: several waiters wake from the same settled
  // switch, and the first to re-acquire starts a new one the rest must also
  // wait out — otherwise two overlapping activations run interleaved.
  while (gatewaySwitch) {
    await gatewaySwitch.catch(() => undefined)
  }

  if (signal?.aborted) {
    return
  }

  $gatewaySwapTarget.set(target)

  const activationWork = (async () => {
    if (signal?.aborted) {
      return
    }

    if (beforeActivate && !beforeActivate()) {
      return
    }

    // Descriptor resolves concurrently with the dial, same as the profile
    // path, so no await sits between the activation and the publication.
    const activation = signal
      ? ensureGatewayForAgent(connection, target, { signal })
      : ensureGatewayForAgent(connection, target)

    const [descriptor, activated] = await Promise.all([resolveConnectionForAgent(connection, target), activation])

    if (signal?.aborted) {
      return
    }

    if (!activated) {
      // The target stopped existing mid-dial (source edited/removed). Keep
      // every atom on the previous backend; the caller's surfaces re-check
      // what's active. Log so a dead agent click is diagnosable (#89622's
      // silence lesson) — but never fail the whole switch closed here.
      console.warn(`[profile] agent gateway activation for "${connection}:${target}" did not land`)

      return
    }

    // ONE publication frame, profile pointer + descriptor together. A null
    // descriptor keeps the previous one — fail open, resynced by
    // boot/reconnect later.
    batch(() => {
      // Registry-scoped: the live socket belongs to `connection`, not the
      // pool. Published in the same batch as the profile pointer so no
      // listener can observe the agent paired with the wrong source.
      $activeGatewayConnection.set(connection)

      // Remote-aware paths (image.attach_bytes vs image.attach, /api/fs/*,
      // /api/media) follow $connection. Null here is only the no-bridge case,
      // so keeping the previous descriptor is correct; a failed lookup
      // rejected above and never reached this frame.
      if (descriptor) {
        setConnection(descriptor)
      }

      // The activated registry coordinate is authoritative even when the
      // best-effort descriptor lookup failed. Publish it before the profile
      // atom wakes forced model reseeds or a picker can persist a selection.
      setComposerSelectionOwner(connection, target)
      $activeGatewayProfile.set(target)
    })
  })()

  // Cancellation releases the mutex immediately; activationWork remains
  // observed and is ownership-guarded at both gateway and publication seams.
  gatewaySwitch = releaseWhenAborted(activationWork, signal)

  try {
    await gatewaySwitch
  } finally {
    gatewaySwitch = null
    $gatewaySwapTarget.set(null)
  }
}

// Session-create/branch swap that PRESERVES the active source. The callers
// (send, fork, branch) resolve only a profile NAME, but a profile name does
// not identify a backend: `claudecode` on a registry connection and
// `claudecode` on the local pool are different machines. ensureGatewayProfile
// is by design the LOCAL-POOL door — its "already here" fast path requires
// $activeGatewayConnection === null, so calling it while a registry agent is
// active never no-ops: it dials a same-named LOCAL backend and re-homes the
// whole window to this device ($activeGatewayConnection ← null). That is the
// "I typed a message on the remote profile and got dumped back to my main
// profile" bug: the send itself silently retargeted the window — the exact
// authoritative-write fallback the desktop ladder forbids.
//
// When the target profile is the one the active registry connection already
// serves, stay on that connection (re-dialing through the agent door, which
// also recovers a dropped socket). Anything else keeps the legacy local-pool
// meaning, byte-identical for single-source users.
export async function ensureGatewaySessionProfile(profile: string | null | undefined): Promise<void> {
  const connection = $activeGatewayConnection.get()

  if (connection) {
    const active = normalizeProfileKey($activeGatewayProfile.get())
    const target = profile == null || !String(profile).trim() ? active : normalizeProfileKey(profile)

    if (target === active) {
      return ensureGatewayAgent(connection, target)
    }
  }

  return ensureGatewayProfile(profile)
}

// ── Sidebar profile scope (the "workspace switcher" model) ─────────────────
// Mirrors how Slack/VS Code/Linear do multi-context: you're "in" one profile at
// a time and the sidebar shows only that profile's sessions (clean rows, no
// per-row tags). The lone exception is an explicit "All profiles" mode that
// fans every profile's sessions into one grouped, browsable list.

export const ALL_PROFILES = '__all__'

/** Normalize a sidebar scope to the profile key used by session and cron queries. */
export const sidebarProfileForScope = (profileScope: string): string =>
  profileScope === ALL_PROFILES ? 'all' : normalizeProfileKey(profileScope)

/** Key a platform total by its Desktop profile route so counts cannot leak across profiles. */
export const messagingTotalsKey = (messagingProfile: string, sourceId: string): string =>
  `${messagingProfile}:${sourceId}`

const SHOW_ALL_PROFILES_STORAGE_KEY = 'hermes.desktop.showAllProfiles'

// Opt-in unified view. When false, scope follows the live gateway profile, so
// single-profile users (who never see the switcher) are completely unaffected.
export const $showAllProfiles = atom<boolean>(storedBoolean(SHOW_ALL_PROFILES_STORAGE_KEY, false))

$showAllProfiles.subscribe(value => persistBoolean(SHOW_ALL_PROFILES_STORAGE_KEY, value))

// The profile context the sidebar is currently showing: a concrete profile key,
// or ALL_PROFILES for the unified grouped view. Concrete scope is tied to the
// gateway so opening/selecting a profile (which swaps the gateway) moves the
// whole sidebar with it — a real context switch, not a separate filter to keep
// in sync.
export const $profileScope = computed([$showAllProfiles, $activeGatewayProfile], (showAll, gateway) =>
  showAll ? ALL_PROFILES : normalizeProfileKey(gateway)
)

// Switch the active context to `name`: leave "All profiles" mode, point new
// chats at it, and swap the single live gateway onto its backend (which moves
// $activeGatewayProfile → name, so $profileScope follows).
//
// `forceLocal` pins the activation to the LOCAL pool regardless of whichever
// source is currently browsed — needed by selectAgent's explicit
// connectionId:null case (the command palette's "this device" row), which
// must not inherit activateOnCurrentSource's "stay on the browsed remote"
// behavior below (#92194): that behavior is for bare rail/menu picks with no
// explicit connection in play, not for an explicit "go local" request.
export function selectProfile(name: string, options?: { forceLocal?: boolean }): void {
  const target = normalizeProfileKey(name)

  // Switching profiles (or coming back from the all-profiles browse view) starts
  // fresh; re-tapping the profile you're already in leaves your session be.
  // Coming back from a REGISTRY agent counts as switching even when the profile
  // key matches — it's a different machine.
  const switching =
    $showAllProfiles.get() ||
    target !== normalizeProfileKey($activeGatewayProfile.get()) ||
    $activeGatewayConnection.get() !== null

  $showAllProfiles.set(false)
  $newChatProfile.set(target)
  $newChatRoute.set(null)
  // Clearing the agent route must NOT discard the registry identity: the pick
  // is made on the source the user is looking at (activateOnCurrentSource
  // dials exactly that pair), so the draft's exact owner is that pair — or the
  // legacy profile-only path when that is the door the pick takes.
  captureNewChatSource(profilePickConnectionId(target))

  if (switching) {
    requestFreshSession()
  }

  // A profile with a remote override can fail to activate because the remote
  // host rejected its saved token (rotated/revoked). That must surface as a
  // "re-enter token" affordance, never a silently dead profile (#91349).
  // #81094: any other failed switch must be visible too — the profile pill
  // stays on the previous profile and the user learns why the backend is
  // unreachable.
  //
  // The profile rail is a live workspace switch, so it must not call
  // profile.set() and reload the window. Once activation succeeds, remember
  // the selection for the next Desktop launch through the persistence-only
  // IPC instead (#79886). Registry-source picks name ANOTHER source's
  // profiles, so only a primary-backend activation updates the startup
  // preference.
  const forceLocal = options?.forceLocal ?? false
  const onPrimary = forceLocal || activeGatewayConnectionId() == null

  const shouldRememberStartupProfile = onPrimary ? isLocalDesktopProfile(target) : Promise.resolve(false)

  void Promise.all([activateOnCurrentSource(target, forceLocal), shouldRememberStartupProfile])
    .then(([, shouldRemember]) => {
      if (shouldRemember) {
        return window.hermesDesktop?.profile?.remember(target)
      }

      return undefined
    })
    .catch((error: unknown) => {
      if (!notifyRemoteOverrideAuthFailure(target, error)) {
        notifyError(error, `Failed to switch to profile "${target}"`)
      }
    })
}

// Resolve persistence from the saved per-profile Desktop route, rather than the
// live backend descriptor. A descriptor lookup is intentionally best-effort:
// failure must not discard a successful local selection's startup preference.
// Conversely, `ssh`, `remote`, and `cloud` here are per-profile overrides and
// must never replace the local Desktop startup profile.
async function isLocalDesktopProfile(target: string): Promise<boolean> {
  const getConnectionConfig = window.hermesDesktop?.getConnectionConfig

  if (!getConnectionConfig) {
    return true
  }

  try {
    return (await getConnectionConfig(target)).mode === 'local'
  } catch {
    // Preserve the pre-fix local-primary behavior when Electron's config bridge
    // is temporarily unavailable. The next successful config read will still
    // exclude any remote override.
    return true
  }
}

// Route a profile pick at the source the user is LOOKING at. $profiles is the
// active gateway's list, so a pick made while a remote registry source is live
// names one of THAT source's profiles and must keep its connection id. Named
// picks on the primary and explicit "local" source stay on the legacy
// profile-only path so the main process can resolve a per-profile remote
// override before falling back to a local backend. Default on `local` stays
// on that source — see profilePickConnectionId.
//
// `forceLocal` bypasses that "current source" lookup entirely and always
// targets the local pool — see selectProfile's doc above.
function activateOnCurrentSource(target: string, forceLocal = false): Promise<void> {
  const connectionId = forceLocal ? null : profilePickConnectionId(target)

  return connectionId ? ensureGatewayAgent(connectionId, target) : ensureGatewayProfile(target)
}

// Registry-aware sibling of selectProfile: switch the active context to an agent
// on a NAMED connection. Same user-visible contract (leave the all-profiles
// view, point new chats at it, start fresh when the context actually changes),
// but the swap goes through ensureGatewayAgent so the socket is dialed against
// that connection's own backend. A null/local connectionId is delegated to
// selectProfile with forceLocal — an explicit (connection, profile) pair
// naming the local pool must land there, not wherever the app currently
// happens to be browsing.
//
// "Switching" is judged on the (connection, profile) PAIR, not the profile key
// alone — re-selecting `default` on a remote source while sitting on the local
// `default` is a real backend change, and comparing profile names alone would
// silently skip it.
export function selectAgent(connectionId: null | string, name: string): void {
  const connection = (connectionId ?? '').trim() || null

  if (!connection) {
    selectProfile(name, { forceLocal: true })

    return
  }

  const target = normalizeProfileKey(name)

  const switching =
    $showAllProfiles.get() ||
    target !== normalizeProfileKey($activeGatewayProfile.get()) ||
    connection !== $activeGatewayConnection.get()

  $showAllProfiles.set(false)
  $newChatProfile.set(target)
  $newChatRoute.set(null)
  captureNewChatSource(connection)

  if (switching) {
    requestFreshSession()
  }

  void ensureGatewayAgent(connection, target).catch((error: unknown) => {
    notifyError(error, `Failed to switch to profile "${target}"`)
  })
}

// Pin the next new chat to `name` (legacy profile-only door) so session.create
// reads the profile the user clicked "+" under, not whatever
// $activeGatewayProfile holds once an in-flight profile swap settles (#79005).
export function pinNewChatProfile(name: string): string {
  const target = normalizeProfileKey(name)
  $newChatProfile.set(target)
  $newChatRoute.set(null)
  captureNewChatSource(profilePickConnectionId(target))

  return target
}

// Start a fresh session in `name` WITHOUT collapsing the "All profiles" browse
// view. Unlike selectProfile, it leaves $showAllProfiles untouched, so the
// unified sidebar stays put — used by the per-profile "+" in the all-profiles
// session list, where switching scope would throw away the browse state the user
// is in. Points new chats at the profile and opens its backend so the next
// message lands in the right place.
export function newSessionInProfile(name: string): void {
  const target = pinNewChatProfile(name)
  requestFreshSession()
  // #81094: surface the failed dial instead of failing silently.
  void activateOnCurrentSource(target).catch((error: unknown) => {
    if (!notifyRemoteOverrideAuthFailure(target, error)) {
      notifyError(error, `Failed to open profile "${target}"`)
    }
  })
}

/** Start a draft owned by a specific registry agent. Foreground activation is
 * only a presentation step; the route stays attached to the draft for the
 * eventual session.create request. */
export function newSessionInAgent(route: AgentProfileRoute): void {
  const captured = {
    ...route,
    connectionId: route.connectionId.trim(),
    profile: normalizeProfileKey(route.profile),
    ...(route.targetProfile ? { targetProfile: normalizeProfileKey(route.targetProfile) } : {})
  }

  if (!captured.connectionId) {
    throw new Error('Agent profile route is missing connectionId')
  }

  $newChatProfile.set(captured.profile)
  $newChatRoute.set(captured)
  $newChatConnectionId.set(captured.connectionId)
  requestFreshSession()
  // #81094: surface the failed dial instead of failing silently.
  void ensureGatewayAgent(captured.connectionId, captured.profile).catch((error: unknown) => {
    notifyError(error, `Failed to open profile "${captured.profile}"`)
  })
}

export function setShowAllProfiles(value: boolean): void {
  $showAllProfiles.set(value)
}

export function toggleShowAllProfiles(): void {
  $showAllProfiles.set(!$showAllProfiles.get())
}

// ── Hotkey-driven profile switching ────────────────────────────────────────
// Positional + relative navigation for the rail, used by the keybind runtime.
// The ordered list is [default, ...named-in-rail-order]; switching is a no-op
// when the slot is empty so unused ⌘N keys stay harmless.

function orderedProfileKeys(): string[] {
  const profiles = $profiles.get()

  const named = sortByProfileOrder(
    profiles.filter(profile => !profile.is_default),
    $profileOrder.get()
  ).map(profile => normalizeProfileKey(profile.name))

  const hasDefault = profiles.some(profile => profile.is_default)

  return hasDefault ? ['default', ...named] : named
}

// Switch to the default (root ~/.hermes) profile — bound to ⌘1.
export function switchToDefaultProfile(): void {
  const def = $profiles.get().find(profile => profile.is_default)

  selectProfile(def ? def.name : 'default')
}

// Switch to the Nth named (non-default) profile in rail order (1-based).
export function switchProfileToSlot(slot: number): void {
  const named = sortByProfileOrder(
    $profiles.get().filter(profile => !profile.is_default),
    $profileOrder.get()
  )

  const target = named[slot - 1]

  if (target) {
    selectProfile(target.name)
  }
}

// Step to the next/previous profile in the rail, wrapping around.
export function cycleProfile(direction: 1 | -1): void {
  const keys = orderedProfileKeys()

  if (keys.length < 2) {
    return
  }

  const current = $showAllProfiles.get() ? -1 : keys.indexOf(normalizeProfileKey($activeGatewayProfile.get()))
  const start = current < 0 ? (direction === 1 ? -1 : 0) : current
  const next = (start + direction + keys.length) % keys.length

  selectProfile(keys[next])
}

// Bumped to ask the rail to open its "create profile" dialog (the dialog state
// is local to the rail component; this lets a global hotkey trigger it).
export const $profileCreateRequest = atom(0)

export function requestProfileCreate(): void {
  $profileCreateRequest.set($profileCreateRequest.get() + 1)
}

// Keepalive ping for the active pool backend so the main-process idle reaper
// (which can't see the direct renderer↔backend WS) spares it. No-op for the
// primary/default backend, which is never pooled.
export function touchActiveGatewayBackend(): void {
  // Always ping: the main process no-ops for non-pool (primary) backends, so we
  // don't need to know which profile is primary from here.
  const target = normalizeProfileKey($activeGatewayProfile.get())
  void window.hermesDesktop?.touchBackend?.(target).catch(() => undefined)
}
