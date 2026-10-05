"""Deliberate archive paths must own upstream's ``auto_archived`` provenance.

Upstream's idle sweep stamps ``auto_archived = 1`` and ``reopen_session`` un-hides
sweep-owned lineages. The fork routes deliberate archives through
``hermes_fork.session_archive`` (storage guard + ``TransactionView``), so those
setters must clear the stamp in the same guarded UPDATE, or a later reopen
would un-hide a session the user archived on purpose.
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _flags(db, session_id):
    row = db.get_session(session_id)
    return (row["archived"], row["auto_archived"])


def _swept(db, session_id):
    db.create_session(session_id, "cli")
    assert db._auto_archive_lineage(session_id)
    assert _flags(db, session_id) == (1, 1)


def test_manual_archive_after_sweep_clears_provenance(db):
    _swept(db, "s")
    db.set_session_archived("s", True)
    assert _flags(db, "s") == (1, 0)


def test_unarchive_after_sweep_clears_provenance(db):
    _swept(db, "s")
    db.set_session_archived("s", False)
    assert _flags(db, "s") == (0, 0)


def test_reopen_does_not_unhide_a_manual_archive(db):
    _swept(db, "s")
    db.set_session_archived("s", True)
    db.end_session("s", "user_exit")
    db.reopen_session("s")
    assert _flags(db, "s") == (1, 0)


def test_reopen_still_unhides_a_sweep_archive(db):
    # Control: upstream's contract for sweep-owned archives is preserved.
    _swept(db, "s")
    db.end_session("s", "user_exit")
    db.reopen_session("s")
    assert _flags(db, "s")[0] == 0


def test_admitted_transport_archive_clears_provenance(db):
    from hermes_fork.session_archive import TransactionView

    _swept(db, "s")
    db._execute_write(lambda conn: TransactionView(db, conn).set_session_archived("s", True))
    assert _flags(db, "s") == (1, 0)
    db.reopen_session("s")
    assert _flags(db, "s") == (1, 0)


def test_sweep_never_relabels_a_manual_archive(db):
    db.create_session("s", "cli")
    db.set_session_archived("s", True)
    db._auto_archive_lineage("s")
    assert _flags(db, "s") == (1, 0)
