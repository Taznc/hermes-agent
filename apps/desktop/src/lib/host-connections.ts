import type { HermesConnection } from '@/global'

/**
 * Whether the HOST this renderer runs in owns a connection registry at all.
 *
 * Electron's main process keeps the desktop config — the saved gateway modes,
 * the registry of remote/SSH/Cloud entries, the OAuth state — and exposes it
 * through `getConnectionConfig`. The browser build has no main process, so the
 * web bridge shim deliberately omits that member; it is the sentinel the whole
 * connection surface gates on (see `web-bridge-shim.ts`).
 *
 * Gate on the ABSENT BRIDGE MEMBER, never on an `isWeb` flag: the capability
 * question is "can anything here register a second gateway?", and the bridge
 * is what answers it. A build flag would also be wrong for the Electron app
 * talking to an older main that predates the member.
 */
export function connectionsManagedByHost(): boolean {
  return typeof window !== 'undefined' && typeof window.hermesDesktop?.getConnectionConfig === 'function'
}

/**
 * The host of the one backend this renderer is pinned to, for honest copy
 * ("attached to a single backend on <host>").
 *
 * Returns '' when there is no descriptor yet or its `baseUrl` does not parse —
 * callers render a host-free sentence rather than "on undefined". Only the
 * authority (host[:port]) is used: a full URL with a token-bearing query is
 * never the right thing to paint into a settings page.
 */
export function singleBackendHostLabel(connection: HermesConnection | null | undefined): string {
  const baseUrl = connection?.baseUrl?.trim()

  if (!baseUrl) {
    return ''
  }

  try {
    return new URL(baseUrl).host
  } catch {
    return ''
  }
}
