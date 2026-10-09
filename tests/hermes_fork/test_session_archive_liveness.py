"""Use canonical in-memory registries; never spawn work in the serving HOME."""

import os
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("parent", source="desktop")
    yield database
    database.close()


@pytest.fixture
def runtime(db, monkeypatch):
    import tui_gateway.server as srv
    from tools import async_delegation
    from tools import delegate_tool_registry as children
    from tools.process_registry import process_registry

    monkeypatch.setattr(srv, "_sessions", {})
    monkeypatch.setattr(srv, "_get_db", lambda: db)
    monkeypatch.setattr(children, "_active_subagents", {})
    monkeypatch.setattr(async_delegation, "_records", {})
    monkeypatch.setattr(process_registry, "_running", {})
    return srv, children, process_registry


@pytest.mark.parametrize("flag", ["running", "starting", "queued", "waiting"])
def test_live_work_blocks_but_idle_open_does_not(db, runtime, flag):
    srv, _, _ = runtime
    record = {
        "session_key": "parent",
        "agent": SimpleNamespace(session_id="parent", _session_db=db),
        "profile_home": str(db.db_path.parent),
        "history_lock": threading.RLock(),
    }
    if flag == "queued":
        record["queued_prompt"] = {"text": "later"}
    elif flag == "waiting":
        record["pending_prompt"] = "approval"
    else:
        record[flag] = True
    srv._sessions["live-reconnected"] = record
    with pytest.raises(ValueError, match="active owned work"):
        db.set_session_archived("parent", True)
    assert db.get_session("parent")["archived"] == 0


def test_owned_child_survives_parent_idle_and_compression(db, runtime):
    _, children, _ = runtime
    db.end_session("parent", "compression")
    db.create_session("tip", source="desktop", parent_session_id="parent")
    children._active_subagents["child"] = {
        "owner_agent_session_id": "parent",
        "owner_session_id": "old-live-id",
        "owner_profile_home": str(db.db_path.parent),
        "status": "running",
    }
    with pytest.raises(ValueError, match="active owned work"):
        db.set_session_archived("tip", True)
    assert db.get_session("parent")["archived"] == 0
    assert db.get_session("tip")["archived"] == 0


def test_owned_background_process_blocks(db, runtime):
    from tools.process_registry import ProcessSession

    _, _, registry = runtime
    proc = ProcessSession("proc_test", "test", parent_session_id="parent")
    proc.profile_home = str(db.db_path.parent)
    registry._running[proc.id] = proc
    with pytest.raises(ValueError, match="active owned work"):
        db.set_session_archived("parent", True)
    assert not proc.exited


def test_foreign_profile_activity_cannot_block_same_id(db, runtime, tmp_path):
    from tools.process_registry import ProcessSession

    srv, children, registry = runtime
    foreign = str(tmp_path / "other-profile")
    srv._sessions["foreign"] = {
        "session_key": "parent",
        "running": True,
        "profile_home": foreign,
    }
    children._active_subagents["foreign-child"] = {
        "owner_agent_session_id": "parent",
        "owner_profile_home": foreign,
        "status": "running",
    }
    proc = ProcessSession("proc_foreign", "test", parent_session_id="parent")
    proc.profile_home = foreign
    registry._running[proc.id] = proc
    assert db.set_session_archived("parent", True)


def test_stale_client_check_cannot_win_over_new_turn_lease(db, runtime):
    from hermes_fork.session_archive import archive_blockers

    assert archive_blockers(db, "parent") == []
    started = threading.Event()

    def start_work():
        assert db.try_acquire_session_turn_lease(
            "parent", f"pid={os.getpid()}:turn=race"
        )
        started.set()

    thread = threading.Thread(target=start_work)
    thread.start()
    assert started.wait(3)
    thread.join(3)
    with pytest.raises(ValueError, match="active owned work"):
        db.set_session_archived("parent", True)


def test_retention_archive_skips_active_owned_work(db, runtime):
    _, children, _ = runtime
    db._write_sql("UPDATE sessions SET started_at=1, ended_at=2 WHERE id='parent'")
    children._active_subagents["retained-child"] = {
        "owner_agent_session_id": "parent",
        "owner_profile_home": str(db.db_path.parent),
    }
    assert db.archive_stale_sessions(1) == 0
    assert db.get_session("parent")["archived"] == 0
