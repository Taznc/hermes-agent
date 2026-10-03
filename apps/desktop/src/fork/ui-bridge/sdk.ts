/**
 * `host.fork.ui` — X02 plugin UI bridge, mounted on the fork namespace in
 * `src/fork/sdk-host.ts` (reached from upstream through its one `host-fork` anchor).
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
 * A Desktop plugin renders a Python plugin's `ui.request(kind, …)` inline under the
 * tool call that asked:
 *
 *   ctx.register({ id: 'ask', area: host.fork.ui.UI_REQUEST_AREA,
 *                  data: { kind: 'fork-ask/questions', render: props => … } })
 *
 * `render({params, respond, cancel, sessionId, isActive, kind})`; `respond(payload)` is
 * what `ui.request` returns, `cancel()` makes it return None. No renderer for `kind` →
 * the backend gets `{unsupported: true}` at once and the plugin falls back. Fire-and-forget
 * `ui.emit(kind, …)` arrives as `host.onEvent(host.fork.ui.PLUGIN_EVENT_TYPE, e => …)`
 * with `e.payload = {kind, payload}`. Feature-detect: `host.fork?.ui?.version >= 1`.
 */
export const forkUi = { PLUGIN_EVENT_TYPE, PLUGIN_REQUEST_METHOD, UI_REQUEST_AREA, version: 1 as const } as const
