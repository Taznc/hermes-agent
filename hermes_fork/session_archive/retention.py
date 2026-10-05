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

            def set_session_archived(self, session_id, archived):
                try:
                    changed = db.set_session_archived(session_id, archived)
                except SessionArchiveBlocked:
                    return False
                self.archived += bool(changed)
                return changed

        view = RetentionView()
        fn(view, *args, **kwargs)
        return view.archived

    return wrapped
