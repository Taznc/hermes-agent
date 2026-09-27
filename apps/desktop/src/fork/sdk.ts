/**
 * `host.fork` — the fork's additive plugin SDK namespace (FORK.md "fork extension
 * points"). Reached from upstream through ONE marked property in `sdk/index.ts`; every
 * later Desktop extension point adds its key here, never in the upstream SDK.
 */
import { PLUGIN_EVENT_TYPE, PLUGIN_REQUEST_METHOD, UI_REQUEST_AREA } from '@/fork/ui-bridge/types'

export type {
  PluginEventPayload,
  PluginRequestParams,
  PluginRequestResult,
  UiRequestContribution,
  UiRequestRenderProps,
  UiToolResultRenderProps
} from '@/fork/ui-bridge/types'

/**
 * X02 plugin UI bridge. A Desktop plugin renders a Python plugin's `ui.request(kind, …)`
 * inline under the tool call that asked:
 *
 *   ctx.register({ id: 'ask', area: host.fork.ui.UI_REQUEST_AREA,
 *                  data: { kind: 'ask/questions', render: props => … } })
 *
 * `render({params, respond, cancel, sessionId, isActive, kind})`; `respond(payload)` is
 * what `ui.request` returns, `cancel()` makes it return None. No renderer for `kind` →
 * the backend gets `{unsupported: true}` at once and the plugin falls back. Fire-and-forget
 * `ui.emit(kind, …)` arrives as `host.onEvent(host.fork.ui.PLUGIN_EVENT_TYPE, e => …)`
 * with `e.payload = {kind, payload}`.
 */
export const forkUi = { PLUGIN_EVENT_TYPE, PLUGIN_REQUEST_METHOD, UI_REQUEST_AREA } as const

export const forkHost = { ui: forkUi } as const
