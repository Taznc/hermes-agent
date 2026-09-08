"""The preservation safety net must actually FIRE from every lifecycle path
that ends or reclaims a run — and cleanup must still refuse a workspace whose
preservation failed.

These are wiring tests: they assert the call sites exist and pass the right
ownership arguments, not that the preservation mechanism itself works (that is
``test_kanban_worker_preservation.py``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_preserve as kp


def _git(*args: str, cwd: str | Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
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
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record every ``preserve_task_work`` invocation, without running it."""
    seen: list[tuple] = []

    def _spy(conn, task_id, *, expected_run_id=None, known_worker_pid=None):
        seen.append((task_id, expected_run_id, known_worker_pid))
        return kp.PreserveResult(status="nothing_to_preserve")

    monkeypatch.setattr(kp, "preserve_task_work", _spy)
    return seen


def _task(conn, task_id: str, *, status: str = "running", run_id: int | None = None,
          pid: int | None = None, lock: str | None = None) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, workspace_kind, "
        " workspace_path, branch_name, current_run_id, worker_pid, claim_lock, "
        " created_at, started_at) "
        "VALUES (?, 'demo', 'worker', ?, 'worktree', '/nonexistent', ?, ?, ?, ?, "
        " strftime('%s','now'), strftime('%s','now'))",
        (task_id, status, f"wt/{task_id}", run_id, pid, lock),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Normal end-of-run paths
# ---------------------------------------------------------------------------


def test_complete_task_preserves_before_cleanup(kanban_home: Path, calls: list) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_c1")
        kb.complete_task(conn, "t_c1", summary="done")

    assert ("t_c1", None, None) in [(t, r, p) for (t, r, p) in calls]


def test_request_review_preserves(kanban_home: Path, calls: list) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_r1", run_id=None)
        kb.request_review(conn, "t_r1", summary="please review", force=True)

    assert any(t == "t_r1" for (t, _, _) in calls)


def test_block_task_preserves(kanban_home: Path, calls: list) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_b1")
        kb.block_task(conn, "t_b1", reason="need input", kind="needs_input")

    assert any(t == "t_b1" for (t, _, _) in calls)


def test_archive_task_preserves(kanban_home: Path, calls: list) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_a1", status="ready")
        kb.archive_task(conn, "t_a1")

    assert any(t == "t_a1" for (t, _, _) in calls)


# ---------------------------------------------------------------------------
# Ownership pid must be captured BEFORE the lifecycle UPDATE clears it
# ---------------------------------------------------------------------------


def test_complete_task_passes_the_live_worker_pid_it_captured(
    kanban_home: Path, calls: list
) -> None:
    """``complete_task`` clears ``worker_pid`` to NULL in its own UPDATE, so the
    pid preservation must gate on has to be captured beforehand — otherwise a
    completed-while-running task would look ownerless to the safety net."""
    with kbc.connect_closing() as conn:
        _task(conn, "t_c2", pid=555)
        kb.complete_task(conn, "t_c2", summary="done")

    assert ("t_c2", None, 555) in calls


def test_block_task_passes_the_live_worker_pid_it_captured(
    kanban_home: Path, calls: list
) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_b2", pid=556)
        kb.block_task(conn, "t_b2", reason="need input", kind="needs_input")

    assert ("t_b2", None, 556) in calls


def test_request_review_passes_the_live_worker_pid_it_captured(
    kanban_home: Path, calls: list
) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_r2", pid=557, run_id=None)
        kb.request_review(conn, "t_r2", summary="please review", force=True)

    assert ("t_r2", None, 557) in calls


def test_archive_task_passes_the_live_worker_pid_it_captured(
    kanban_home: Path, calls: list
) -> None:
    """Archive is status-agnostic and never terminates a running worker — the
    captured pid is the ONLY way preservation can see it is still live."""
    with kbc.connect_closing() as conn:
        _task(conn, "t_a2", status="running", pid=558)
        kb.archive_task(conn, "t_a2")

    assert ("t_a2", None, 558) in calls


# ---------------------------------------------------------------------------
# Reclaim paths — the ones that exist precisely because the worker is gone
# ---------------------------------------------------------------------------


def test_manual_reclaim_preserves_before_releasing_the_claim(
    kanban_home: Path, calls: list
) -> None:
    with kbc.connect_closing() as conn:
        _task(conn, "t_m1", lock="host:1", pid=None)
        assert kb.reclaim_task(conn, "t_m1", reason="operator") is True

    assert any(t == "t_m1" for (t, _, _) in calls)


def test_stale_claim_release_preserves(
    kanban_home: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        kb, "_terminate_reclaimed_worker", lambda *a, **k: {"terminated": True},
    )
    monkeypatch.setattr(kb, "_worker_survived_termination", lambda t: False)
    with kbc.connect_closing() as conn:
        _task(conn, "t_s1", lock=f"{kb._host_prefix()}999", pid=999)
        conn.execute(
            "UPDATE tasks SET claim_expires = 1 WHERE id = ?", ("t_s1",),
        )
        conn.commit()
        kb.release_stale_claims(conn)

    assert any(t == "t_s1" for (t, _, _) in calls)


def test_timed_out_run_preserves_with_its_own_run_id(
    kanban_home: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout sweep knows exactly which run it is killing, so it must pass
    that run id — otherwise it could snapshot over a newer worker."""
    monkeypatch.setattr(kbd, "_terminate_reclaimed_worker", lambda *a, **k: {"terminated": True})
    monkeypatch.setattr(kbd, "_worker_survived_termination", lambda t: False)
    with kbc.connect_closing() as conn:
        _task(conn, "t_t1", lock=f"{kb._host_prefix()}998", pid=998, run_id=7)
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds = 1, started_at = 1 WHERE id = ?",
            ("t_t1",),
        )
        conn.commit()
        kbd.enforce_max_runtime(conn)

    assert any(t == "t_t1" and r == 7 for (t, r, _) in calls)


def test_dead_worker_sweep_preserves(
    kanban_home: Path, calls: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    with kbc.connect_closing() as conn:
        _task(conn, "t_d1", lock=f"{kb._host_prefix()}997", pid=997, run_id=8)
        kbd._reclaim_dead_workers(conn)

    assert any(t == "t_d1" for (t, _, _) in calls)


# ---------------------------------------------------------------------------
# Cleanup must still refuse work that preservation could not save
# ---------------------------------------------------------------------------


def test_cleanup_still_refuses_a_dirty_unpushed_workspace_after_failed_preservation(
    kanban_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The safety net makes cleanup MORE likely to succeed; it must never make
    an unsafe removal possible when preservation refused."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "--initial-branch=main", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("config", "user.email", "t@example.com", cwd=project)
    _git("config", "user.name", "Test", cwd=project)
    (project / "README.md").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=project)
    _git("commit", "-m", "init", cwd=project)
    _git("push", "-u", "origin", "main", cwd=project)
    wt = project / ".worktrees" / "t_x1"
    _git("worktree", "add", "-b", "wt/t_x1", str(wt), "main", cwd=project)
    # Content preservation must refuse: a credential-named file.
    (wt / ".env").write_text("TOKEN=abc\n", encoding="utf-8")

    with kbc.connect_closing() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, workspace_kind, "
            " workspace_path, branch_name, created_at) "
            "VALUES ('t_x1', 'demo', 'worker', 'running', 'worktree', ?, 'wt/t_x1', "
            " strftime('%s','now'))",
            (str(wt),),
        )
        conn.commit()
        result = kp.preserve_task_work(conn, "t_x1")
        assert result.status == "unsafe", result
        kbw._cleanup_worktree_workspace("t_x1", str(wt), "wt/t_x1")

    # The worktree survives: dirty and unpushed, exactly as cleanup requires.
    assert wt.is_dir()
    assert (wt / ".env").exists()
