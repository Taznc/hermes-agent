"""active_pr release on a deliberate requeue (``hermes_fork.kanban.pr_requeue``).

Drives the real ``dispatch_once`` -> ``_dispatch_lane_task`` path on an
isolated board, so the ``pr-requeue-recovery`` FORK ANCHOR is what is under
test. ``spawn_fn`` records which card a worker would have started for.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_fork.kanban import pr_requeue

PR = "https://github.com/Taznc/hermes-agent/pull/4242"
OTHER_PR = "https://github.com/Taznc/hermes-agent/pull/4343"


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A dispatcher-spawned worker inherits HERMES_KANBAN_DB & co. pointing at
    # the LIVE board; clear every one before any path resolves.
    for var in [v for v in os.environ if v.startswith("HERMES_KANBAN_")]:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path()
    assert str(db_path).startswith(str(tmp_path)), f"NOT ISOLATED: {db_path}"
    kb.init_db()

    import hermes_cli.config as cfgmod
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {})
    spawned: list[str] = []

    def spawn(task, workspace):
        spawned.append(task.id)
        return None

    with kbc.connect() as conn:
        assert Path(conn.execute("PRAGMA database_list").fetchone()["file"]) == db_path
        yield conn, spawned, spawn


def _age(conn, tid, seconds=100):
    """Push every existing comment/event into the past, so the next write is
    strictly newer (the guard compares whole-second timestamps)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE task_comments SET created_at = created_at - ? WHERE task_id = ?", (seconds, tid))
        conn.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ?", (seconds, tid))


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else None
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind),
        )
    ]


def _guarded(res, tid):
    return dict(res.respawn_guarded).get(tid)


def _pr_then_dependency_block(conn, spawned, spawn):
    """Worker opens a PR, then blocks on a dependency (card -> todo)."""
    parent = kb.create_task(conn, title="parent", assignee="builder")
    tid = kb.create_task(conn, title="card", assignee="coder")
    kb.claim_task(conn, parent)  # parent busy: the tick below only spawns the card
    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert spawned == [tid]
    run_id = kb.get_task(conn, tid).current_run_id
    kb.add_comment(conn, tid, "coder", f"Opened {PR} for review.")
    kb.link_tasks(conn, parent, tid, expected_child_run_id=run_id)
    assert kb.block_task(conn, tid, reason="needs parent", kind="dependency", expected_run_id=run_id)
    assert kb.get_task(conn, tid).status == "todo"
    return parent, tid


def _land_parent(conn, parent, tid):
    _age(conn, tid)
    assert kb.complete_task(conn, parent, summary="done", force=True)
    assert kb.get_task(conn, tid).status == "ready"
    assert _events(conn, tid, "promoted")


def test_promotion_after_pr_comment_releases_guard_with_receipt(board):
    conn, spawned, spawn = board
    parent, tid = _pr_then_dependency_block(conn, spawned, spawn)
    _land_parent(conn, parent, tid)
    # Upstream alone holds the card for the whole 24h window.
    assert kbd.check_respawn_guard(conn, tid) == "active_pr"

    res = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert _guarded(res, tid) is None
    assert spawned == [tid, tid]
    assert kb.get_task(conn, tid).status == "running"
    (receipt,) = _events(conn, tid, "active_pr_recovery")
    assert receipt["pr_urls"] == [PR]
    assert receipt["requeue_event"] == "promoted"
    assert receipt["recovery_reason"] == "requeued"
    # The existing-PR context reaches the resumed worker's packet.
    ctx = kb.build_worker_context(conn, tid)
    assert f"PR recovery: {PR}" in ctx
    assert "do not create a duplicate PR" in ctx


@pytest.mark.parametrize("requeue", ["unblock", "reclaim"])
def test_other_deliberate_requeues_release(board, requeue):
    conn, spawned, spawn = board
    tid = kb.create_task(conn, title="card", assignee="coder")
    kbd.dispatch_once(conn, spawn_fn=spawn)
    kb.add_comment(conn, tid, "coder", f"PR: {PR}")
    if requeue == "unblock":
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.block_task(conn, tid, reason="?", kind="needs_input", expected_run_id=run_id)
        _age(conn, tid)
        assert kb.unblock_task(conn, tid)
    else:
        _age(conn, tid)
        assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=lambda *a, **k: None)
    assert kb.get_task(conn, tid).status == "ready"

    res = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert _guarded(res, tid) is None
    assert spawned == [tid, tid]


def test_no_requeue_stays_guarded(board):
    conn, spawned, spawn = board
    tid = kb.create_task(conn, title="card", assignee="coder")
    kb.add_comment(conn, tid, "coder", f"Opened {PR}")
    _age(conn, tid)
    # A crash is not a deliberate requeue.
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "crashed", {"pid": 1})

    res = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert _guarded(res, tid) == "active_pr"
    assert spawned == []
    assert _events(conn, tid, "active_pr_recovery") == []


def test_newer_unrelated_pr_comment_rearms_guard(board):
    conn, spawned, spawn = board
    parent, tid = _pr_then_dependency_block(conn, spawned, spawn)
    _land_parent(conn, parent, tid)
    _age(conn, tid, 50)
    kb.add_comment(conn, tid, "someone", f"see also {OTHER_PR}")

    res = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert _guarded(res, tid) == "active_pr"
    assert spawned == [tid]
    assert _events(conn, tid, "active_pr_recovery") == []


def test_same_second_tie_stays_guarded(board):
    conn, spawned, spawn = board
    parent, tid = _pr_then_dependency_block(conn, spawned, spawn)
    now = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    assert kb.complete_task(conn, parent, summary="done", force=True)
    with kb.write_txn(conn):  # pin comment and promotion to the same second
        conn.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (now, tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (now, tid))

    res = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert _guarded(res, tid) == "active_pr"


def test_dry_run_previews_release_without_writing(board):
    conn, spawned, spawn = board
    parent, tid = _pr_then_dependency_block(conn, spawned, spawn)
    _land_parent(conn, parent, tid)
    comments = len(kb.list_comments(conn, tid))

    res = kbd.dispatch_once(conn, spawn_fn=spawn, dry_run=True)

    assert tid in [s[0] for s in res.spawned]
    assert _events(conn, tid, "active_pr_recovery") == []
    assert len(kb.list_comments(conn, tid)) == comments
    assert kb.get_task(conn, tid).status == "ready"


def test_receipt_is_not_pr_evidence_after_failed_spawn(board):
    conn, spawned, spawn = board
    parent, tid = _pr_then_dependency_block(conn, spawned, spawn)
    _land_parent(conn, parent, tid)

    def boom(task, workspace):
        raise RuntimeError("spawn exploded")

    kbd.dispatch_once(conn, spawn_fn=boom, failure_limit=10)
    assert kb.get_task(conn, tid).status == "ready"
    assert len(_events(conn, tid, "active_pr_recovery")) == 1

    res = kbd.dispatch_once(conn, spawn_fn=spawn, failure_limit=10)

    assert _guarded(res, tid) is None
    assert spawned == [tid, tid]


def test_non_active_pr_reasons_pass_through(board):
    conn, _spawned, _spawn = board
    tid = kb.create_task(conn, title="card", assignee="coder")
    for reason in (None, "recent_success", "blocker_auth", "rate_limit_cooldown"):
        assert pr_requeue.release(conn, tid, reason, dry_run=False) == reason
    assert _events(conn, tid, "active_pr_recovery") == []
