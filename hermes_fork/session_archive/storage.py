"""Storage admission entry point with an explicit anchor-resolvable module."""

from . import TransactionView, assert_archivable
from .activity import owned_work_scope


def set_archived(db, session_id, archived):
    if not archived:
        return db._set_lineage_column("archived", session_id, 0)

    with owned_work_scope(db, session_id) as work:

        def update(conn):
            assert_archivable(db, conn, session_id, work)
            return TransactionView(db, conn)._set_lineage_column(
                "archived", session_id, 1
            )

        return db._execute_write(update)
