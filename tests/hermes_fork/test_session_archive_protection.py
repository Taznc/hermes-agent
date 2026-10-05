"""Archive guard regressions; all databases and runtime records are isolated."""

import os

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("parent", source="desktop")
    database.set_session_title("parent", "original")
    yield database
    database.close()


def test_active_turn_rejects_storage_archive(db):
    holder = f"pid={os.getpid()}:turn=archive-test"
    assert db.try_acquire_session_turn_lease("parent", holder)
    with pytest.raises(ValueError, match="active owned work"):
        db.set_session_archived("parent", True)
    assert db.get_session("parent")["archived"] == 0
    db.release_session_turn_lease("parent", holder)
    assert db.set_session_archived("parent", True)


def test_idle_open_session_and_unarchive_are_allowed(db):
    assert db.get_session("parent")["ended_at"] is None
    assert db.set_session_archived("parent", True)
    holder = f"pid={os.getpid()}:turn=unarchive-test"
    assert db.try_acquire_session_turn_lease("parent", holder)
    assert db.set_session_archived("parent", False)
    assert db.get_session("parent")["archived"] == 0
