"""Board-wide circuit for a proven restart-safe scope prerequisite outage."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tools import process_registry


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _to_review(conn, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))


def test_scope_prerequisite_fault_pauses_board_without_burning_cards(
    kanban_home, all_assignees_spawnable,
):
    """The first typed scope failure defers every ready/review card untouched."""
    board = "shared-scope-outage"
    spawn_calls: list[str] = []

    def unavailable(task, _workspace, board=None):
        spawn_calls.append(task.id)
        raise process_registry.RestartSafeScopeUnavailable("scope user bus is unavailable")

    with kbc.connect(board=board) as conn:
        ready_first = kb.create_task(conn, title="first", assignee="worker")
        ready_second = kb.create_task(conn, title="second", assignee="worker")
        review = kb.create_task(conn, title="review", assignee="reviewer")
        _to_review(conn, review)
        conn.commit()

        result = kbd.dispatch_once(conn, board=board, spawn_fn=unavailable)

        assert spawn_calls == [ready_first]
        assert result.dispatch_paused is not None
        assert result.dispatch_paused["reason"] == "restart_safe_scope_unavailable"
        assert result.dispatch_paused["fault_code"] == "systemd_user_scope_unavailable"
        assert result.dispatch_paused["trigger_task_id"] == ready_first
        for task_id, expected_status in (
            (ready_first, "ready"),
            (ready_second, "ready"),
            (review, "review"),
        ):
            task = kb.get_task(conn, task_id)
            assert task is not None
            assert task.status == expected_status
            assert task.consecutive_failures == 0
            assert task.last_failure_error is None
        events = kb.list_events(conn, ready_first)
        tripped = [event for event in events if event.kind == "dispatch_circuit_tripped"]
        assert len(tripped) == 1
        assert tripped[0].payload["fault_code"] == "systemd_user_scope_unavailable"

    # A new dispatcher connection sees the durable board pause and does not call
    # a repaired/otherwise-valid spawner until an operator explicitly resumes.
    with kbc.connect(board=board) as conn:
        paused = kbd.dispatch_once(conn, board=board, spawn_fn=lambda *_a, **_k: pytest.fail("must not spawn"))
        assert paused.dispatch_paused is not None
        assert paused.dispatch_paused["trigger_task_id"] == ready_first

    cleared = kbd.resume_dispatch(board)
    assert cleared["was_paused"] is True
    with kbc.connect(board=board) as conn:
        resumed = kbd.dispatch_once(conn, board=board, spawn_fn=lambda *_a, **_k: 4242, max_spawn=1)
    resumed_ids = [task_id for task_id, _assignee, _workspace in resumed.spawned]
    # The existing review-reservation policy may give the one available slot to
    # review; recovery proves that an explicit resume permits exactly one safe
    # probe rather than silently re-arming while the circuit remains tripped.
    assert len(resumed_ids) == 1
    assert resumed_ids[0] in {ready_first, ready_second, review}


def test_scope_classifier_uses_the_typed_boundary_not_error_text():
    typed = process_registry.RestartSafeScopeUnavailable("scope is unavailable")

    assert kbd._is_shared_launcher_prerequisite_fault(typed) is True
    assert kbd._is_shared_launcher_prerequisite_fault(
        RuntimeError("systemd-run --user --scope is unavailable")
    ) is False


def test_unrelated_spawn_failure_keeps_per_task_failure_semantics(
    kanban_home, all_assignees_spawnable,
):
    """Only the typed launch-prerequisite boundary trips the board circuit."""
    board = "ordinary-failure"
    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="credentials", assignee="worker")

        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("credential rejected")),
        )

        task = kb.get_task(conn, task_id)
        assert result.dispatch_paused is None
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert task.last_failure_error == "credential rejected"
