"""Per-session archive/unarchive by explicit id.

The bulk, filter-driven selector lives in :mod:`hermes_state_maintenance`; it is pinned to
metadata predicates and (unless ``include_open``) to ended sessions.  This sibling owns the
complementary path: a caller that has already *decided* which sessions to hide acts on the
ids directly, with no filter round-trip and no ``ended_at`` gate — the same semantics
:meth:`SessionMaintenanceMixin.archive_stale_sessions` already relies on.

Archive stays a reversible soft-hide: every flip goes through
:meth:`SessionSessionsMixin.set_session_archived`, so a compression lineage moves as a unit.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple


class SessionArchiveMixin:
    """Resolve and archive/unarchive explicit session ids for SessionDB."""

    def resolve_session_ids(
        self, session_ids: Optional[Iterable[str]],
    ) -> Tuple[List[str], List[str]]:
        """Split raw ids/prefixes into ``(resolved, unknown)``.

        Each entry goes through :meth:`resolve_session_id`, so unique prefixes work exactly as
        they do for single-id commands.  Resolved ids keep input order and are de-duplicated
        (two prefixes of one session collapse); anything that resolves to nothing is returned
        verbatim in *unknown* so a caller can report it instead of silently skipping it.
        """
        resolved: List[str] = []
        unknown: List[str] = []
        seen: set = set()
        for raw in session_ids or ():
            candidate = str(raw or "").strip()
            if not candidate:
                continue
            match = self.resolve_session_id(candidate)
            if not match:
                unknown.append(candidate)
            elif match not in seen:
                seen.add(match)
                resolved.append(match)
        return resolved, unknown

    def set_sessions_archived(
        self, session_ids: Optional[Iterable[str]], archived: bool,
    ) -> List[str]:
        """Archive (or unarchive) each id via :meth:`set_session_archived`; returns the ids flipped.

        Open sessions are archived like ended ones — ``ended_at`` is a lifecycle field, not a
        curation gate, and a session the user navigated away from never gets one.  Ids that no
        longer exist are skipped, so a racing delete cannot fail the whole batch; resolve first
        (:meth:`resolve_session_ids`) when unknown ids must be reported.
        """
        flipped: List[str] = []
        for session_id in session_ids or ():
            if session_id and self.set_session_archived(session_id, bool(archived)):
                flipped.append(session_id)
        return flipped
