"""Sticky per-board start budget for Kanban worker dispatch."""

from __future__ import annotations

import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def _spawn(_task, _workspace, board=None):
    return 43210


def test_resolve_dispatch_caps_includes_start_budget(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_start_budget": 20,
                "dispatch_start_window_seconds": 600,
            }
        },
    )

    caps = kbd.resolve_dispatch_caps()

    assert caps.dispatch_start_budget == 20
    assert caps.dispatch_start_window_seconds == 600


def test_start_budget_trips_sticky_board_pause(all_assignees_spawnable):
    board = "usage-guard"
    with kbc.connect(board=board) as conn:
        prior = kb.create_task(conn, title="already started", assignee="worker")
        queued = kb.create_task(conn, title="do not start", assignee="worker")
        kb._append_event(conn, prior, "spawned", {"pid": 1})
        conn.execute("UPDATE tasks SET status = 'on_hold' WHERE id = ?", (prior,))
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )

        assert result.spawned == []
        assert result.dispatch_paused is not None
        assert result.dispatch_paused["reason"] == "start_budget_exceeded"
        assert result.dispatch_paused["recent_starts"] == 1
        assert kb.get_task(conn, queued).status == "ready"

    # Sliding-window expiry alone must not silently resume a circuit that fired.
    with kbc.connect(board=board) as conn:
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE kind = 'spawned'",
            (int(time.time()) - 3600,),
        )
        conn.commit()
        still_paused = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )
        assert still_paused.spawned == []
        assert still_paused.dispatch_paused["reason"] == "start_budget_exceeded"

    cleared = kbd.resume_dispatch(board)
    assert cleared["was_paused"] is True
    assert kbd.read_dispatch_pause(board) is None

    with kbc.connect(board=board) as conn:
        resumed = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )
        assert [task_id for task_id, _who, _ws in resumed.spawned] == [queued]


def test_terminal_card_replay_violation_trips_pause_before_spawn():
    board = "replay-guard"
    with kbc.connect(board=board) as conn:
        terminal = kb.create_task(conn, title="was done", assignee="worker")
        claim = kb.claim_task(conn, terminal)
        assert claim is not None
        assert kb.complete_task(conn, terminal, result="finished") is True
        # Simulate corrupt/out-of-band lifecycle mutation without an explicit
        # unarchive transition, matching the incident this guard contains.
        conn.execute(
            "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
            (terminal,),
        )
        conn.commit()

        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=20,
            dispatch_start_window_seconds=600,
        )

        assert result.spawned == []
        assert result.dispatch_paused is not None
        assert result.dispatch_paused["reason"] == "terminal_card_replay"
        assert result.dispatch_paused["task_ids"] == [terminal]
        assert kb.get_task(conn, terminal).status == "ready"


def test_explicit_unarchive_is_not_treated_as_terminal_replay(
    all_assignees_spawnable,
):
    board = "explicit-reopen"
    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="deliberately reopened", assignee="worker")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, result="finished") is True
        assert kb.archive_task(conn, task_id) is True
        assert kb.unarchive_task(conn, task_id, status="ready") is True

        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=20,
            dispatch_start_window_seconds=600,
        )

        assert result.dispatch_paused is None
        assert result.spawned == []
        assert (task_id, "recent_success") in result.respawn_guarded


def test_unreadable_pause_state_fails_closed_until_explicit_resume(
    all_assignees_spawnable,
):
    board = "malformed-pause"
    pause_path = kbd._dispatch_pause_path(board)
    pause_path.parent.mkdir(parents=True, exist_ok=True)
    pause_path.write_text("{not-json", encoding="utf-8")

    with kbc.connect(board=board) as conn:
        kb.create_task(conn, title="must stay queued", assignee="worker")
        result = kbd.dispatch_once(conn, board=board, spawn_fn=_spawn)

    assert result.spawned == []
    assert result.dispatch_paused is not None
    assert result.dispatch_paused["reason"] == "pause_state_unreadable"
    resumed = kbd.resume_dispatch(board)
    assert resumed["was_paused"] is True
    assert kbd.read_dispatch_pause(board) is None
