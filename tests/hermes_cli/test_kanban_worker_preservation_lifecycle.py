"""Kanban task-level preservation: ownership gating, event recording, and the
lifecycle hooks that fire it.

``kanban_preserve.preserve_worktree`` is the mechanism; this module decides
WHEN it is allowed to run for a given task and records what happened on the
board so a human can see the commit SHA, the push result, and any reason it
refused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_preserve as kp


def _git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    if check:
        assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "--initial-branch=main", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("config", "user.email", "t@example.com", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    _git("push", "-u", "origin", "main", cwd=project)
    return project


def _make_task(repo: Path, task_id: str = "t_demo") -> Path:
    """A ``worktree``-workspace task with a materialized linked worktree."""
    wt = repo / ".worktrees" / task_id
    _git("worktree", "add", "-b", f"wt/{task_id}", str(wt), "main", cwd=repo)
    with kbc.connect_closing() as conn:
        kb.create_task(
            conn, title="demo", assignee="worker",
            workspace_kind="worktree", workspace_path=str(wt),
        )
        conn.execute(
            "UPDATE tasks SET id = ?, workspace_path = ?, branch_name = ? "
            "WHERE title = 'demo'",
            (task_id, str(wt), f"wt/{task_id}"),
        )
        conn.commit()
    return wt


def _events(task_id: str, kind: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id", (task_id, kind),
        ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


# ---------------------------------------------------------------------------
# Happy path + event record
# ---------------------------------------------------------------------------


def test_preserving_a_task_records_sha_and_push_result_on_the_board(
    kanban_home: Path, repo: Path
) -> None:
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "preserved", result
    assert result.pushed is True
    events = _events("t_demo", "work_preserved")
    assert len(events) == 1
    assert events[0]["commit_sha"] == result.commit_sha
    assert events[0]["pushed"] is True
    assert events[0]["branch"] == "wt/t_demo"


def test_unsafe_snapshot_records_an_actionable_event_and_keeps_the_worktree(
    kanban_home: Path, repo: Path
) -> None:
    wt = _make_task(repo)
    (wt / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    before = _git("rev-parse", "HEAD", cwd=wt).strip()

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "unsafe", result
    events = _events("t_demo", "work_preservation_failed")
    assert len(events) == 1
    assert events[0]["reason"] == "suspected_secret"
    assert ".env" in events[0]["detail"]
    # Nothing committed; the worktree still holds the work for a human.
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before
    assert wt.is_dir()


def test_nothing_to_preserve_records_no_event(kanban_home: Path, repo: Path) -> None:
    wt = _make_task(repo)
    _git("push", "-u", "origin", "wt/t_demo", cwd=wt)

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "nothing_to_preserve", result
    assert _events("t_demo", "work_preserved") == []
    assert _events("t_demo", "work_preservation_failed") == []


# ---------------------------------------------------------------------------
# Ownership gating
# ---------------------------------------------------------------------------


def test_a_stale_run_never_preserves_over_a_newer_live_worker(
    kanban_home: Path, repo: Path
) -> None:
    """A reclaim path carrying an old run id must not snapshot work that the
    CURRENT run is still producing."""
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET current_run_id = 99 WHERE id = ?", ("t_demo",))
        conn.commit()
    before = _git("rev-parse", "HEAD", cwd=wt).strip()

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo", expected_run_id=42)

    assert result.status == "skipped", result
    assert result.reason == "stale_run"
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before


def test_matching_run_id_is_allowed_to_preserve(kanban_home: Path, repo: Path) -> None:
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET current_run_id = 42 WHERE id = ?", ("t_demo",))
        conn.commit()

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo", expected_run_id=42)

    assert result.status == "preserved", result


def test_a_live_worker_pid_blocks_preservation_by_another_process(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the owning worker is still alive, only that worker may snapshot;
    an outside process would race its editor."""
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", ("t_demo",))
        conn.commit()
    monkeypatch.setattr(kp, "_pid_alive", lambda pid: pid == 4242)
    before = _git("rev-parse", "HEAD", cwd=wt).strip()

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "skipped", result
    assert result.reason == "worker_alive"
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before


def test_the_owning_worker_itself_may_preserve(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?", (os.getpid(), "t_demo"),
        )
        conn.commit()
    monkeypatch.setattr(kp, "_pid_alive", lambda pid: True)

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "preserved", result


def test_a_dead_worker_pid_does_not_block_preservation(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET worker_pid = 4242 WHERE id = ?", ("t_demo",))
        conn.commit()
    monkeypatch.setattr(kp, "_pid_alive", lambda pid: False)

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "preserved", result


# ---------------------------------------------------------------------------
# Scope: only this task's own worktree
# ---------------------------------------------------------------------------


def test_non_worktree_workspaces_are_never_touched(
    kanban_home: Path, tmp_path: Path
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with kbc.connect_closing() as conn:
        kb.create_task(
            conn, title="scratchy", assignee="worker",
            workspace_kind="scratch", workspace_path=str(scratch),
        )
        conn.execute("UPDATE tasks SET id = 't_scratch' WHERE title = 'scratchy'")
        conn.commit()
        result = kp.preserve_task_work(conn, "t_scratch")

    assert result.status == "skipped", result
    assert result.reason == "not_a_worktree_workspace"


def test_a_missing_workspace_directory_is_a_safe_skip(
    kanban_home: Path, repo: Path
) -> None:
    wt = _make_task(repo)
    _git("worktree", "remove", "--force", str(wt), cwd=repo)

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "skipped", result
    assert result.reason == "workspace_missing"


def test_disabled_by_config_is_an_explicit_skip(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(kp, "_preservation_config", lambda: {"enabled": False})
    before = _git("rev-parse", "HEAD", cwd=wt).strip()

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "skipped", result
    assert result.reason == "disabled"
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before
