"""Retention is best-effort: skip owned work, report only successful archives."""

from functools import wraps

from . import SessionArchiveBlocked


def skip_busy_archive(fn):
    @wraps(fn)
    def wrapped(db, *args, **kwargs):
        class RetentionView:
            archived = 0

            def __getattr__(self, name):
                return getattr(db, name)

            def _admit(self, archive):
                try:
                    changed = archive()
                except SessionArchiveBlocked:
                    return False
                self.archived += bool(changed)
                return changed

            def set_session_archived(self, session_id, archived):
                return self._admit(lambda: db.set_session_archived(session_id, archived))

            def _auto_archive_lineage(self, session_id):
                # Upstream's sweep archives through this provenance-stamping
                # helper; it must pass the same owned-work admission.
                from .storage import auto_archive

                return self._admit(lambda: auto_archive(db, session_id))

        view = RetentionView()
        fn(view, *args, **kwargs)
        return view.archived

    return wrapped
