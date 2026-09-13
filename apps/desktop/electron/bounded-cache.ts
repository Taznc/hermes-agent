// Shared eviction helpers for the small per-process caches scattered through
// main.ts. Each of these used to grow forever (bounded only by how long the
// app stays open + how many distinct keys a user's session/window/connection
// churn produces) — slow multi-day leaks, not crashes, which is exactly why
// they went unnoticed. `titleCache` (see main.ts) already had the right shape
// — cap the size, evict oldest-first on write, since a `Map`'s iteration
// order is insertion order — so this factors that one pattern out for reuse
// instead of hand-rolling it at every call site.

/**
 * Evict the oldest entries (insertion order) until `map.size <= limit`.
 * Call this right after a `.set()` — a size-only cap, no TTL. Cheap enough
 * to run on every write; a `Map` has no batch-delete, so this loop is the
 * whole cost.
 */
export function capMapSize<K, V>(map: Map<K, V>, limit: number): void {
  while (map.size > limit) {
    const oldestKey = map.keys().next().value

    if (oldestKey === undefined) {
      break
    }

    map.delete(oldestKey)
  }
}

/**
 * Delete entries whose recorded timestamp is older than `ttlMs`, then cap
 * the survivors at `limit`. For caches that honor a TTL on READ (return
 * `undefined`/refetch past the TTL) but never previously deleted the stale
 * entry — so every distinct key that ever passed through the cache stayed
 * resident forever even though it stopped being useful the moment it expired.
 * `getTimestamp` extracts the "recorded at" instant from a cache entry.
 */
export function pruneExpiredEntries<K, V>(
  map: Map<K, V>,
  now: number,
  ttlMs: number,
  getTimestamp: (value: V) => number,
  limit?: number
): void {
  for (const [key, value] of map) {
    if (now - getTimestamp(value) >= ttlMs) {
      map.delete(key)
    }
  }

  if (limit !== undefined) {
    capMapSize(map, limit)
  }
}
