/**
 * Fork extension point X02 (Desktop half): the plugin UI bridge.
 *
 * Wire shapes are HAND-WRITTEN here on purpose. The Python half
 * (`hermes_fork/ui/`) declares them at registration and the
 * `gateway-contracts-upstream-only` anchor keeps them out of upstream's
 * generated `gateway-contract.generated.ts` (F24 Option A), so the shared
 * contract never learns about them and this file is their only TS home.
 *
 *   server→client request  `plugin.request` {session_id, kind, payload}
 *                          → {payload} | {unsupported: true}
 *   event                  `plugin.event`   {kind, payload}   (via host.onEvent)
 */
import type { ReactNode } from 'react'

export const PLUGIN_REQUEST_METHOD = 'plugin.request'
export const PLUGIN_EVENT_TYPE = 'plugin.event'

/** Contribution area a Desktop plugin registers its renderers into. */
export const UI_REQUEST_AREA = 'fork.ui.requests'

/** `plugin.request` params as they arrive (session_id is added by the gateway). */
export interface PluginRequestParams {
  session_id: string
  /** `<plugin_id>/<name>` — the Python side refuses any other namespace. */
  kind: string
  payload: unknown
}

/** A `plugin.request` answer. */
export type PluginRequestResult = { payload: unknown } | { unsupported: true }

/** `plugin.event` payload (fire-and-forget, observed with `host.onEvent('plugin.event', …)`). */
export interface PluginEventPayload {
  kind: string
  payload: unknown
}

/** Props handed to a contributor's `render`. */
export interface UiRequestRenderProps {
  kind: string
  /** The plugin's own payload from `ui.request(kind, payload)`. Untrusted. */
  params: unknown
  /** Answer the request; the Python `ui.request` returns this payload. First answer wins. */
  respond: (payload: unknown) => void
  /** Dismiss without an answer; the Python `ui.request` returns `None`. */
  cancel: () => void
  /** Runtime session the originating tool call belongs to. */
  sessionId: string
  /** That session is the one on screen in the primary view. */
  isActive: boolean
}

/** Props handed to an optional `renderResult` — the settled tool row, rebuilt from the
 *  tool RESULT (the only durable record; nothing else is persisted). */
export interface UiToolResultRenderProps {
  args: unknown
  isError: boolean
  result: unknown
  toolCallId: string
  toolName: string
}

/** Payload of a `UI_REQUEST_AREA` contribution's `data`. */
export interface UiRequestContribution {
  /** The `kind` this renderer answers. First registration wins on collision. */
  kind: string
  /** Renders the live, answerable card inline under the originating tool call. */
  render: (props: UiRequestRenderProps) => ReactNode
  /** Optional: the tool NAME whose settled result `renderResult` draws (after answer and
   *  on hydration). Without it the settled row is the stock tool row. */
  tool?: string
  renderResult?: (props: UiToolResultRenderProps) => ReactNode
}
