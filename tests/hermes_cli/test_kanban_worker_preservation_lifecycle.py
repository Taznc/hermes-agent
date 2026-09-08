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
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
            conn,
            title="demo",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(wt),
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
            "ORDER BY id",
            (task_id, kind),
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
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (os.getpid(), "t_demo"),
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
            conn,
            title="scratchy",
            assignee="worker",
            workspace_kind="scratch",
            workspace_path=str(scratch),
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


# ---------------------------------------------------------------------------
# Cross-task workspace-path aliasing must never let one task commit over
# another task's live worktree
# ---------------------------------------------------------------------------


def test_two_tasks_aliasing_the_same_workspace_path_refuse_to_preserve(
    kanban_home: Path, repo: Path
) -> None:
    """A corrupt or aliased row (two task ids pointing at the SAME
    workspace_path) must never let preserving task A commit and push task B's
    worktree while attributing the event to A."""
    wt = _make_task(repo, task_id="t_owner")
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        kb.create_task(
            conn,
            title="alias",
            assignee="worker",
            workspace_kind="worktree",
            workspace_path=str(wt),
        )
        conn.execute(
            "UPDATE tasks SET id = 't_alias', workspace_path = ?, branch_name = ? "
            "WHERE title = 'alias'",
            (str(wt), "wt/t_owner"),
        )
        conn.commit()
    before = _git("rev-parse", "HEAD", cwd=wt).strip()

    with kbc.connect_closing() as conn:
        result_a = kp.preserve_task_work(conn, "t_owner")
        result_b = kp.preserve_task_work(conn, "t_alias")

    assert result_a.status == "skipped", result_a
    assert result_a.reason == "workspace_path_conflict"
    assert result_b.status == "skipped", result_b
    assert result_b.reason == "workspace_path_conflict"
    # Neither call touched the worktree.
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before
    assert _events("t_owner", "work_preserved") == []
    assert _events("t_alias", "work_preserved") == []


# ---------------------------------------------------------------------------
# An unexpected exception must still leave an auditable record
# ---------------------------------------------------------------------------


def test_an_unexpected_exception_still_records_a_failed_event_with_the_sha(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit can land locally before something unrelated blows up further
    down; the record must still show it happened, not go silent."""
    wt = _make_task(repo)
    (wt / "work.py").write_text("value = 1\n", encoding="utf-8")

    real_preserve_worktree = kp.preserve_worktree

    secret = "ghp_" + "A" * 36

    def _commit_then_blow_up(worktree, branch, **kwargs):
        real_preserve_worktree(worktree, branch, **kwargs)
        raise RuntimeError(f"simulated push failure with credential {secret}")

    monkeypatch.setattr(kp, "preserve_worktree", _commit_then_blow_up)

    with kbc.connect_closing() as conn:
        result = kp.preserve_task_work(conn, "t_demo")

    assert result.status == "failed", result
    assert result.reason == "preservation_error"
    # The commit genuinely landed; the record must name it.
    real_head = _git("rev-parse", "HEAD", cwd=wt).strip()
    assert result.commit_sha == real_head
    events = _events("t_demo", "work_preservation_failed")
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["reason"] == "preservation_error"
    assert events[0]["commit_sha"] == real_head
    assert events[0]["pushed"] is False
    assert events[0]["push_error"]
    assert secret not in json.dumps(events[0])


# ---------------------------------------------------------------------------
# End-to-end: archiving a RUNNING task with a live worker must not commit
# over that worker's half-written tree — the ownership pid must be captured
# before archive_task's own UPDATE clears tasks.worker_pid.
# ---------------------------------------------------------------------------


def test_archiving_a_task_with_a_live_worker_never_commits_its_worktree(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``archive_task`` is status-agnostic and — unlike complete/block/
    request_review — never terminates a running worker first. If preservation
    read ``tasks.worker_pid`` AFTER archive_task's own UPDATE cleared it to
    NULL, a still-running worker's dirty tree would look ownerless and get
    committed out from under it."""
    wt = _make_task(repo, task_id="t_live")
    (wt / "work.py").write_text("half-written\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'running', worker_pid = 424242 WHERE id = ?",
            ("t_live",),
        )
        conn.commit()
    before = _git("rev-parse", "HEAD", cwd=wt).strip()
    monkeypatch.setattr(kp, "_pid_alive", lambda pid: pid == 424242)

    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, "t_live") is True

    # No commit, no push — the live worker's tree is untouched.
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before
    assert _events("t_live", "work_preserved") == []
    # And the worktree directory survives (cleanup also refuses it).
    assert wt.is_dir()


def test_a_new_claim_between_archive_and_preservation_blocks_the_stale_caller(
    kanban_home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity captured before archive clears the old claim must
    corroborate the row read by preservation, not replace it. A new claim can
    land after the archive transaction commits but before its preservation
    hook runs; the stale archive caller must not snapshot that newer worker's
    dirty tree."""
    wt = _make_task(repo, task_id="t_reclaimed")
    (wt / "work.py").write_text("new worker is still editing\n", encoding="utf-8")
    with kbc.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = 7, worker_pid = 111 "
            "WHERE id = ?",
            ("t_reclaimed",),
        )
        conn.commit()
    before = _git("rev-parse", "HEAD", cwd=wt).strip()
    real_preserve = kb._preserve_task_work

    def _new_claim_then_preserve(
        conn, task_id, *, expected_run_id=None, known_worker_pid=None
    ):
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = 8, worker_pid = 222 "
            "WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        return real_preserve(
            conn,
            task_id,
            expected_run_id=expected_run_id,
            known_worker_pid=known_worker_pid,
        )

    monkeypatch.setattr(kb, "_preserve_task_work", _new_claim_then_preserve)
    monkeypatch.setattr(kp, "_pid_alive", lambda pid: pid == 222)

    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, "t_reclaimed") is True

    assert wt.is_dir()
    assert _git("rev-parse", "HEAD", cwd=wt).strip() == before
    assert _events("t_reclaimed", "work_preserved") == []
