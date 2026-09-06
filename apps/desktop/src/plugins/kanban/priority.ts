/**
 * Support-facing severity labels over Kanban's numeric scheduler tiebreaker.
 *
 * The backend intentionally accepts arbitrary integers and sorts descending.
 * This UI policy keeps ordinary board work predictable: new cards are Normal
 * (0), while Critical and High run ahead and Low runs after normal work.
 */
export const PRIORITY_LEVELS = [
  { id: 'critical', value: 2 },
  { id: 'high', value: 1 },
  { id: 'normal', value: 0 },
  { id: 'low', value: -1 }
] as const

export type PriorityLevelId = (typeof PRIORITY_LEVELS)[number]['id'] | 'custom'

export interface PriorityLevel {
  id: PriorityLevelId
  value: number
}

export function priorityLevel(priority: number | undefined): PriorityLevel {
  const value = priority ?? 0

  return PRIORITY_LEVELS.find(level => level.value === value) ?? { id: 'custom', value }
}
