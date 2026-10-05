"""Storage admission entry point with an explicit anchor-resolvable module."""

from . import DELIBERATE_ARCHIVE_SET_SQL, TransactionView, assert_archivable
from .activity import owned_work_scope


def _guarded_archive(db, session_id, write):
    """Run *write(view)* inside the owned-work admission transaction."""
    with owned_work_scope(db, session_id) as work:

        def update(conn):
            assert_archivable(db, conn, session_id, work)
            return write(TransactionView(db, conn))

        return db._execute_write(update)


def set_archived(db, session_id, archived):
    # A deliberate archive or unarchive owns the provenance, so clear the idle
    # sweep's auto_archived stamp in the same UPDATE (upstream contract).
    if not archived:
        return db._set_lineage_column(
            "archived", session_id, 0, extra_set_sql=DELIBERATE_ARCHIVE_SET_SQL
        )
    return _guarded_archive(
        db,
        session_id,
        lambda view: view._set_lineage_column(
            "archived", session_id, 1, extra_set_sql=DELIBERATE_ARCHIVE_SET_SQL
        ),
    )


def auto_archive(db, session_id):
    """The idle sweep's archive under the same admission guard as a deliberate one.

    Delegates to upstream ``_auto_archive_lineage`` bound to the guarded
    transaction, so upstream keeps owning the provenance-stamping SQL."""
    return _guarded_archive(
        db, session_id, lambda view: view._auto_archive_lineage(session_id)
    )
