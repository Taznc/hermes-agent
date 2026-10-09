"""Archive discovery consumes actual producer formats and pre-persistence records."""

from concurrent.futures import Future

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_fork.session_archive import SessionArchiveBlocked, archive_blockers
from hermes_state import SessionDB


@pytest.mark.parametrize(
    "kind", ["empty", "owned", "unrelated", "foreign", "completed"]
)
def test_real_checkpoint_producer_archive_contract(tmp_path, monkeypatch, kind):
    from tools import process_registry as processes
    from tools.process_registry import ProcessRegistry, ProcessSession

    writer = ProcessRegistry()
    reader = ProcessRegistry()
    monkeypatch.setattr(processes, "process_registry", reader)
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(processes, "_checkpoint_path", lambda: checkpoint)
    monkeypatch.setattr(
        ProcessRegistry, "_detached_host_fate", lambda self, pid, start: "running"
    )
    if kind != "empty":
        process = ProcessSession(
            id="proc_contract",
            command="build",
            parent_session_id="other" if kind == "unrelated" else "parent",
            profile_home=str(tmp_path / "foreign")
            if kind == "foreign"
            else str(tmp_path),
            pid=123,
            detached=True,
        )
        process.exited = kind == "completed"
        writer._running[process.id] = process
    writer._write_checkpoint()
    assert checkpoint.is_file(), "Real producer did not write the checkpoint"
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "desktop")
    try:
        if kind == "owned":
            assert archive_blockers(db, "parent") == ["checkpoint_background_process"]
            with pytest.raises(SessionArchiveBlocked):
                db.set_session_archived("parent", True)
        else:
            assert archive_blockers(db, "parent") == []
            assert db.set_session_archived("parent", True)
    finally:
        db.close()


def test_real_async_registration_blocks_before_persistence(tmp_path, monkeypatch):
    from tools import async_delegation as work

    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "desktop")
    monkeypatch.setattr(work, "_records", {})
    seen = []

    def before_persistence(record):
        seen.append(record)
        assert archive_blockers(db, "parent") == ["async_delegation"]
        with pytest.raises(SessionArchiveBlocked):
            db.set_session_archived("parent", True)

    monkeypatch.setattr(work, "_persist_dispatch", before_persistence)
    future = Future()
    future.set_result(None)
    monkeypatch.setattr(
        work,
        "_get_executor",
        lambda *args: type("Executor", (), {"submit": lambda *args: future})(),
    )
    token = set_hermes_home_override(tmp_path)
    try:
        result = work._dispatch_admitted(
            delegation_id="contract-child",
            goal="test",
            goals=None,
            context=None,
            toolsets=None,
            role="leaf",
            model=None,
            session_key="runtime-parent",
            parent_session_id="parent",
            runner=lambda: {},
            origin_ui_session_id="runtime-parent",
            origin_session_id="parent",
            interrupt_fn=None,
            max_async_children=2,
            progress_fn=None,
            capacity_error="full",
        )
        assert result["status"] == "dispatched"
        assert len(seen) == 1
    finally:
        reset_hermes_home_override(token)
        db.close()
