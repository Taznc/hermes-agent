import { atom, computed, type ReadableAtom } from 'nanostores'

import { parseMcpAppCard, type McpAppCardPayload } from '@/components/assistant-ui/mcp-app-card'

const $mcpApps = atom<Record<string, McpAppCardPayload>>({})
const cardCache = new Map<string, ReadableAtom<McpAppCardPayload | null>>()

function cardKey(sessionId: string, toolCallId: string): string {
  return `${sessionId}\u0000${toolCallId}`
}

/** Store a card only from the live tool-complete projection. Session hydration never writes here. */
export function recordMcpAppCard(sessionId: string, toolCallId: string, value: unknown) {
  const card = parseMcpAppCard(value)
  if (!sessionId || !toolCallId || !card) {
    return
  }

  const key = cardKey(sessionId, toolCallId)
  const current = $mcpApps.get()
  if (current[key]?.id === card.id) {
    return
  }

  $mcpApps.set({ ...current, [key]: card })
}

export function clearMcpAppCards(sessionId: string): void {
  if (!sessionId) {
    return
  }

  const prefix = `${sessionId}\u0000`
  const current = $mcpApps.get()
  const remaining = Object.fromEntries(Object.entries(current).filter(([key]) => !key.startsWith(prefix)))
  if (Object.keys(remaining).length !== Object.keys(current).length) {
    $mcpApps.set(remaining)
  }
}

export function getMcpAppCard(sessionId: string, toolCallId: string): McpAppCardPayload | null {
  return sessionId && toolCallId ? $mcpApps.get()[cardKey(sessionId, toolCallId)] || null : null
}

export function $mcpAppCard(sessionId: string, toolCallId: string): ReadableAtom<McpAppCardPayload | null> {
  const key = cardKey(sessionId, toolCallId)
  let cached = cardCache.get(key)
  if (!cached) {
    cached = computed($mcpApps, cards => (sessionId && toolCallId ? cards[key] || null : null))
    cardCache.set(key, cached)
  }
  return cached
}
