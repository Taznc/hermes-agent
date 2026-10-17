"""Concurrent admission, retention and success-path coverage."""

import os
import threading

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    database.create_session("parent", "desktop")
    database.set_session_title("parent", "original")
    yield database
    database.close()


_COMPOUND = {"title": "changed", "archived": True, "hidden": True, "pinned": True, "unread": True}


def _authority_patch(db, payload, request_id):
    """Upstream's one metadata seam: REST, the gateway owner and the offline store all land here."""
    import hermes_state_runtime as rt

    epoch = rt.begin_runtime_epoch(db, instance_id="owner")
    return rt.mutate_runtime_session(
        db, epoch=epoch, principal_id="human", session_id="parent", request_id=request_id,
        expected_revision=db.get_session("parent")["runtime_revision"], operation="sidebar",
        payload=payload,
    )


def test_compound_patch_rechecks_after_work_starts_on_other_connection(db, monkeypatch):
    from hermes_fork.session_archive import archive_blockers
    from hermes_state_runtime import RuntimeStoreError

    assert archive_blockers(db, "parent") == []
    second = SessionDB(db.db_path)
    reached_write, admitted = threading.Event(), threading.Event()
    execute = db._execute_write
    errors = []

    def producer():
        try:
            assert reached_write.wait(5)
            assert second.try_acquire_session_turn_lease(
                "parent", f"pid={os.getpid()}:turn=race"
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            admitted.set()

    def delayed_execute(fn, **kwargs):
        reached_write.set()
        assert admitted.wait(5)
        return execute(fn, **kwargs)

    _authority_patch(db, {"title": "original"}, "warm")  # epoch + receipt tables exist
    monkeypatch.setattr(db, "_execute_write", delayed_execute)
    worker = threading.Thread(target=producer)
    worker.start()
    try:
        with pytest.raises(RuntimeStoreError) as refused:
            _authority_patch(db, _COMPOUND, "race")
        worker.join(5)
        assert not worker.is_alive() and not errors
        assert refused.value.reason == "session_archive_blocked"
        row = db.get_session("parent")
        assert row["title"] == "original"
        assert not row["archived"] and not row["hidden"] and not row["pinned"]
        assert row["last_read_at"] is None
    finally:
        admitted.set()
        worker.join(5)
        second.close()


def test_rest_idle_compound_patch_success_and_busy_unarchive(db):
    result = _authority_patch(db, _COMPOUND, "compound")
    assert result["archived"] is True
    row = db.get_session("parent")
    assert row["title"] == "changed" and row["archived"] and row["pinned"]
    assert row["last_read_at"] == 0
    assert db.try_acquire_session_turn_lease("parent", f"pid={os.getpid()}:turn=unarchive")
    # Unarchiving a busy session is never refused.
    assert _authority_patch(db, {"archived": False}, "unarchive")["archived"] is False
    assert not db.get_session("parent")["archived"]


def test_retention_skips_busy_and_continues(db):
    db.create_session("idle", "desktop")
    db._write_sql("UPDATE sessions SET started_at=1")
    assert db.try_acquire_session_turn_lease(
        "parent", f"pid={os.getpid()}:turn=retention"
    )
    assert db.archive_stale_sessions(1) == 1
    assert not db.get_session("parent")["archived"]
    assert db.get_session("idle")["archived"]


def test_read_only_discovery_does_not_reclaim_stale_lease(db):
    from hermes_fork.session_archive import archive_blockers

    assert db.try_acquire_session_turn_lease("parent", "test-expired")
    db._write_sql(
        "UPDATE session_turn_leases SET expires_at=1 WHERE conversation_id='parent'"
    )
    readonly = SessionDB(db.db_path, read_only=True)
    try:
        assert archive_blockers(readonly, "parent") == []
    finally:
        readonly.close()
    assert (
        db._read_one(
            "SELECT holder FROM session_turn_leases WHERE conversation_id='parent'"
        )[0]
        == "test-expired"
    )


def test_status_rpc_uses_same_authority(db, monkeypatch):
    import tui_gateway.server as srv

    monkeypatch.setattr(srv, "_get_db", lambda: db)
    monkeypatch.setattr(srv, "_sessions", {})
    method = srv._methods["fork.session.archive_status"]
    result = method(1, {"session_id": "parent"})["result"]
    assert result == {"session_key": "parent", "archivable": True, "blockers": []}
    assert db.try_acquire_session_turn_lease("parent", f"pid={os.getpid()}:turn=status")
    result = method(2, {"session_id": "parent"})["result"]
    assert not result["archivable"] and result["blockers"] == ["turn_or_compression"]
