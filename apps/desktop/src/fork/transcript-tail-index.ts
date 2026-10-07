import type { TranscriptTailState } from '@/store/transcript-tail'

type TailEntry = [string, TranscriptTailState]
const indexes = new WeakMap<Record<string, TranscriptTailState>, Map<string, TailEntry[]>>()

/**
 * Records are immutable snapshots: production writers spread/delete into a NEW
 * record before .set(), or replace it with {}. In-place key/state replacement on
 * an already indexed record is unsupported; .set(newRecord) invalidates by identity.
 * Preserve Object.entries order and bare-key equality, even for JSON-looking ids.
 */
export function indexedTailEntries(
  record: Record<string, TranscriptTailState>,
  storedSessionId: string
): TailEntry[] | undefined {
  if (typeof storedSessionId !== 'string' || !record || typeof record !== 'object' || Array.isArray(record)) {
    return undefined
  }

  let index = indexes.get(record)

  if (!index) {
    const prototype: unknown = Object.getPrototypeOf(record)

    if (prototype !== Object.prototype && prototype !== null) {
      return undefined
    }

    if (
      Object.values(Object.getOwnPropertyDescriptors(record)).some(field => field.enumerable && !('value' in field))
    ) {
      return undefined
    }

    index = new Map()

    const add = (id: string, entry: TailEntry) => {
      const entries = index!.get(id)

      if (entries) {
        entries.push(entry)
      } else {
        index!.set(id, [entry])
      }
    }

    for (const entry of Object.entries(record)) {
      const key = entry[0]
      add(key, entry)

      try {
        const scope: unknown = JSON.parse(key)

        if (Array.isArray(scope) && scope.length === 3 && typeof scope[2] === 'string' && scope[2] !== key) {
          add(scope[2], entry)
        }
      } catch {
        // A non-JSON key is a supported bare session id.
      }
    }

    indexes.set(record, index)
  }

  // Never expose the cached bucket to callers that sort/splice the result.
  return (index.get(storedSessionId) ?? []).map(([key, state]) => [key, state])
}

/** A session-specific source, not a general-purpose Array.filter replacement.
 * Supported immutable snapshots are already matched by the index. Unsupported
 * records still execute the untouched upstream predicate over Object.entries.
 */
export function tailEntriesForSession(record: Record<string, TranscriptTailState>, storedSessionId: string) {
  const indexed = indexedTailEntries(record, storedSessionId)

  return {
    matching(legacyPredicate: (entry: TailEntry) => boolean): TailEntry[] {
      return indexed ?? Object.entries(record).filter(legacyPredicate)
    }
  }
}
