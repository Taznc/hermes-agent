import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { searchSessions, type SessionInfo, type SessionSearchResult } from '@/hermes'
import { useI18n } from '@/i18n'
import { sessionMatchesSearch } from '@/lib/session-search'
import { $sidebarSessionByAnyId, $sidebarSortedSessions } from '@/store/sidebar-model'

import { SidebarSessionSkeletons } from './section-states'
import { SidebarSessionsSection } from './sessions-section'

// FTS results cover sessions that aren't in the loaded page; synthesize a
// minimal SessionInfo so they render in the same row component (resume works
// by id; the snippet stands in for the preview).

// The backend's FTS layer wraps matched terms in literal '>>>' / '<<<'
// highlight markers (sqlite snippet() delimiters — see hermes_state_search.py).
// The sidebar renders the snippet as plain text, so the markers must be
// stripped or a search for "foo" paints rows titled ">>>foo<<<".
// Exported for tests.
export function stripFtsMarkers(snippet: string): string {
  return snippet.replaceAll('>>>', '').replaceAll('<<<', '')
}

function searchResultToSession(result: SessionSearchResult): SessionInfo {
  const ts = result.session_started ?? Date.now() / 1000

  return {
    archived: false,
    cwd: null,
    ended_at: null,
    id: result.session_id,
    _lineage_root_id: result.lineage_root ?? null,
    input_tokens: 0,
    is_active: false,
    last_active: ts,
    message_count: 0,
    model: result.model ?? null,
    output_tokens: 0,
    preview: stripFtsMarkers(result.snippet ?? '').trim() || null,
    source: result.source ?? null,
    started_at: ts,
    title: null,
    tool_call_count: 0
  }
}

interface SidebarSearchSectionProps {
  /** Already-trimmed, non-empty search query. The parent only mounts this
   *  component while a query is active — see index.tsx. */
  query: string
  contentClassName: string
  rootClassName: string
  activeSessionId: null | string
  onArchiveSession: (sessionId: string) => void
  onBranchSession?: (sessionId: string, profile?: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  onToggleUnread: (sessionId: string) => void
}

/**
 * Owns every store subscription and effect that only exists to answer a
 * search query ($sidebarSortedSessions, $sidebarSessionByAnyId, the debounced
 * full-text search call). Mounted by the sidebar root only while a query is
 * active (`trimmedQuery` is local UI state in index.tsx, not a store), so an
 * ordinary session tick while search is inactive never touches this
 * subscription at all — the root itself carries zero session-derived stores.
 */
export function SidebarSearchSection({
  query,
  contentClassName,
  rootClassName,
  activeSessionId,
  onArchiveSession,
  onBranchSession,
  onDeleteSession,
  onResumeSession,
  onTogglePin,
  onToggleUnread
}: SidebarSearchSectionProps) {
  const { t } = useI18n()
  const s = t.sidebar

  const [serverMatches, setServerMatches] = useState<SessionSearchResult[]>([])
  const [searchPending, setSearchPending] = useState(false)

  // Full-text search across *all* sessions (not just the loaded page) so many
  // sessions stay findable. Debounced; loaded sessions are matched instantly
  // client-side and merged ahead of the server hits.
  useEffect(() => {
    let cancelled = false

    setSearchPending(true)

    const id = window.setTimeout(() => {
      void searchSessions(query)
        .then(res => {
          if (!cancelled) {
            setServerMatches(res.results)
          }
        })
        .catch(() => undefined)
        .finally(() => {
          if (!cancelled) {
            setSearchPending(false)
          }
        })
    }, 200)

    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [query])

  const sortedSessions = useStore($sidebarSortedSessions)
  const sessionByAnyId = useStore($sidebarSessionByAnyId)

  const searchResults = useMemo(() => {
    const out = new Map<string, SessionInfo>()

    for (const sess of sortedSessions) {
      if (sessionMatchesSearch(sess, query)) {
        out.set(sess.id, sess)
      }
    }

    for (const match of serverMatches) {
      if (out.has(match.session_id)) {
        continue
      }

      const loaded = sessionByAnyId.get(match.session_id)
      out.set(match.session_id, loaded ?? searchResultToSession(match))
    }

    return [...out.values()]
  }, [query, sortedSessions, serverMatches, sessionByAnyId])

  return (
    <SidebarSessionsSection
      activeSessionId={activeSessionId}
      contentClassName={contentClassName}
      emptyState={
        searchPending ? (
          <SidebarSessionSkeletons />
        ) : (
          <div className="wrap-anywhere grid min-h-24 place-items-center rounded-lg px-2 text-center text-xs text-(--ui-text-tertiary)">
            {s.noMatch(query)}
          </div>
        )
      }
      label={s.results}
      onArchiveSession={onArchiveSession}
      onBranchSession={onBranchSession}
      onDeleteSession={onDeleteSession}
      onResumeSession={onResumeSession}
      onToggle={() => undefined}
      onTogglePin={onTogglePin}
      onToggleUnread={onToggleUnread}
      open
      pinned={false}
      rootClassName={rootClassName}
      sessions={searchResults}
    />
  )
}
