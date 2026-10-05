/**
 * Stored `tool_call` bridge rows, hydrated as the tool they actually ran.
 *
 * A deferred tool is invoked through the `tool_call` bridge. The backend unwraps the
 * bridge before dispatch, so the live stream (`tool.start` / `tool.complete`) and the
 * stored tool RESULT row already carry the inner tool's name. Only the stored
 * assistant `tool_calls` entry keeps the wrapper (it must, for provider pairing).
 * Without this, a reloaded transcript names the row `tool_call`, so every name-keyed
 * surface (plugin result cards, card classification, answer-only visibility) misses it.
 *
 * Mirrors `resolve_underlying_call` in tools/tool_search.py: exactly one local entry
 * unwraps; connector batches, multi-entry batches and malformed calls stay `tool_call`
 * (connector rows render from the wrapper via `connectorCalls`). Registry-independent
 * on purpose: hydration can run before Desktop plugins finish registering.
 */

const TOOL_CALL_NAME = 'tool_call'
const BRIDGE_TOOL_NAMES = new Set([TOOL_CALL_NAME, 'tool_describe', 'tool_search'])
const CONNECTOR_NAME = /^connectors__/i

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

function objectArgs(value: unknown): Record<string, unknown> | null {
  if (value === undefined || value === null || (typeof value === 'string' && !value.trim())) {
    return {}
  }

  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value)

      return isRecord(parsed) ? parsed : null
    } catch {
      return null
    }
  }

  return isRecord(value) ? value : null
}

/** The single local call a stored `tool_call` row ran, or null to keep the wrapper. */
export function unwrapBridgedCall(
  toolName: string,
  args: Record<string, unknown>
): null | { args: Record<string, unknown>; toolName: string } {
  if (toolName !== TOOL_CALL_NAME) {
    return null
  }

  let calls: unknown = args.calls ?? (args.name !== undefined ? [{ arguments: args.arguments, name: args.name }] : null)

  if (typeof calls === 'string') {
    calls = objectArgs(`{"calls":${calls}}`)?.calls ?? null
  }

  const entries = isRecord(calls) ? [calls] : calls

  if (!Array.isArray(entries) || entries.length !== 1 || !isRecord(entries[0])) {
    return null
  }

  const name = typeof entries[0].name === 'string' ? entries[0].name.trim() : ''
  const inner = objectArgs(entries[0].arguments)

  if (!name || BRIDGE_TOOL_NAMES.has(name) || CONNECTOR_NAME.test(name) || !inner) {
    return null
  }

  return { args: inner, toolName: name }
}

const LABELS_ARG = 'hermes_tool_labels'

/**
 * Spread into `toolPartFromStoredCall`'s part after its own name/args: renames a stored
 * bridge row to the tool it ran (gateway labels ride along), or adds nothing.
 */
export function bridgedCallOverride(
  toolName: string,
  args: Record<string, unknown>
): { args?: never; argsText?: string; toolName?: string } {
  const unwrapped = unwrapBridgedCall(toolName, args)

  if (!unwrapped) {
    return {}
  }

  const next = { ...unwrapped.args, ...(args[LABELS_ARG] !== undefined ? { [LABELS_ARG]: args[LABELS_ARG] } : {}) }

  return {
    args: next as never,
    argsText: Object.keys(next).length ? JSON.stringify(next) : '',
    toolName: unwrapped.toolName
  }
}
