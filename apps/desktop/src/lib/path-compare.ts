/** Comparing two paths for identity or containment, across host spellings.
 *
 *  A cwd reaches us from the backend, a picker, a session row and a project
 *  record, and the four don't agree on separator, trailing slash or drive-letter
 *  case. Compare through these rather than `===` / `startsWith`.
 */

/** POSIX-style spelling: one separator, no trailing slash. */
export const cleanPath = (path: string): string => path.trim().replace(/\\/g, '/').replace(/\/+$/, '') || '/'

/** Case-folded comparison key. Windows drive/UNC paths are case-insensitive;
 *  POSIX paths are not, and callers that display a path want its real spelling,
 *  so fold only the key. Expects an already-`cleanPath`ed value. */
export const comparisonPath = (path: string): string =>
  /^[A-Za-z]:(?:\/|$)/.test(path) || path.startsWith('//') ? path.toLowerCase() : path

/** True when `child` IS `parent` or lives underneath it. */
export const isUnderPath = (parent: string, child: string): boolean => {
  const p = comparisonPath(cleanPath(parent))
  const c = comparisonPath(cleanPath(child))

  return c === p || c.startsWith(`${p}/`)
}

/** Join a base path with one or more relative segments using the same
 *  forward-slash spelling `cleanPath` produces, regardless of whether `base`
 *  itself is POSIX- or Windows-spelled (a local Windows backend reports
 *  backslash cwds). Segments are trimmed of leading/trailing slashes before
 *  joining so a caller never has to reason about double slashes.
 *  Replacement for hardcoded template-literal joins (interpolating a raw
 *  path variable followed by a literal slash), which break when that
 *  variable turns out to be backslash-separated. */
export const joinPath = (base: string, ...segments: string[]): string => {
  const cleanedBase = cleanPath(base)
  const cleanedSegments = segments.map(segment => segment.trim().replace(/^\/+|\/+$/g, '')).filter(Boolean)

  return [cleanedBase, ...cleanedSegments].join('/')
}
