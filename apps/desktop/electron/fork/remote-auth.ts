// Fork-owned: token-auth remote gateway support for WS ticket minting.
//
// A gated dashboard rejects the legacy `?token=` query param on the WS
// upgrade (403) — only a minted ticket is accepted — but it DOES accept that
// same static session token as a bearer on the mint endpoint. Without this
// rung a basic-auth/token backend can pass every HTTP check and still never
// open /api/ws. The OAuth-cookie and native-bearer rungs stay in upstream's
// mintGatewayWsTicket; this module owns only the fork's static-token rung
// and the remote-token branch of freshGatewayWsUrl.

export interface RemoteAuthDeps {
  /** main.ts's fetchJson — shared timeout/JSON/bearer handling. */
  fetchJson(url: string, token: string | null, options: any): Promise<any>
}

/**
 * Mint a single-use WS ticket using a static session token as a bearer on
 * the mint endpoint (the token-auth remote path).
 */
export async function mintWsTicketWithStaticToken(
  baseUrl: string,
  headers: Record<string, string>,
  staticToken: string,
  deps: RemoteAuthDeps
): Promise<string> {
  const body = (await deps.fetchJson(`${baseUrl}/api/auth/ws-ticket`, null, {
    method: 'POST',
    timeoutMs: 8_000,
    bearer: staticToken,
    headers
  })) as any

  const ticket = body?.ticket

  if (!ticket || typeof ticket !== 'string') {
    throw new Error('Gateway did not return a WS ticket.')
  }

  return ticket
}

export interface RemoteTokenWsUrlDeps {
  /** Loosely typed to match main.ts's untyped mintGatewayWsTicket(baseUrl, headers?, staticToken?). */
  mintTicket(baseUrl: string, headers: Record<string, string> | undefined, staticToken: string | null): Promise<string>
  buildTicketUrl(baseUrl: string, ticket: string): string
}

/**
 * The remote token-auth branch of freshGatewayWsUrl: a gated dashboard
 * refuses the legacy `?token=` param on the WS upgrade, so trade the token
 * for a single-use ticket. Local backends are ungated and have no mint
 * endpoint — the cached wsUrl is correct there, and a mint failure falls
 * back to it.
 */
export async function freshRemoteTokenWsUrl(
  connection: { baseUrl: string; headers: Record<string, string>; token: string; wsUrl: string },
  deps: RemoteTokenWsUrlDeps
): Promise<string> {
  try {
    const ticket = await deps.mintTicket(connection.baseUrl, connection.headers, connection.token)

    return deps.buildTicketUrl(connection.baseUrl, ticket)
  } catch {
    return connection.wsUrl
  }
}
