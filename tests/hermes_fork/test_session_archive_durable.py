"""Durable ownership remains discoverable when no parent turn is running."""

import json
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


def test_durable_async_dispatch_blocks_without_live_registry(tmp_path, monkeypatch):
    from tools import async_delegation as async_work

    monkeypatch.setattr(async_work, "_records", {})
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "desktop")
    token = set_hermes_home_override(tmp_path)
    try:
        async_work._persist_dispatch({
            "delegation_id": "dispatch-test",
            "dispatched_at": time.time(),
            "parent_session_id": "parent",
        })
        with pytest.raises(ValueError, match="active owned work"):
            db.set_session_archived("parent", True)
    finally:
        reset_hermes_home_override(token)
        db.close()


def test_checkpoint_process_blocks_without_live_registry(tmp_path, monkeypatch):
    from tools import process_registry as processes
    from tools.process_registry import (
        _CHECKPOINT_FIELDS,
        ProcessRegistry,
        ProcessSession,
    )

    registry = ProcessRegistry()
    monkeypatch.setattr(processes, "process_registry", registry)
    monkeypatch.setattr(
        ProcessRegistry,
        "_detached_host_fate",
        lambda self, pid, expected_start: "running",
    )
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", "desktop")
    process = ProcessSession(
        id="proc_durable",
        command="build",
        parent_session_id="parent",
        pid=123,
        detached=True,
        profile_home=str(tmp_path),
    )
    record = {field: getattr(process, field) for field in _CHECKPOINT_FIELDS}
    record["session_id"] = process.id
    checkpoint = tmp_path / "processes.json"
    checkpoint.write_text(json.dumps([record]))
    try:
        with pytest.raises(ValueError, match="active owned work"):
            db.set_session_archived("parent", True)
        assert checkpoint.is_file()
    finally:
        db.close()
