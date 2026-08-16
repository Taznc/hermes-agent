import type { DesktopAgentRoster, DesktopRosterAgent } from '@/global'

/** A roster agent reduced to what a palette row needs to render and act. */
export interface AgentPaletteRow {
  /** null = the local pool (this device); otherwise the registry connection to dial. */
  connectionId: null | string
  /** Device name shown as the row's detail — the caller supplies the local label. */
  device: string
  /** True when this row is the (connection, profile) pair the live socket is on. */
  isActive: boolean
  /** True for the app-managed local runtime. */
  isLocal: boolean
  /** Its source failed to enumerate — still listed, but flagged. */
  isUnreachable: boolean
  profile: string
  /** Pre-computed @name-device handle; a search keyword, not a label. */
  handle: string
}

interface BuildAgentPaletteRowsInput {
  activeConnectionId: null | string
  activeProfile: string
  /** Label for local-kind rows ("This device") — passed in so this stays i18n-free. */
  localLabel: string
  normalizeProfile: (name: string) => string
  roster: DesktopAgentRoster | null | undefined
}

/**
 * Reduce the union agent roster to palette rows.
 *
 * Two rules live here rather than in the component, because both are easy to
 * get wrong and neither is observable by rendering:
 *
 *  1. **Single-source suppression.** With only the local runtime registered the
 *     profile rail already lists every agent, so the palette must contribute
 *     nothing — otherwise every single-source user (the majority) gets a
 *     duplicate list they never asked for.
 *  2. **Active is a PAIR.** The same profile name routinely exists on several
 *     sources, so `default` locally and `default` on a remote box are
 *     indistinguishable by profile key. Matching on the key alone marks the
 *     wrong row active and makes the real switch look like a no-op (#85731).
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

  const unreachable = new Set(roster.sources.filter(source => !source.reachable).map(source => source.connectionId))
  const activeKey = normalizeProfile(activeProfile)

  return roster.agents.map((agent: DesktopRosterAgent) => {
    const isLocal = agent.connectionKind === 'local'
    const sameSource = isLocal ? activeConnectionId === null : agent.connectionId === activeConnectionId

    return {
      connectionId: isLocal ? null : agent.connectionId,
      device: isLocal ? localLabel : agent.connectionLabel,
      handle: agent.handle,
      isActive: sameSource && normalizeProfile(agent.profile) === activeKey,
      isLocal,
      isUnreachable: unreachable.has(agent.connectionId),
      profile: agent.profile
    }
  })
}
