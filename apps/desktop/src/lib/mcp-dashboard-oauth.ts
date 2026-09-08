import { capabilityScoped, type ProfileScope } from '@/api/client'
import { type McpOAuthFlow, mcpOAuthRpc } from '@/api/mcp'

import { isMissingRpcMethod } from './gateway-rpc'

interface CompleteOptions {
  serverName: string
  profile?: ProfileScope
  cancelled?: () => boolean
  sleep?: (milliseconds: number) => Promise<void>
  maxPollFailures?: number
  timeoutMs?: number
  /**
   * A popup window already opened at the real user-gesture boundary (see
   * `openMcpOAuthPopup`), for callers that must `await` something (adding a
   * server, resolving a profile, installing a catalog preset) before they can
   * call this function. Chromium's transient activation from a click expires
   * across such an await, so `window.open()` called for the first time INSIDE
   * this function can be silently blocked as an unsolicited popup — passing a
   * handle opened synchronously in the click handler avoids that entirely.
   * `null` means the caller already tried and the browser blocked it (skip
   * the flow immediately, no network call); `undefined` means the caller has
   * no await before this call and lets this function open the popup itself.
   */
  popupWindow?: Window | null
}

interface OAuthResult {
  ok: boolean
  session_id?: string
  auth_url?: string
  status?: 'pending' | 'approved' | 'error'
  error_message?: string
  tools?: McpOAuthFlow['tools']
}

/** Deliberate cancellation is not an error toast. */
export class McpOAuthCancelled extends Error {
  constructor() {
    super('OAuth cancelled by user')
    this.name = 'McpOAuthCancelled'
  }
}

const defaultSleep = (milliseconds: number) => new Promise<void>(resolve => window.setTimeout(resolve, milliseconds))
const UPDATE_BACKEND = 'Update the Hermes backend to support Desktop MCP OAuth callbacks.'

/**
 * Open the OAuth popup at the REAL user-gesture boundary. Callers whose click
 * handler does anything async (add a server, resolve/create a profile,
 * install a catalog preset) before reaching `completeMcpDesktopOAuth` must
 * call this SYNCHRONOUSLY inside the click handler, before their first
 * `await`, and pass the result through as `popupWindow` — Chromium's
 * transient activation from the click expires across an intervening await,
 * so a `window.open()` first attempted deep inside the OAuth helper can be
 * silently blocked as an unsolicited popup on a slow network hop. A handler
 * with NO await before calling completeMcpDesktopOAuth may omit this and let
 * the helper open its own popup (undefined `popupWindow`) — both shapes are
 * equally valid; add it wherever the click-to-call path is not synchronous.
 */
export function openMcpOAuthPopup(): Window | null {
  const authWindow = window.open('about:blank', '_blank') as Window | null

  if (authWindow) {
    authWindow.opener = null
  }

  return authWindow
}

/**
 * Browser-native fallback used when window.hermesDesktop.mcpOauth is absent
 * (the web build — a browser tab cannot host a loopback HTTP listener, so
 * Electron's IPC-relayed flow does not apply here at all). The REST
 * `/api/mcp/servers/{name}/auth` route already tells the SERVER to host its
 * own loopback listener when no `client_redirect_uri` is supplied — the same
 * code path completeMcpDesktopOAuth's `!listener` branch above already
 * exercises for a local Electron connection — so this needs no new backend
 * route, just a browser-appropriate driver: open a popup synchronously
 * (before the first await, so browsers don't block it as unsolicited),
 * start the flow, navigate the popup to the authorization URL, and poll
 * `/api/mcp/oauth/flows/{flow_id}` until it settles.
 */
async function completeMcpBrowserOAuth({
  serverName,
  profile,
  cancelled,
  sleep = defaultSleep,
  maxPollFailures = 3,
  timeoutMs = 360_000,
  popupWindow
}: CompleteOptions): Promise<McpOAuthFlow> {
  const deadline = Date.now() + timeoutMs
  const scope = capabilityScoped(profile)

  // Prefer a popup the caller already opened synchronously at its own click
  // boundary (see openMcpOAuthPopup's doc comment); only open one here when
  // the caller has no intervening await and passed nothing (undefined).
  const authWindow = popupWindow !== undefined ? popupWindow : (window.open('about:blank', '_blank') as Window | null)

  if (!authWindow) {
    throw new Error('OAuth popup was blocked — allow popups for this app and retry.')
  }

  authWindow.opener = null

  let flowId: string | undefined
  let approved = false

  const checkCancelled = () => {
    if (cancelled?.()) {
      throw new McpOAuthCancelled()
    }
  }

  try {
    checkCancelled()

    const started = await window.hermesDesktop.api<{
      flow_id: string
      status: string
      authorization_url: string | null
      error: string | null
    }>({
      ...scope,
      // client_public_origin: this tab's real public origin, so the backend
      // can build an externally reachable OAuth callback when
      // dashboard.public_url is unset. Without it, a same-origin `/api`
      // proxy with changeOrigin:true (the web-served Desktop renderer's
      // vite dev-server proxy) makes the backend see its own loopback
      // address as request.base_url — a callback the OAuth provider could
      // never reach. See _mcp_oauth_callback_url in hermes_cli/web_routers/mcp.py.
      path: `/api/mcp/servers/${encodeURIComponent(serverName)}/auth?client_public_origin=${encodeURIComponent(window.location.origin)}`,
      method: 'POST'
    })

    flowId = started.flow_id
    checkCancelled()

    if (started.status === 'error') {
      throw new Error(started.error || 'MCP OAuth failed to start')
    }

    if (!started.authorization_url) {
      throw new Error('OAuth server did not provide an authorization URL')
    }

    authWindow.location.href = started.authorization_url

    let pollFailures = 0

    for (;;) {
      checkCancelled()

      if (Date.now() >= deadline) {
        throw new Error('Timed out waiting for MCP OAuth authorization')
      }

      let current: { status: string; error: string | null; tools?: McpOAuthFlow['tools'] }

      try {
        current = await window.hermesDesktop.api({
          ...scope,
          path: `/api/mcp/oauth/flows/${encodeURIComponent(flowId)}`
        })
        pollFailures = 0
      } catch (error) {
        if (++pollFailures >= maxPollFailures) {
          throw error
        }

        await sleep(1000)

        continue
      }

      checkCancelled()

      if (current.status === 'approved') {
        approved = true

        return {
          flow_id: flowId,
          server_name: serverName,
          status: 'approved',
          authorization_url: started.authorization_url,
          error: null,
          tools: current.tools
        }
      }

      if (current.status === 'error') {
        throw new Error(current.error || 'OAuth authorization failed')
      }

      if (authWindow.closed) {
        throw new Error('OAuth authorization window was closed before completion')
      }

      await sleep(1000)
    }
  } finally {
    if (!authWindow.closed) {
      authWindow.close()
    }

    if (flowId && !approved) {
      await window.hermesDesktop
        .api({ ...scope, path: `/api/mcp/oauth/flows/${encodeURIComponent(flowId)}`, method: 'DELETE' })
        .catch(() => {
          // Best-effort cleanup; the flow TTLs out server-side regardless.
        })
    }
  }
}

/** Remote gateways require the Desktop callback bridge. An explicitly local
 *  gateway can host its own loopback listener when that capability is absent.
 *  On the web build the bridge namespace never exists at all — see
 *  completeMcpBrowserOAuth above for that path. */
export async function completeMcpDesktopOAuth(options: CompleteOptions): Promise<McpOAuthFlow> {
  const { serverName, profile, cancelled, sleep = defaultSleep, maxPollFailures = 3, timeoutMs = 360_000 } = options
  const deadline = Date.now() + timeoutMs
  const scope = capabilityScoped(profile)
  const rpc = mcpOAuthRpc(scope)
  const bridge = window.hermesDesktop.mcpOauth

  if (!bridge) {
    // isWebBuild is an explicit build-identity flag (fork/desktop-api.d.ts),
    // set ONLY by the web shim — never inferred from bridge-member absence.
    // That distinction matters because "no mcpOauth" means two different
    // things: on web, no browser tab can ever host a loopback listener, so
    // the REST/popup fallback is always correct. On an OLD Electron preload
    // that merely predates this bridge member (bridge-absent + a REMOTE
    // connection, or bridge-absent + explicit 'local'), the app CAN still
    // host a real loopback listener — the pre-fix behavior (compat message
    // for 'local', hard failure otherwise) is the correct compatibility path
    // and must stay untouched here, not be widened into a browser popup that
    // Electron cannot complete (no in-app browser tab to navigate).
    if (window.hermesDesktop.isWebBuild) {
      return completeMcpBrowserOAuth(options)
    }

    // A legacy null connection can resolve to a remote registry primary,
    // where the compat message is still correct (an OLD Electron build
    // predating the bridge, dialed at 'local'). Any other bridge-absent
    // Electron connection (a remote gateway on an old preload) fails the
    // same way — this restores the exact pre-fix behavior for every
    // bridge-absent case that is not the web build.
    throw new Error('Update Hermes Desktop to support MCP OAuth callbacks.')
  }

  let listener: { id: string; redirectUri: string } | undefined
  let sessionId: string | undefined
  let authUrl: string | undefined
  let approved = false
  let closed = false
  let relayError: unknown

  const checkCancelled = () => {
    if (cancelled?.()) {
      throw new McpOAuthCancelled()
    }
  }

  const request = async (action: 'start' | 'poll' | 'callback' | 'cancel', params: Record<string, unknown>) => {
    const result = await rpc<OAuthResult>(action, { name: serverName, ...params })

    if (!result.ok) {
      throw new Error(result.error_message || 'MCP OAuth request failed')
    }

    return result
  }

  try {
    checkCancelled()

    if (bridge) {
      listener = await bridge.listen()
      checkCancelled()
    }

    const started = await request('start', listener ? { client_redirect_uri: listener.redirectUri } : {})
    sessionId = started.session_id
    authUrl = started.auth_url
    // Start may have created a flow while the user cancelled. Keep its id so
    // finally can release it, but do not launch a browser after cancellation.
    checkCancelled()

    if (!sessionId || !authUrl) {
      throw new Error('OAuth server did not provide an authorization URL and session')
    }

    // Pre-relay backends silently ignore unknown start params. Do not open an
    // authorization URL whose DCR/PKCE flow points at a different machine.
    const redirectUri = new URL(authUrl).searchParams.get('redirect_uri')

    if (listener && redirectUri !== listener.redirectUri) {
      throw new Error(UPDATE_BACKEND)
    }

    if (!listener) {
      // Match the existing backend-hosted listener, not an arbitrary local URL.
      const redirect = new URL(redirectUri || '')

      if (
        redirect.protocol !== 'http:' ||
        redirect.hostname !== '127.0.0.1' ||
        !redirect.port ||
        Number(redirect.port) === 0 ||
        redirect.pathname !== '/callback' ||
        redirect.username ||
        redirect.password ||
        redirect.search ||
        redirect.hash
      ) {
        throw new Error('OAuth server did not provide a local loopback callback URL')
      }
    }

    const flowId = sessionId

    if (bridge && listener) {
      void bridge
        .wait(listener.id)
        .then(async callback => {
          if (closed || cancelled?.()) {
            return
          }

          if (!callback.state) {
            throw new Error(callback.error || 'OAuth callback did not include state')
          }

          await request('callback', { session_id: flowId, ...callback })
        })
        .catch(error => {
          relayError = error
        })
    }

    await window.hermesDesktop.openExternal(authUrl)
    let pollFailures = 0

    for (;;) {
      checkCancelled()

      if (Date.now() >= deadline) {
        throw new Error('Timed out waiting for MCP OAuth authorization')
      }

      if (relayError) {
        throw relayError
      }

      let current: OAuthResult

      try {
        current = await request('poll', { session_id: flowId })
        pollFailures = 0
      } catch (error) {
        if (++pollFailures >= maxPollFailures) {
          throw error
        }

        await sleep(1000)

        continue
      }

      checkCancelled()

      if (relayError) {
        throw relayError
      }

      if (current.status === 'approved') {
        approved = true

        return {
          flow_id: flowId,
          server_name: serverName,
          status: 'approved',
          authorization_url: authUrl,
          error: null,
          tools: current.tools
        }
      }

      if (current.status === 'error') {
        throw new Error(current.error_message || 'OAuth authorization failed')
      }

      await sleep(1000)
    }
  } catch (error) {
    if (isMissingRpcMethod(error)) {
      throw new Error(UPDATE_BACKEND)
    }

    throw error
  } finally {
    closed = true

    // Stop the native waiter first: its synthetic cancellation is NOT a
    // provider callback and must never race cleanup back onto the gateway.
    if (bridge && listener) {
      await bridge.cancel(listener.id).catch(() => {})
    }

    if (sessionId && !approved) {
      try {
        await request('cancel', { session_id: sessionId })
      } catch (error) {
        // Relay-capable backends predating oauth.cancel can still release a
        // waiting worker with a state-checked denial. No HTTP redirect retry.
        if (isMissingRpcMethod(error) && authUrl) {
          const state = new URL(authUrl).searchParams.get('state')

          if (state) {
            await request('callback', { session_id: sessionId, state, error: 'access_denied' }).catch(() => {})
          }
        }
        // Network cleanup is best-effort; backend callback timeout is bounded.
      }
    }
  }
}
