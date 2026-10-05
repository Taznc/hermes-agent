"""Authoritative, non-destructive archive refusal at the SessionDB write boundary."""

from __future__ import annotations

from contextlib import contextmanager
from types import MethodType

# Deliberate (user/CLI/API/transport) archive state clears the idle sweep's
# provenance stamp so upstream reopen never un-hides an intentional archive.
DELIBERATE_ARCHIVE_SET_SQL = ", auto_archived = 0"


class SessionArchiveBlocked(ValueError):
    """Owned work must finish or be explicitly controlled before archiving."""

    def __init__(self, blockers):
        self.blockers = sorted(set(blockers))
        super().__init__("Session has active owned work: " + ", ".join(self.blockers))


def lineage_ids(conn, session_id):
    """The same compression-only fanout as SessionDB._set_lineage_column."""
    rows = conn.execute(
        """WITH RECURSIVE ancestors(id) AS (
          SELECT ? UNION SELECT parent.id FROM ancestors a
          JOIN sessions child ON child.id=a.id
          JOIN sessions parent ON parent.id=child.parent_session_id
          WHERE parent.end_reason='compression'
        ), descendants(id) AS (
          SELECT id FROM ancestors UNION SELECT child.id FROM descendants d
          JOIN sessions parent ON parent.id=d.id
          JOIN sessions child ON child.parent_session_id=parent.id
          WHERE parent.end_reason='compression'
        ) SELECT id FROM descendants""",
        (session_id,),
    )
    return {row[0] for row in rows}


class TransactionView:
    """Bind canonical setters to one transaction; never owns/closes DB resources."""

    def __init__(self, db, conn):
        self.db = db
        self.conn = conn

    def __getattr__(self, name):
        value = getattr(self.db, name)
        if isinstance(value, MethodType) and value.__self__ is self.db:
            return MethodType(value.__func__, self)
        return value

    def set_session_archived(self, session_id, archived):
        # Admission was already performed for this transaction.
        return self._set_lineage_column(
            "archived", session_id, int(archived), extra_set_sql=DELIBERATE_ARCHIVE_SET_SQL
        )

    def _execute_write(self, fn, **kwargs):
        return fn(self.conn)

    def _read_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def _read_all(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    @contextmanager
    def _read_ctx(self):
        yield self.conn


def durable_blockers(db, conn, session_id):
    """Reuse transcript lease liveness without its destructive stale-row cleanup."""
    import time

    from hermes_state_messages import _stale_holder
    from hermes_state_sessions import _collect_delegate_child_ids

    ids = lineage_ids(conn, session_id)
    ids.update(_collect_delegate_child_ids(conn, list(ids)))
    now = time.time()
    for sid in ids:
        key = db._session_turn_lease_key_on_conn(conn, sid)
        for table, column, value in (
            ("session_turn_leases", "conversation_id", key),
            ("compression_locks", "session_id", sid),
        ):
            row = conn.execute(
                f"SELECT holder, expires_at FROM {table} WHERE {column}=?", (value,)
            ).fetchone()
            if row is not None and not _stale_holder(row, now):
                return ["turn_or_compression"]
    return []


def blockers_on_conn(db, conn, session_id, work):
    return sorted(
        set(durable_blockers(db, conn, session_id) + work.blockers(conn, session_id))
    )


def assert_archivable(db, conn, session_id, work):
    blockers = blockers_on_conn(db, conn, session_id, work)
    if blockers:
        raise SessionArchiveBlocked(blockers)


def archive_blockers(db, session_id, aliases=()):
    """Read-only advisory discovery; writes ALWAYS re-evaluate under admission locks."""
    from .activity import owned_work_scope

    with owned_work_scope(db, session_id, aliases) as work, db._read_ctx() as conn:
        return blockers_on_conn(db, conn, session_id, work)


# Public API retained for existing fork callers.
from .storage import set_archived as set_archived
