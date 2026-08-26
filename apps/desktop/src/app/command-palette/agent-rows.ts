import type { DesktopAgentRoster, DesktopRosterAgent } from '@/global'

/** A roster agent (or an undialed source) reduced to what a palette row needs. */
export interface AgentPaletteRow {
  /** null = the local pool (this device); otherwise the registry connection to dial. */
  connectionId: null | string
  /** Device name — the label's subject. */
  device: string
  /** Pre-computed @name-device handle; a search keyword, not a label. */
  handle: string
  /** True when this row is the (connection, profile) pair the live socket is on. */
  isActive: boolean
  /** True for the app-managed local runtime. */
  isLocal: boolean
  /**
   * The source has no enumerated agents yet, so this row DIALS it rather than
   * switching between known profiles. Selecting it opens the connection (an SSH
   * tunnel is bootstrapped on demand) and lands on `profile`.
   */
  needsConnect: boolean
  profile: string
  /** Why the source produced no agents — shown as the row's detail. */
  unavailableReason?: string
}

interface BuildAgentPaletteRowsInput {
  activeConnectionId: null | string
  activeProfile: string
  /** Label for local-kind rows ("This device") — passed in so this stays i18n-free. */
  localLabel: string
  normalizeProfile: (name: string) => string
  roster: DesktopAgentRoster | null | undefined
}

/** The roster's marker for an SSH source that has never been dialed. */
const CONNECT_ON_DEMAND = 'connect-on-demand'

/** Profile every Hermes install has — the landing point when dialing a source blind. */
const DEFAULT_PROFILE = 'default'

/**
 * Reduce the union agent roster to palette rows.
 *
 * Three rules live here rather than in the component, because none of them is
 * observable by rendering:
 *
 *  1. **Single-source suppression.** With only the local runtime registered the
 *     profile rail already lists every agent, so the palette contributes
 *     nothing rather than duplicating it for the majority case.
 *  2. **Active is a PAIR.** The same profile name routinely exists on several
 *     sources, so `default` locally and `default` on a remote box are
 *     indistinguishable by profile key. Matching on the key alone marks the
 *     wrong row active and makes the real switch look like a no-op (#85731).
 *  3. **Undialed sources still get a row.** `hermes:agents:roster` deliberately
 *     skips SSH connections that have never been dialed (`connect-on-demand`)
 *     so that merely listing agents cannot spawn tunnels — they enumerate no
 *     profiles at all. Rendering only enumerated agents therefore strands the
 *     user in a dead end: the source can't be dialed because it has no row, and
 *     it has no row because it was never dialed. Such sources get a synthesized
 *     "connect" row targeting `default`, which dials the tunnel on demand; the
 *     real profile rows appear on the next roster refresh.
 */
export function buildAgentPaletteRows({
  activeConnectionId,
  activeProfile,
  localLabel,
  normalizeProfile,
  roster
}: BuildAgentPaletteRowsInput): AgentPaletteRow[] {
  if (!roster || roster.sources.length < 2) {
    return []
  }

  const activeKey = normalizeProfile(activeProfile)
  const enumerated = new Set(roster.agents.map(agent => agent.connectionId))

  const isActivePair = (connectionId: string, isLocal: boolean, profile: string) =>
    (isLocal ? activeConnectionId === null : connectionId === activeConnectionId) &&
    normalizeProfile(profile) === activeKey

  const agentRows = roster.agents.map((agent: DesktopRosterAgent) => {
    const isLocal = agent.connectionKind === 'local'

    return {
      connectionId: isLocal ? null : agent.connectionId,
      device: isLocal ? localLabel : agent.connectionLabel,
      handle: agent.handle,
      isActive: isActivePair(agent.connectionId, isLocal, agent.profile),
      isLocal,
      needsConnect: false,
      profile: agent.profile
    }
  })

  // Sources that enumerated nothing — undialed SSH boxes and unreachable hosts.
  // Both are actionable: dialing is exactly what an undialed source needs, and
  // retrying an unreachable one is how the user learns it came back.
  const connectRows = roster.sources
    .filter(source => source.kind !== 'local' && !enumerated.has(source.connectionId))
    .map(source => ({
      connectionId: source.connectionId,
      device: source.label,
      handle: source.label,
      isActive: isActivePair(source.connectionId, false, DEFAULT_PROFILE),
      isLocal: false,
      needsConnect: true,
      profile: DEFAULT_PROFILE,
      unavailableReason: source.error === CONNECT_ON_DEMAND ? undefined : source.error
    }))

  return [...agentRows, ...connectRows]
}
