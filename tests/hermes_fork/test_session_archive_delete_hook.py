"""The fork's guarded archive paths keep upstream's ``on_session_delete`` (#124511) contract.

Upstream fires the hook once, after commit, for every deliberate unarchived -> archived flip.
The fork routes ``SessionDB.set_session_archived`` through ``session-archive-storage-guard`` and the
TUI ``session.archive`` / messaging ``PATCH archived=true`` transports through
``transports.archive_mutation`` (``TransactionView``); both must still notify, exactly once.
"""

import pytest

from hermes_cli.plugins import get_plugin_manager
from hermes_fork.session_archive.transports import archive_mutation
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def events(db):
    manager = get_plugin_manager()
    saved = {name: list(cbs) for name, cbs in manager._hooks.items()}
    seen: list[dict] = []

    def _record(**kw):
        row = db.get_session(kw["session_id"])
        seen.append({**kw, "row_after": None if row is None else bool(row["archived"])})

    manager._hooks.setdefault("on_session_delete", []).append(_record)
    try:
        yield seen
    finally:
        manager._hooks = saved


def _chain(db, tip):
    db.create_session(f"{tip}-root", source="cli")
    db.end_session(f"{tip}-root", "compression")
    db.create_session(tip, source="cli", parent_session_id=f"{tip}-root")


def test_transport_archive_fires_once_after_commit(db, events):
    _chain(db, "t")
    archive = lambda view: view.set_session_archived("t", True)
    assert archive_mutation(db, "t", archive)
    assert archive_mutation(db, "t", archive)          # repeat archive: silent
    assert archive_mutation(db, "t", lambda view: view.set_session_archived("t", False))  # silent
    assert [(e["session_id"], e["reason"], e.get("deleted_ids"), e["row_after"]) for e in events] == [
        ("t", "archive", ["t", "t-root"], True)]


def test_storage_guarded_archive_fires_once(db, events):
    db.create_session("s", source="cli")
    assert db.set_session_archived("s", True)
    assert db.set_session_archived("s", True)
    assert db.set_session_archived("s", True, notify=False) is True
    assert [(e["session_id"], e["reason"], e["row_after"]) for e in events] == [("s", "archive", True)]


def test_refused_transport_archive_is_silent(db, events, monkeypatch):
    from hermes_fork import session_archive
    from hermes_fork.session_archive import transports

    db.create_session("busy", source="cli")

    def _refuse(*_a, **_k):
        raise session_archive.SessionArchiveBlocked(["turn_or_compression"])

    monkeypatch.setattr(transports, "assert_archivable", _refuse)
    with pytest.raises(session_archive.SessionArchiveBlocked):
        archive_mutation(db, "busy", lambda view: view.set_session_archived("busy", True))
    assert not db.get_session("busy")["archived"]
    assert events == []
