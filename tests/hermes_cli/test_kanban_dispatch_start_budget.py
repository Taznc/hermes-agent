"""Sticky per-board start budget for Kanban worker dispatch."""

from __future__ import annotations

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


def test_start_budget_cooldown_expires_and_dispatches_without_manual_resume(
    all_assignees_spawnable, monkeypatch,
):
    board = "usage-guard"
    with kbc.connect(board=board) as conn:
        prior = kb.create_task(conn, title="starts first", assignee="worker")
        queued = kb.create_task(conn, title="starts after cooldown", assignee="worker")

        first = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )
        started_at = conn.execute(
            "SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'spawned'",
            (prior,),
        ).fetchone()[0]

        assert [task_id for task_id, _who, _ws in first.spawned] == [prior]
        assert first.dispatch_paused is not None
        assert first.dispatch_paused["reason"] == "start_budget_exceeded"

        # A second shared dispatcher entry point inside the inclusive window
        # defers the queued task; it must not need --resume-circuit later.
        limited = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )
        assert limited.spawned == []
        assert limited.dispatch_paused is not None
        assert limited.dispatch_paused["reason"] == "start_budget_exceeded"
        assert limited.dispatch_paused["recent_starts"] == 1
        assert limited.dispatch_paused["next_eligible_at"] == started_at + 601
        assert kb.get_task(conn, queued).status == "ready"
        # Keep the first synthetic worker from being reclaimed and retried; the
        # only eligible card after the window must be the queued one.
        conn.execute("UPDATE tasks SET status = 'on_hold' WHERE id = ?", (prior,))
        conn.commit()

    # The completed rolling window frees exactly one slot on
    # the next shared dispatcher tick; no --resume-circuit is required.
    monkeypatch.setattr(kbd.time, "time", lambda: started_at + 601)
    with kbc.connect(board=board) as conn:
        resumed = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )

        assert [task_id for task_id, _who, _ws in resumed.spawned] == [queued]
        assert resumed.dispatch_paused is not None
        assert resumed.dispatch_paused["reason"] == "start_budget_exceeded"
        assert kbd.read_dispatch_pause(board) is not None


def test_start_budget_next_eligible_time_accounts_for_lowered_hot_reload(monkeypatch):
    """A smaller live budget may require more than the oldest start to expire."""
    board = "hot-reload-exact-expiry"
    with kbc.connect(board=board) as conn:
        first = kb.create_task(conn, title="first", assignee="worker")
        second = kb.create_task(conn, title="second", assignee="worker")
        kb._append_event(conn, first, "spawned", {"pid": 1})
        kb._append_event(conn, second, "spawned", {"pid": 2})
        conn.execute("UPDATE tasks SET status = 'on_hold'")
        conn.execute("UPDATE task_events SET created_at = 900 WHERE task_id = ?", (first,))
        conn.execute("UPDATE task_events SET created_at = 950 WHERE task_id = ?", (second,))
        conn.commit()
        monkeypatch.setattr(kbd.time, "time", lambda: 1000)

        result = kbd.dispatch_once(
            conn, board=board, spawn_fn=_spawn,
            dispatch_start_budget=1, dispatch_start_window_seconds=100,
        )

    assert result.spawned == []
    assert result.dispatch_paused is not None
    assert result.dispatch_paused["recent_starts"] == 2
    assert result.dispatch_paused["next_eligible_at"] == 1051


def test_start_budget_caps_one_tick_across_ready_and_review_lanes(
    all_assignees_spawnable, monkeypatch,
):
    board = "ready-review-window"
    with kbc.connect(board=board) as conn:
        ready = kb.create_task(conn, title="ready", assignee="worker")
        review = kb.create_task(conn, title="review", assignee="worker")
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        conn.commit()
        monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)

        result = kbd.dispatch_once(
            conn, board=board, spawn_fn=_spawn, max_spawn=8,
            dispatch_start_budget=2, dispatch_start_window_seconds=600,
        )
        starts = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'spawned'"
        ).fetchone()[0]

    assert {task_id for task_id, _who, _ws in result.spawned} == {ready, review}
    assert starts == 2
    assert result.dispatch_paused is not None


def test_start_budget_does_not_bypass_a_contended_dispatch_tick_lock(
    all_assignees_spawnable,
):
    board = "start-budget-lock"
    with kbc.connect(board=board) as conn:
        queued = kb.create_task(conn, title="must start once", assignee="worker")
        db_path = kb.kanban_db_path(board=board)
        with kbc._dispatch_tick_lock(db_path) as held:
            assert held is True
            locked = kbd.dispatch_once(
                conn, board=board, spawn_fn=_spawn,
                dispatch_start_budget=1, dispatch_start_window_seconds=600,
            )
        started = kbd.dispatch_once(
            conn, board=board, spawn_fn=_spawn,
            dispatch_start_budget=1, dispatch_start_window_seconds=600,
        )

    assert locked.skipped_locked is True
    assert locked.spawned == []
    assert [task_id for task_id, _who, _ws in started.spawned] == [queued]


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

    # A safety pause is not a cooldown: changing time cannot restart this board.
    with kbc.connect(board=board) as conn:
        still_paused = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=20,
            dispatch_start_window_seconds=1,
        )
        assert still_paused.spawned == []
        assert still_paused.dispatch_paused is not None
        assert still_paused.dispatch_paused["reason"] == "terminal_card_replay"


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


def test_descendant_invalidation_is_not_treated_as_terminal_replay(
    all_assignees_spawnable,
):
    board = "parent-reopen"
    with kbc.connect(board=board) as conn:
        task_id = kb.create_task(conn, title="invalidated child", assignee="worker")
        assert kb.claim_task(conn, task_id) is not None
        assert kb.complete_task(conn, task_id, result="finished") is True
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
            kb._append_event(
                conn,
                task_id,
                "descendant_invalidated",
                {"parent_id": "parent"},
            )

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


def test_malformed_start_budget_pause_state_fails_closed(all_assignees_spawnable):
    board = "malformed-rate-limit"
    pause_path = kbd._dispatch_pause_path(board)
    pause_path.parent.mkdir(parents=True, exist_ok=True)
    pause_path.write_text('{"reason": "start_budget_exceeded"}\n', encoding="utf-8")

    with kbc.connect(board=board) as conn:
        kb.create_task(conn, title="must stay queued", assignee="worker")
        result = kbd.dispatch_once(
            conn,
            board=board,
            spawn_fn=_spawn,
            dispatch_start_budget=1,
            dispatch_start_window_seconds=600,
        )

    assert result.spawned == []
    assert result.dispatch_paused is not None
    assert result.dispatch_paused["reason"] == "pause_state_unreadable"


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

    with kbc.connect(board=board) as conn:
        still_paused = kbd.dispatch_once(conn, board=board, spawn_fn=_spawn)
    assert still_paused.spawned == []
    assert still_paused.dispatch_paused is not None
    assert still_paused.dispatch_paused["reason"] == "pause_state_unreadable"

    resumed = kbd.resume_dispatch(board)
    assert resumed["was_paused"] is True
    assert kbd.read_dispatch_pause(board) is None


def test_pause_state_follows_database_path_pin(monkeypatch):
    live_path = kbd._dispatch_pause_path(None)
    sandbox_db = kb.kanban_home() / "sandbox" / "isolated.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(sandbox_db))

    sandbox_pause = kbd._dispatch_pause_path(None)

    assert sandbox_pause == sandbox_db.with_suffix(".dispatch-pause.json")
    assert sandbox_pause != live_path
