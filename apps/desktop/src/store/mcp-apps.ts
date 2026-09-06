import { atom, computed, type ReadableAtom } from 'nanostores'

import { parseMcpAppCard, type McpAppCardPayload } from '@/components/assistant-ui/mcp-app-card'

const $mcpApps = atom<Record<string, McpAppCardPayload>>({})
const cardCache = new Map<string, ReadableAtom<McpAppCardPayload | null>>()

/** Store a card only from the live tool-complete projection. Session hydration never writes here. */
export function recordMcpAppCard(toolCallId: string, value: unknown) {
  const card = parseMcpAppCard(value)
  if (!toolCallId || !card) {
    return
  }

  const current = $mcpApps.get()
  if (current[toolCallId]?.id === card.id) {
    return
  }

  $mcpApps.set({ ...current, [toolCallId]: card })
}

export function getMcpAppCard(toolCallId: string): McpAppCardPayload | null {
  return toolCallId ? $mcpApps.get()[toolCallId] || null : null
}

export function $mcpAppCard(toolCallId: string): ReadableAtom<McpAppCardPayload | null> {
  let cached = cardCache.get(toolCallId)
  if (!cached) {
    cached = computed($mcpApps, cards => (toolCallId ? cards[toolCallId] || null : null))
    cardCache.set(toolCallId, cached)
  }
  return cached
}
